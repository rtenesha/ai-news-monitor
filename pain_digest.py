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
from notifier import KEYWORDS, HIGH_VALUE, _kw_matches
from tools_digest import _stem_matches

_BROWSER_UA = news_pipeline._BROWSER_UA
HN_BASE = "https://hacker-news.firebaseio.com/v0"

_DEFAULT_HOURS = 48
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


def _hn_visit(cid: str, depth: int, out: list[dict], budget: list[int]) -> None:
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
    for kid in (comment.get("kids") or [])[:2]:
        if budget[0] <= 0:
            break
        _hn_visit(kid, depth + 1, out, budget)


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