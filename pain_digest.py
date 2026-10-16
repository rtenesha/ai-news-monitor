"""Pain points pipeline: discussions → comments → fragments → one AI call → cards."""
from __future__ import annotations

import html
import json as _json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import news_pipeline
import notifier
from notifier import KEYWORDS, HIGH_VALUE, _kw_matches
from tools_digest import _stem_matches

_BROWSER_UA = news_pipeline._BROWSER_UA
HN_BASE = "https://hacker-news.firebaseio.com/v0"

_DEFAULT_HOURS = 48
DEFAULT_HOURS = _DEFAULT_HOURS  # публичное имя для bot.py
PAIN_DISCUSSION_CAP = 16
REDDIT_DISCUSSION_CAP = 8
HN_DISCUSSION_CAP = 8

HNS_TOP_COUNT = 60
HNS_ASK_COUNT = 30

_REDDIT_JSON_DISABLED = False

time_sleep = time.sleep  # module-level для патчей в тестах


# --- HN: discussion collectors -------------------------------------------

def _hn_get_json(path: str, timeout: int = 12) -> Optional[Any]:
    """Firebase item/list fetch with one retry. Never raises."""
    url = f"{HN_BASE}/{path}"
    for i in range(2):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return _json.loads(resp.read())
        except Exception:
            if i == 0:
                time.sleep(1)
    return None


def _window_ok(story: dict, cutoff: float) -> bool:
    t = story.get("time") or 0
    return t >= cutoff


def _story_to_discussion(story: dict, source: str) -> dict:
    sid = story.get("id")
    item_url = f"https://news.ycombinator.com/item?id={sid}"
    return {
        "title": story.get("title", ""),
        "url": story.get("url") or item_url,
        "summary": (story.get("text") or "").replace("<p>", " ").strip()[:600],
        "source": source or "Hacker News",
        "published": None,
        "score": story.get("score", 0),
        "num_comments": None,
        "hn_id": sid,
        "reddit_fullname": None,
        "_story": story,
        "_permalink": None,
    }


def _fetch_hn_discussions(hours: int) -> list[dict]:
    cutoff = time.time() - hours * 3600
    top_ids = _hn_get_json("topstories.json") or []
    ask_ids = _hn_get_json("askstories.json") or []
    all_ids = top_ids[:HNS_TOP_COUNT] + ask_ids[:HNS_ASK_COUNT]
    seen: set = set()
    stories = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for story in pool.map(_hn_get_json,
                              [f"item/{i}.json" for i in all_ids]):
            if not story or story.get("id") in seen:
                continue
            seen.add(story.get("id"))
            min_score = (100 if story["id"] in set(top_ids[:HNS_TOP_COUNT]) else 50)
            if story.get("score", 0) < min_score or not _window_ok(story, cutoff):
                continue
            stories.append(story)
    return [_story_to_discussion(story, "Hacker News") for story in stories]


def _pain_score(article: dict) -> int:
    """Keyword-балл релевантности: стем-матч по KEYWORDS, HIGH_VALUE дороже."""
    text = (article["title"] + " " + article["summary"]).lower()
    score = 0
    for kw in KEYWORDS:
        if _stem_matches(kw, text):
            score += 2 if kw.lower() in HIGH_VALUE else 1
    return score


# --- Comment fetchers -----------------------------------------------------

_HN_ENTITIES = [("&#x27;", "'"), ("&#x2F;", "/"), ("&gt;", ">"), ("&lt;", "<"),
                ("&quot;", '"'), ("&amp;", "&")]


def strip_html(text: str) -> str:
    s = str(text or "")
    for ent, ch in _HN_ENTITIES:
        s = s.replace(ent, ch)
    s = re.sub(r"<[^>]+>", " ", s)
    return " ".join(s.split())


def _hn_visit(cid: str, depth: int, out: list[dict], budget: list[int],
              max_depth: int = 1) -> None:
    comment = _hn_get_json(f"item/{cid}.json")
    if not comment or comment.get("deleted") or comment.get("dead"):
        return
    budget[0] -= 1
    text = strip_html(comment.get("text", ""))[:500]
    if text:
        out.append({
            "text": text,
            "comment_url": f"https://news.ycombinator.com/item?id={cid}",
            "depth": depth,
            "source": "HN",
            "discussion_url": "",
        })
    if depth >= max_depth:
        return
    for kid in (comment.get("kids") or [])[:2]:
        if budget[0] <= 0:
            break
        _hn_visit(kid, depth + 1, out, budget, max_depth)


def fetch_hn_comments(disc: dict, max_comments: int = 40) -> list[dict]:
    """BFS по kids: 12 веток, 2 уровня, кап 40 (deleted/dead/пустые — пропуск)."""
    story = disc.get("_story") or {}
    out: list[dict] = []
    budget = [max_comments]
    for kid in (story.get("kids") or [])[:12]:
        if budget[0] <= 0:
            break
        _hn_visit(kid, 0, out, budget)
    for c in out:
        c["discussion_url"] = disc["url"]
    return out


def _reddit_get_json(url: str) -> Optional[Any]:
    """GET Reddit JSON, никогда не кидает исключение; статус последней
    HTTP-ошибки остаётся в _reddit_last_status (403 включает деградацию)."""
    global _reddit_last_status
    _reddit_last_status = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        _reddit_last_status = e.code
        return None
    except Exception:
        return None


def _reddit_failed() -> bool:
    """Payload не пришёл из-за 403 — Reddit API недоступен этим ключом."""
    return _reddit_last_status == 403


def fetch_reddit_comments(disc: dict, max_comments: int = 30) -> list[dict]:
    """Comments via keyless /comments/<id>.json; недоступен → [] (и лог один раз)."""
    global _REDDIT_JSON_DISABLED
    if _REDDIT_JSON_DISABLED:
        return []
    perm = disc.get("_permalink")
    if not perm:
        return []
    payload = _reddit_get_json(f"https://www.reddit.com{perm}.json")
    if not isinstance(payload, list) or len(payload) < 2:
        if _reddit_failed():
            _REDDIT_JSON_DISABLED = True
        return []
    out: list[dict] = []
    def add(data: dict, depth: int) -> None:
        text = strip_html(data.get("body", ""))[:500]
        if not text or len(out) >= max_comments:
            return
        out.append({
            "text": text,
            "comment_url": "https://www.reddit.com" + (data.get("permalink") or perm),
            "depth": depth,
            "source": "Reddit",
            "discussion_url": "https://www.reddit.com" + perm,
        })
    children = payload[1].get("data", {}).get("children", [])
    for ch in children[:max_comments]:
        if len(out) >= max_comments:
            break
        d = ch.get("data", {})
        add(d, 0)
        replies = d.get("replies")
        if isinstance(replies, dict):
            for rc in replies.get("data", {}).get("children", []):
                if len(out) >= max_comments:
                    break
                add((rc.get("data") or {}), 1)
    return out


_reddit_last_status: Optional[int] = None


def attach_comments(discussions: list[dict]) -> list[dict]:
    """К каждому обсуждению — _comments; HN параллельно, Reddit с паузами."""
    hn = [d for d in discussions if d.get("hn_id") is not None]
    reddit = [d for d in discussions if d.get("reddit_fullname") is not None]
    if hn:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(fetch_hn_comments, hn))
        for d, comments in zip(hn, results):
            d["_comments"] = comments
    for i, d in enumerate(reddit):
        if i:
            time_sleep(2)
        try:
            d["_comments"] = fetch_reddit_comments(d)
        except Exception:
            d["_comments"] = []
    return discussions


# --- Fragments ------------------------------------------------------------

_PAIN_MARKERS = re.compile(
    r"\?|how do (you|i)|does anyone|any(one|body) (else )?know|is there a way|"
    r"can('t|not|'t get)|stuck|struggling|confus|frustrat|annoying|problem is|"
    r"the hard part|pain point|regression|wasted (hours|days)|wish.*handled|"
    r"what do you (use|recommend)|recommendat", re.I)

FRAGMENT_CAP = 50
FRAGMENT_TEXT_MAX = 400


def _fragment(disc: dict, text: str, kind: str, comment_url: str) -> dict:
    return {
        "text": (text or "").strip()[:FRAGMENT_TEXT_MAX],
        "source": "HN" if disc.get("hn_id") is not None else "Reddit",
        "discussion_url": disc["url"],
        "discussion_title": disc["title"],
        "comment_url": comment_url,
        "kind": kind,
    }


def extract_fragments(discussions: list[dict]) -> list[dict]:
    """Post bodies всегда, комментарии — по маркер-регексу; кап 50 по порядку."""
    fragments: list[dict] = []
    for disc in discussions:
        if disc.get("summary"):
            fragments.append(_fragment(disc, disc["summary"], "post", disc["url"]))
        for comment in disc.get("_comments") or []:
            if not _PAIN_MARKERS.search(comment.get("text", "")):
                continue
            fragments.append(_fragment(disc, comment["text"],
                                       "comment", comment["comment_url"]))
        if len(fragments) >= FRAGMENT_CAP:
            break
    return fragments[:FRAGMENT_CAP]


# --- AI analysis -----------------------------------------------------------

_PAIN_CATEGORIES = ("выбор инструмента", "агент не контролируется",
                    "контекст/память", "деплой", "ошибка/не работает",
                    "стоимость", "безопасность", "обучение", "другое")
PAIN_CATEGORIES = _PAIN_CATEGORIES
_SOLUTION_STATUSES = ("подтверждено", "предложено", "не найдено")
_PAIN_AI_MODEL = "openai/gpt-oss-120b"
_MAX_CARDS = 6

_PAIN_SYSTEM_RULES = """Ты редактор Telegram-канала о ИИ и вайбкодинге. Ищешь в обсуждениях (комментарии Hacker News и Reddit) проблемы и запросы аудитории и группируешь их в карточки редакционных возможностей.

Правила:
- Карточка описывает конкретную боль или запрос, а не тему.
- Цитаты брать ТОЛЬКО дословными обрезками из переданных фрагментов, без пересказа и выдумывания. Ссылки на обсуждения и комментарии — только из переданного набора.
- Ничего не выдумывать: если фрагменты не дают материала под категорию/решение — пиши честный статус («не найдено»).
- problem/essence/hook/potential — по-русски, простыми словами, коротко. Цитаты оставляй на языке источника.
- Группируй похожие боли из разных обсуждений в одну карточку, если это одна проблема.
- 10 комментариев в одной ветке — не 10 независимых подтверждений; уникальность считается по обсуждению.
""" + notifier._NATURAL_TEXT_RULES

_PAIN_PROMPT = """Вот фрагменты обсуждений за окно. Отбери и сгруппируй до 6 проблем аудитории. Ответ — строго JSON, без markdown-обёртки:

{{"cards": [
  {{"problem": "короткое имя проблемы (по-русски)",
    "category": "одна из: выбор инструмента | агент не контролируется | контекст/память | деплой | ошибка/не работает | стоимость | безопасность | обучение | другое",
    "essence": "суть 2-3 предложения: что спрашивают и почему это боль",
    "hook": "вариант хука для поста",
    "content_angle": "инструкция | разбор | сравнение | эксперимент | дискуссия",
    "solution_status": "подтверждено | предложено | не найдено",
    "potential": "почему это зайдёт аудитории, 1 предложение",
    "evidence": [
      {{"source": "HN|Reddit", "discussion_url": "...", "comment_url": "...", "quote": "дословная обрезка ≤200 зн из соответствующего фрагмента"}}]}}],
 "notes": "краткие редакторские примечания"}}

Фрагменты:
{frags}"""


def _parse_pain_json(raw: str) -> Optional[dict]:
    """Strip code fences, find outermost JSON object, parse it.
    Schema guard: cards must be a list (garbage shape → None → fallback)."""
    if not raw:
        return None
    text = raw.strip()
    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = _json.loads(text[start:end + 1])
    except Exception:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("cards"), list):
        return None
    parsed["cards"] = [c for c in parsed["cards"] if isinstance(c, dict)]
    return parsed


def _norm_quote(text: str) -> str:
    return " ".join(str(text or "").split())


def _normalize_card(card: dict, frag_index: dict) -> Optional[dict]:
    """Coerce fields, validate category/status/провенанс.
    Карточка с невалидным evidence отбрасывается (None)."""
    evidence = card.get("evidence")
    if not isinstance(evidence, list):
        return None
    cleaned = []
    for e in evidence:
        if not isinstance(e, dict):
            return None
        comment_url = str(e.get("comment_url") or "")
        frag = frag_index.get(comment_url)
        if frag is None:
            return None
        if _norm_quote(e.get("quote")) not in _norm_quote(frag["text"]):
            return None
        cleaned.append({"source": str(e.get("source") or frag["source"]),
                        "discussion_url": frag["discussion_url"],
                        "comment_url": comment_url,
                        "quote": _norm_quote(e.get("quote"))[:200]})
    category = str(card.get("category") or "").strip()
    status = str(card.get("solution_status") or "").strip()
    return {
        "problem": str(card.get("problem") or "")[:200],
        "category": category if category in _PAIN_CATEGORIES else "другое",
        "essence": str(card.get("essence") or ""),
        "hook": str(card.get("hook") or ""),
        "content_angle": str(card.get("content_angle") or "разбор"),
        "solution_status": status if status in _SOLUTION_STATUSES else "не найдено",
        "potential": str(card.get("potential") or ""),
        "evidence": cleaned,
    }


def analyze_fragments(fragments: list[dict], client=None) -> dict:
    """One Groq call over fragments → ≤6 problem cards with provenance.
    No client or bad structure → fallback list."""
    if not client or not fragments:
        return _fallback_cards(fragments)
    frag_index = {f["comment_url"]: f for f in fragments}
    frag_lines = []
    for f in fragments:
        frag_lines.append(
            f"- {f['source']} | {f['discussion_title']}\n"
            f"  discussion_url: {f['discussion_url']} | comment_url: {f['comment_url']}\n"
            f"  текст: {f['text']}")
    prompt = _PAIN_PROMPT.format(frags="\n".join(frag_lines))
    try:
        response = client.chat.completions.create(
            model=_PAIN_AI_MODEL, reasoning_effort="low",
            messages=[
                {"role": "system", "content": _PAIN_SYSTEM_RULES},
                {"role": "user", "content": prompt},
            ], temperature=0.2)
        parsed = _parse_pain_json(response.choices[0].message.content)
    except Exception as e:
        print(f"Pain AI error: {e}")
        parsed = None
    if parsed:
        cards = []
        for card in parsed["cards"]:
            normalized = _normalize_card(card, frag_index)
            if normalized:
                normalized["evidence_count"] = len({e["discussion_url"] for e in normalized["evidence"]})
                normalized["single"] = normalized["evidence_count"] == 1
                cards.append(normalized)
            if len(cards) >= _MAX_CARDS:
                break
        if cards:
            return {"cards": cards, "notes": str(parsed.get("notes") or ""),
                    "mode": "ai"}
    return _fallback_cards(fragments)


def _fallback_cards(fragments: list[dict]) -> dict:
    """No-API mode: bare fragment list with links, no cards."""
    lines = []
    for f in fragments[:10]:
        lines.append(f"- [{f['source']}] {f['discussion_title']}\n"
                     f"  {f['comment_url'] or f['discussion_url']}\n"
                     f"  {f['text'][:160]}")
    if not lines:
        lines = ["фрагментов не нашлось"]
    return {"cards": [],
            "notes": "Фрагменты (режим без AI, суть не классифицирована):\n"
                     + "\n".join(lines),
            "mode": "fallback"}


# --- Rendering and statuses ------------------------------------------------

STATUS_VOCAB = {"new", "taken", "posted", "dropped"}
_STATUS_LABELS = {"taken": "взята в работу", "posted": "опубликована",
                  "dropped": "отклонена"}


def render_pain_cards(result: dict) -> str:
    parts: list[str] = []
    if result.get("mode") == "fallback":
        parts.append("<i>режим без AI — фрагменты классифицируй вручную.</i>")
    for i, card in enumerate(result.get("cards", []), start=1):
        status = card.get("status", "new")
        head = f"🎯 <b>{i}. {html.escape(str(card.get('problem') or ''))}</b>"
        line2 = (f"{html.escape(str(card.get('category') or ''))} · "
                 f"{html.escape(str(card.get('content_angle') or ''))} · "
                 f"обсуждений: {card.get('evidence_count', 0)}")
        if card.get("single"):
            line2 += " · пока единичное"
        if card.get("solution_status") == "не найдено":
            line2 += " · решения нет"
        else:
            line2 += f" · решение {html.escape(str(card.get('solution_status') or ''))}"
        parts.append("\n".join([head, line2,
                                html.escape(str(card.get('essence') or '')),
                                f"💡 {html.escape(str(card.get('potential') or ''))}",
                                f"✏️ Хук: «{html.escape(str(card.get('hook') or ''))}»"]))
        if status in _STATUS_LABELS:
            parts[-1] += f"\nСтатус: {html.escape(_STATUS_LABELS[status])}"
        proof = [f'<a href="{html.escape(e["comment_url"], quote=True)}">💬</a>'
                 for e in (card.get("evidence") or [])[:3]]
        if proof:
            parts[-1] += "\n" + " ".join(proof)
    return "\n\n".join(parts)


def render_pain_notes(result: dict) -> str:
    lines = ["📝 <b>Примечания</b>", html.escape(str(result.get("notes") or "—"))]
    return "\n".join(lines)


def build_pain_messages(result: dict) -> list[str]:
    """Two blocks for the bot, each ≤ 4096."""
    from editorial import _split_by_paragraphs
    if result.get("mode") != "fallback" and not result.get("cards"):
        return ["Проблем аудитории за окно не нашлось — попробуйте /pain 168"]
    cards_block = render_pain_cards(result)
    notes_block = render_pain_notes(result)
    msgs = _split_by_paragraphs(cards_block) + _split_by_paragraphs(notes_block)
    return [m if len(m) <= 4096 else m[:4090] + "…" for m in msgs]


def set_pain_status(store: dict, number: int, status: str) -> Optional[str]:
    """Смена статуса карточки по номеру последней отрисовки.
    Возвращает строку-ошибку или None при успехе."""
    if status not in STATUS_VOCAB:
        return f"Неизвестный статус «{status}» (taken | posted | dropped | new)"
    display_map = store.get("pain_display_map") or {}
    card_id = display_map.get(number)
    if not card_id:
        known = ", ".join(str(n) for n in sorted(display_map)) or "—"
        return f"Карточки {number} в текущем списке нет (есть: {known})"
    store.setdefault("pain_status", {})[card_id] = status
    return None


# --- Cache plumbing --------------------------------------------------------

def _assign_ids(store: dict, cards: list[dict]) -> None:
    """Permanent ids p1, p2 … (counter survives rebuilds) + display map
    «номер последней отрисовки → id»."""
    counter = store.get("pain_counter", 0)
    display: dict = {}
    for i, card in enumerate(cards, start=1):
        counter += 1
        card["id"] = f"p{counter}"
        display[i] = card["id"]
    store["pain_counter"] = counter
    store["pain_display_map"] = display


def _render_messages(store: dict, result: dict) -> list[str]:
    statuses = store.get("pain_status") or {}
    for card in result.get("cards", []):
        card["status"] = statuses.get(card.get("id"), "new")
    return build_pain_messages(result)


def get_or_build_pain(store: dict, hours: int, client=None) -> list[str]:
    """Cache of the last run in bot_data-shaped dict; builds on miss or
    window change. seen excludes all AI-evaluated discussions."""
    if hours < 6:
        hours = 6
    if hours > 168:
        hours = 168
    cache = store.setdefault("pain_cache", {})
    if cache and cache.get("hours") == hours:
        return _render_messages(store, cache["result"])
    seen = store.setdefault("pain_seen", set())
    discussions = [d for d in collect_discussions(hours)
                   if d.get("url") and news_pipeline.normalize_url(d["url"]) not in seen]
    if not discussions:
        return ["Проблем аудитории за окно не нашлось — попробуйте /pain 168"]
    attach_comments(discussions)
    fragments = extract_fragments(discussions)
    if not fragments:
        store["pain_cache"] = {"hours": hours,
                               "result": {"cards": [], "notes": "", "mode": "ai"}}
        return ["Проблем аудитории за окно не нашлось — попробуйте /pain 168"]
    result = analyze_fragments(fragments, client)
    # seen: все обсуждения, чьи фрагменты ушли на AI (как tools_seen)
    seen |= {news_pipeline.normalize_url(f["discussion_url"]) for f in fragments}
    cards = result.get("cards") or []
    if cards:
        _assign_ids(store, cards)
    store["pain_cache"] = {"hours": hours, "result": result}
    return _render_messages(store, result)


# --- Reddit: JSON-based discussion collector ------------------------------

def _reddit_new_json(sub: str, hours: int) -> Optional[dict]:
    return _reddit_get_json(f"https://www.reddit.com/r/{sub}/new.json?limit=50")


def _fetch_reddit_discussions(hours: int) -> list[dict]:
    """Per-subreddit /new.json; отбор по score/num_comments; флаг-деградация."""
    global _REDDIT_JSON_DISABLED
    if _REDDIT_JSON_DISABLED:
        return []
    cutoff = time.time() - hours * 3600
    discussions = []
    subs = news_pipeline.REDDIT_SUBREDDITS
    for i, sub in enumerate(subs):
        if i:
            time_sleep(2)  # Reddit 429s on burst requests
        data = _reddit_new_json(sub, hours)
        if data is None:
            if _reddit_failed():
                _REDDIT_JSON_DISABLED = True  # 403: Reddit API недоступен этим ключом
                break
            continue
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            if post.get("score", 0) < 20 and post.get("num_comments", 0) < 10:
                continue
            if (post.get("created_utc") or 0) < cutoff:
                continue
            permalink = post.get("permalink")
            if not permalink or not post.get("title"):
                continue
            discussions.append({
                "title": post["title"],
                "url": "https://www.reddit.com" + permalink,
                "summary": (post.get("selftext") or "")[:600],
                "source": f"Reddit: r/{sub}",
                "published": None,
                "score": post.get("score", 0),
                "num_comments": post.get("num_comments"),
                "hn_id": None,
                "reddit_fullname": post.get("name"),
                "_story": None,
                "_permalink": permalink,
            })
    return discussions


# --- Merge ----------------------------------------------------------------

def collect_discussions(hours: int = _DEFAULT_HOURS) -> list[dict]:
    """Merge HN discussions and Reddit posts, dedup, keyword-filter, cap.
    Never raises (each fetcher is individually safe)."""
    discussions = _fetch_hn_discussions(hours) + _fetch_reddit_discussions(hours)
    discussions = news_pipeline.dedup_cross_source(discussions)
    relevant = [d for d in discussions if _pain_score(d) > 0]
    relevant.sort(key=_pain_score, reverse=True)
    hn = [d for d in relevant if d["hn_id"] is not None][:HN_DISCUSSION_CAP]
    reddit = [d for d in relevant if d["reddit_fullname"] is not None][:REDDIT_DISCUSSION_CAP]
    return hn + reddit