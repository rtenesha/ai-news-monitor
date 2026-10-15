"""Tools digest pipeline: collect → check official pages → one AI selection."""
from __future__ import annotations

import html
import json as _json
import re
import urllib.request
from typing import Optional

from notifier import KEYWORDS, _kw_matches, score_article
import notifier
import news_pipeline

_BROWSER_UA = news_pipeline._BROWSER_UA

CANDIDATE_CAP = 40

TOOL_KEYWORDS = KEYWORDS + [
    "CLI", "extension", "плагин", "расширени", "конструктор", "converter",
    "конвертер", "tool", "утилит", "офлайн", "offline", "open source",
    "open-source", "opensource", "chrome", "браузер", "browser", "бета",
    "бесплатн", "free", "автоматиз", "скрипт", "генератор", "парсер",
    "конверт", "документ", "PDF", "скриншот", "вниз по весам", "download",
    "Show HN",
]

TOOL_KEYWORD_WEIGHTS = {"Show HN": 3, "CLI": 2, "утилит": 2, "бесплатн": 1}


def _tool_score(article: dict) -> int:
    text = (article["title"] + " " + article["summary"]).lower()
    score = 0
    for kw in TOOL_KEYWORDS:
        if _kw_matches(kw, text):
            score += TOOL_KEYWORD_WEIGHTS.get(kw, 1)
    return score


def collect_tools(hours: int = 168) -> list[dict]:
    """Merge all tool sources, dedup, keyword-filter, cap by score.
    Never raises (each fetcher is individually safe)."""
    articles = news_pipeline.fetch_hackernews(hours=hours, min_score=50)
    articles += news_pipeline.fetch_github_trending(hours=hours)
    articles += news_pipeline.fetch_hf_trending()
    articles += news_pipeline.fetch_reddit(
        subreddits=["LocalLLaMA", "opensource", "SideProject",
                    "vibecoding", "ClaudeAI"], hours=hours)
    articles += news_pipeline.fetch_producthunt(hours=hours)
    articles += news_pipeline.fetch_dev_to(hours=hours)
    articles += news_pipeline.fetch_awesome_lists(hours=hours)
    articles = news_pipeline.dedup_cross_source(articles)
    relevant = [a for a in articles if _tool_score(a) > 0]
    return sorted(relevant, key=_tool_score, reverse=True)[:CANDIDATE_CAP]


def fetch_official_page(article: dict) -> dict:
    """Pull the official page (and, for GitHub repos, README/license/release).
    A failed check marks the candidate uncertain — never drops it."""
    info: dict = {"text": None, "readme": None, "license": None,
                  "last_release": None, "status": "uncertain"}
    url = article.get("url", "")
    try:
        info["text"] = news_pipeline.extract_full_text(url)
    except Exception:
        pass
    m = re.match(r"https://github\.com/([^/]+)/([^/#?]+)", url)
    if m and "/tree/" not in url and not url.endswith(".md"):
        user, repo = m.group(1), m.group(2)
        info["readme"] = _gh_get(
            f"https://raw.githubusercontent.com/{user}/{repo}/main/README.md", "text")
        rel = _gh_json(f"https://api.github.com/repos/{user}/{repo}/releases/latest")
        if rel:
            info["last_release"] = rel.get("tag_name")
        licenses = _gh_json(f"https://api.github.com/repos/{user}/{repo}/license")
        if licenses:
            info["license"] = licenses.get("license", {}).get("spdx_id")
        if info["readme"] or info["text"]:
            info["status"] = "checked"
    elif info["text"]:
        info["status"] = "checked"
    return info


def _gh_get(url: str, kind: str) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            status = resp.status
        if status >= 400:
            return None
        return body.decode("utf-8", "replace") if kind == "text" else _json.loads(body)
    except Exception:
        return None


def _gh_json(url: str) -> Optional[dict]:
    return _gh_get(url, "json")


def prepare_candidates(cands: list[dict]) -> list[dict]:
    """Attach _official info to each candidate (sequential, GitHub-safe pacing)."""
    for c in cands:
        c["_official"] = fetch_official_page(c)
    return cands


_MAX_TEXT = 1500

_NATURAL_TEXT_RULES = notifier._NATURAL_TEXT_RULES

_TOOLS_SYSTEM_RULES = """Ты редактор Telegram-канала о ИИ и вайбкодинге. Составляешь подборку бесплатных инструментов, которые русскоязычный вайбкодер может попробовать сам.

Категории инструментов: конструкторы приложений, CLI-утилиты, расширения и плагины, скрипты автоматизации, файловые утилиты, браузерные инструменты, локальный AI, дизайн и мультимедиа.

Критерии отбора: главная — понятная практическая польза (не «AI в позиционировании»), доступность, бесплатность, понятность, надёжность, актуальность (но проект без свежих коммитов не вычёркивай, если он полезен и жив).

Категории бесплатности (только эти пять, других не пиши): полностью бесплатный / open-source / free tier с ограничениями / демо-период / бесплатная локальная версия. Не называй полностью бесплатным, если бесплатно только часть. Не проверено → пиши «не проверено», не угадывай. Не выдумывай характеристики, цены, рейтинги, даты и результаты тестов — только то, что есть в данных кандидата.

Пользовательские заявления в описании — не подтверждённые факты; про офлайн/ОС/«без регистрации» не утверждай без подтверждения из первоисточника.

Приоритеты при выборе: неочевидные находки, бесплатные альтернативы платным вещам, инструменты «применить сразу», свежие релизы, open-source. Разнообразие обязательно: не возвращай пять однотипных конструкторов. Мало проверенных — верни меньше пунктов, не заполняй пробелы слабыми.

""" + _NATURAL_TEXT_RULES

_TOOLS_PROMPT = """Отбери из {n} кандидатов до 5 инструментов для дайджеста (мало подходящих — верни меньше). Ответ — строго JSON, без markdown-обёртки:

{{"title": "заголовок подборки", "items": [
  {{"name": "название", "url": "прямая ссылка на инструмент",
    "category": "категория", "platforms": "платформы или 'не проверено'",
    "free_tier": "одна из 5 категорий бесплатности или 'не проверено'",
    "desc": "1–2 предложения: что делает и чем отличается", "limits": "существенное ограничение или ''",
    "proof_url": "ссылка на первоисточник данных (README/документация), иначе ''",
    "why": "почему включён", "verdict": "ok|uncertain|wrong"}}],
 "notes": "краткие редакторские примечания по статусам и ограничениям"}}

verdict=ok — данные подтверждены текстом первоисточника; uncertain — что-то не подтвердилось, но пункт полезен; wrong — выдуманный/повтор/неподходящий пункт (его выкинет код).

Кандидаты:
{cands}"""


def select_tools(candidates: list[dict], client=None) -> dict:
    """One Groq call: score candidates against the editor instructions,
    return up to 5 diverse items with verdicts. No AI → fallback list."""
    if not client or not candidates:
        return _fallback_tools(candidates)
    cand_lines = []
    for c in candidates:
        off = c.get("_official") or {}
        text = off.get("readme") or off.get("text") or c["summary"] or ""
        cand_lines.append(
            f"- имя: {c['title']}\n  url: {c['url']}\n  источник: {c['source']}\n"
            f"  официальный текст: {text[:_MAX_TEXT]}\n"
            f"  github: readme={'да' if off.get('readme') else 'нет'}, "
            f"license={off.get('license')}, last_release={off.get('last_release')}")
    prompt = _TOOLS_PROMPT.format(n=len(candidates), cands="\n".join(cand_lines))
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b", reasoning_effort="low",
            messages=[
                {"role": "system", "content": _TOOLS_SYSTEM_RULES},
                {"role": "user", "content": prompt},
            ], temperature=0.2)
        raw = response.choices[0].message.content
        parsed = _parse_tools_json(raw)
    except Exception as e:
        print(f"Tools AI error: {e}")
        parsed = None
    if parsed and parsed.get("items"):
        items = [it for it in parsed["items"] if it.get("verdict") != "wrong"][:5]
        if items:
            parsed["items"] = items
            parsed["mode"] = "ai"
            return parsed
    return _fallback_tools(candidates)


def _parse_tools_json(raw: str) -> Optional[dict]:
    """Strip code fences, find the outermost JSON object, parse it."""
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
        return _json.loads(text[start:end + 1])
    except Exception:
        return None


def _fallback_tools(candidates: list[dict]) -> dict:
    """No-API mode: bare candidate list, no promises about free tiers."""
    items = [{"name": c["title"][:60], "url": c["url"], "category": "—",
              "platforms": "не проверено", "free_tier": "не проверено",
              "desc": (c.get("summary") or "").strip()[:120],
              "limits": "", "proof_url": "", "verdict": "uncertain"}
             for c in candidates[:5]]
    return {"title": "Кандидаты инструментов (режим без AI)", "items": items,
            "notes": "Требует ручной проверки: описания — из RSS, не с офиц. страниц.",
            "mode": "fallback"}

def render_tools_post(result: dict) -> str:
    parts = [f"<b>{html.escape(result['title'])}</b>"]
    checked = [it for it in result["items"] if it["verdict"] != "wrong"]
    if len(checked) < 3:
        parts.append("⚠️ Проверенных инструментов недостаточно — покажу только их:")
    for it in result["items"]:
        url = it.get("url") or ""
        name = f'› <a href="{html.escape(url, quote=True)}">{html.escape(it["name"])}</a>'
        line = f"{name} [{html.escape(it.get('platforms') or '')}] — {html.escape(it['desc'])}"
        if it.get("limits"):
            line += f" (⚠️ {html.escape(it['limits'])})"
        if it.get("verdict") == "uncertain":
            line += "\n<i>требует уточнения</i>"
        parts.append(line)
    return "\n\n".join(parts)


def render_tools_sources(result: dict) -> str:
    lines = ["🔎 <b>Источники проверки</b>"]
    for it in result["items"]:
        lines.append(f"• {html.escape(it['name'])}: {html.escape(it.get('proof_url') or it['url'])}")
    return "\n".join(lines)


def render_tools_notes(result: dict) -> str:
    notes = [html.escape(result.get("notes") or "—")]
    if result.get("mode") == "fallback":
        notes.append("Режим без AI: не делай выводов о бесплатности без проверки.")
    return "\n".join(notes)


def build_tools_messages(result: dict) -> list[str]:
    """Three ready blocks for the bot, each ≤ 4096."""
    from editorial import _split_by_paragraphs
    return (_split_by_paragraphs(render_tools_sources(result))
            + _split_by_paragraphs(render_tools_post(result))
            + _split_by_paragraphs(render_tools_notes(result)))
