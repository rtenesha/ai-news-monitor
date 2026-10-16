"""Tests for pain_digest: discussion collection, comment fetching, fragments,
AI analysis, rendering, cache. Fixtures patched at network boundary."""
from unittest.mock import patch

import pytest

import json as _json

import news_pipeline


def _hn_story(sid, score, title, text=None, kids=None):
    import time
    return {"id": sid, "score": score, "title": title, "text": text,
            "time": time.time() - 100, "url": None, "kids": kids or []}


def _reddit(fullname, score=0, num_comments=0, title="r post", selftext="",
            created_hours_ago=1):
    import time
    return {"name": fullname, "score": score, "num_comments": num_comments,
            "title": title, "selftext": selftext,
            "created_utc": time.time() - created_hours_ago * 3600,
            "permalink": f"/r/LocalLLaMA/comments/{fullname}/slug/"}


def _listing(posts):
    """Reddit /new.json response shape."""
    return {"kind": "Listing",
            "data": {"children": [{"kind": "t3", "data": p} for p in posts]}}


def test_collect_discussions_hn_top_and_ask():
    import pain_digest
    top = _hn_story(101, 150, "Show HN: vibe coding tool", kids=[1, 2])
    ask = _hn_story(102, 70, "Ask HN: how do you keep your agent in scope?",
                    text="how do you keep your agent from touching extra files?")
    low = _hn_story(103, 30, "Unrelated low-score story")
    irrelevant = _hn_story(104, 300, "Wayland desktop for Linux with skeuomorphic design")
    ask2 = _hn_story(105, 80, "Ask HN: what do you use for prompt testing?",
                     text="what do you use to test prompts?")
    with patch.object(pain_digest, "_hn_get_json") as g, \
         patch.object(pain_digest, "_fetch_reddit_discussions", return_value=[]):
        g.side_effect = [
            [101, 104, 103],        # topstories.json (порог 100)
            [102, 105],             # askstories.json (порог 50)
            top, irrelevant, low, ask, ask2,   # item/<id>.json, порядок ids
        ]
        got = pain_digest.collect_discussions(48)
    hn_ids = {d["hn_id"] for d in got if d["source"] == "Hacker News"}
    assert hn_ids == {101, 102, 105}, f"got {hn_ids}"
    assert any(d["_story"] is not None for d in got)          # story передан дальше
    assert any(d["summary"] and d["hn_id"] == 102 for d in got)  # selftext в summary
    assert all(d["reddit_fullname"] is None for d in got)
    assert len(got) <= pain_digest.PAIN_DISCUSSION_CAP


def test_collect_discussions_reddit_selection_by_score_or_comments():
    import pain_digest
    good = _reddit("t3_p1", score=25, num_comments=2)
    chatty = _reddit("t3_p2", score=3, num_comments=12)
    weak = _reddit("t3_p3", score=3, num_comments=2)
    weak["title"] = "полностью нерелевантное кино"   # keyword не пройдёт
    old = _reddit("t3_p4", score=99, num_comments=50, created_hours_ago=72)  # вне окна
    with patch.object(pain_digest, "_reddit_new_json", side_effect=[_listing([good, chatty, weak, old])]), \
         patch.object(pain_digest, "time_sleep"), \
         patch.object(pain_digest.news_pipeline, "REDDIT_SUBREDDITS", ["LocalLLaMA"]):
        got = pain_digest._fetch_reddit_discussions(48)
    assert {d["reddit_fullname"] for d in got} == {"t3_p1", "t3_p2"}
    assert all(d["_permalink"] for d in got)
    assert all(d["num_comments"] is not None for d in got)


def test_reddit_stage_disabled_by_flag():
    import pain_digest
    with patch.object(pain_digest, "_reddit_new_json") as nj:
        pain_digest._REDDIT_JSON_DISABLED = True
        assert pain_digest._fetch_reddit_discussions(48) == []
        assert nj.call_count == 0
        pain_digest._REDDIT_JSON_DISABLED = False   # не тащить состояние дальше


# --- Task 2: comment fetchers ---------------------------------------------

def _hn_comment(cid, text="comment body", kids=None, deleted=False, dead=False):
    return {"id": cid, "text": text, "kids": kids or [], "deleted": deleted,
            "dead": dead}


def _hn_disc(kids):
    return {"title": "Ask HN: agent scope?", "url": "https://news.ycombinator.com/item?id=7",
            "summary": "", "source": "Hacker News", "published": None,
            "score": 70, "num_comments": None, "hn_id": 7,
            "reddit_fullname": None, "_story": {"id": 7, "kids": kids},
            "_permalink": None}


def test_fetch_hn_comments_caps_and_skips_deleted():
    import pain_digest
    # 12 веток первого уровня; у первых двух — по 1 ребёнку (второй уровень);
    # deleted/dead/пустые пропускаются
    branches = [_hn_comment(1000 + i, f"top {i} &#x27;quoted&#x27;" if i == 0
                            else (f"top {i} <b>bold</b>" if i == 1 else f"top {i}"),
                            kids=[2000 + i]) if i < 2
                else _hn_comment(1000 + i, f"top {i}")
                for i in range(12)]
    branches[3] = _hn_comment(1003, "", deleted=True)          # пустой текст
    branches[4] = _hn_comment(1004, "deleted", deleted=True)
    branches[5] = _hn_comment(1005, "dead", dead=True)
    store = {f"item/{b['id']}.json": b for b in branches}
    store.update({f"item/{i}.json": _hn_comment(i, f"reply {i - 2000}")
                  for i in (2000, 2001)})
    disc = _hn_disc([b["id"] for b in branches])
    with patch.object(pain_digest, "_hn_get_json", side_effect=lambda path: store[path]):
        comments = pain_digest.fetch_hn_comments(disc)
        assert len(comments) == 11   # 9 top + 2 replies (deleted/dead/пустые пропущены)
        assert len(pain_digest.fetch_hn_comments(disc, max_comments=10)) == 10
    texts = {c["text"] for c in comments}
    assert any("top 0 'quoted'" in t for t in texts)     # unescape
    assert any("top 1 bold" in t for t in texts)         # html-теги убраны
    c0 = next(c for c in comments if "top 0" in c["text"])
    assert c0["depth"] == 0 and c0["comment_url"] == "https://news.ycombinator.com/item?id=1000"
    c2000 = next(c for c in comments if "reply 0" in c["text"])
    assert c2000["depth"] == 1


def test_fetch_reddit_comments_parses_json_and_403_disables():
    import pain_digest
    child = {"data": {"body": "I can't keep context long", "permalink": "/r/x/comments/t3_p1/slug/a1/",
                      "replies": {"data": {"children": [
                          {"data": {"body": "reply works", "permalink": "/r/x/comments/t3_p1/slug/a2/",
                                    "replies": ""}}]}}}}
    payload = [{"data": {"children": [{"data": {}}]}},
               {"data": {"children": [child]}}]
    disc = {"title": "t", "url": "https://www.reddit.com/r/x/comments/t3_p1/slug/",
            "summary": "", "source": "Reddit: r/x", "published": None, "score": 30,
            "num_comments": 1, "hn_id": None, "reddit_fullname": "t3_p1",
            "_story": None, "_permalink": "/r/x/comments/t3_p1/slug/"}
    with patch.object(pain_digest, "_reddit_get_json", return_value=payload), \
         patch.object(pain_digest, "time_sleep"):
        comments = pain_digest.fetch_reddit_comments(disc)
    texts = [c["text"] for c in comments]
    assert "I can't keep context long" in texts and "reply works" in texts
    assert all(c["source"] == "Reddit" and c["depth"] in (0, 1) for c in comments)
    assert all(c["comment_url"].startswith("https://www.reddit.com") for c in comments)
    # 403/недоступность → [] и не роняет
    with patch.object(pain_digest, "_reddit_get_json", return_value=None), \
         patch.object(pain_digest, "time_sleep"):
        assert pain_digest.fetch_reddit_comments(disc) == []


# --- Task 3: fragments -----------------------------------------------------

def _disc_with_comments(title, summary, comments):
    return {"title": title, "url": f"https://news.ycombinator.com/item?id={abs(hash(title)) % 10**6}",
            "summary": summary, "source": "Hacker News", "published": None,
            "score": 100, "num_comments": None, "hn_id": abs(hash(title)) % 10**6,
            "reddit_fullname": None, "_story": {"id": 1, "kids": []},
            "_permalink": None, "_comments": comments}


def test_extract_fragments_markers_posts_and_cap():
    import pain_digest
    q = {"text": "is there a way to roll back the changes?",
         "comment_url": "https://news.ycombinator.com/item?id=1", "depth": 0,
         "source": "HN", "discussion_url": "https://news.ycombinator.com/item?id=7"}
    plain = {"text": "great work, shipped it today",
             "comment_url": "https://news.ycombinator.com/item?id=2", "depth": 0,
             "source": "HN", "discussion_url": "https://news.ycombinator.com/item?id=7"}
    d1 = _disc_with_comments("Ask HN: how do I keep my agent in scope",
                             "how do I keep the agent from touching extra files?", [q, plain])
    got = pain_digest.extract_fragments([d1])
    kinds = {(f["kind"], f["comment_url"]) for f in got}
    assert ("comment", q["comment_url"]) in kinds
    assert ("comment", plain["comment_url"]) not in kinds
    assert ("post", d1["url"]) in kinds                      # самопост — всегда фрагмент
    assert all(len(f["text"]) <= pain_digest.FRAGMENT_TEXT_MAX for f in got)
    assert all(f["discussion_title"] == d1["title"] for f in got)
    # кап: 3 обсуждения по 20 комментариев → 60 → 50
    many = _disc_with_comments("title LLM agent", "post body",
                               [{"text": f"how do you test {i}?",
                                 "comment_url": f"https://x/{i}", "depth": 0,
                                 "source": "HN", "discussion_url": "https://x/d"} for i in range(20)])
    got2 = pain_digest.extract_fragments([many, dict(many), dict(many)])
    assert len(got2) == pain_digest.FRAGMENT_CAP


# --- Task 4: AI analysis ---------------------------------------------------

def _fake_client(content: str):
    """Minimal Groq client stand-in, same pattern as test_tools_digest."""
    class _Message:
        def __init__(self, c): self.content = c

    class _Choice:
        def __init__(self, c): self.message = _Message(c)

    class _Resp:
        def __init__(self, c): self.choices = [_Choice(c)]

    class _Completions:
        @staticmethod
        def create(**kwargs):
            return _Resp(content)

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    return _Client()


def _frags():
    a = {"text": "is there a way to run the agent offline?",
         "source": "HN", "discussion_url": "https://news.ycombinator.com/item?id=11",
         "discussion_title": "Ask HN: agent offline?", "comment_url": "https://x/c1",
         "kind": "comment"}
    b = {"text": "context runs out too fast, struggling with long edits",
         "source": "Reddit", "discussion_url": "https://www.reddit.com/r/xyz/1",
         "discussion_title": "t", "comment_url": "https://www.reddit.com/r/xyz/1/a1",
         "kind": "comment"}
    return [a, b]


def _cards_raw(cards):
    return _json.dumps({"cards": cards, "notes": "n"})


def test_analyze_provenance_fabricated_quote_dropped():
    import pain_digest
    frags = _frags()
    ok_card = {"problem": "Контекст кончается",
               "category": "контекст/память", "essence": "жалуются на контекст",
               "hook": "хук", "content_angle": "разбор",
               "solution_status": "предложено",
               "potential": "живая боль",
               "evidence": [{"source": "Reddit",
                             "discussion_url": "https://www.reddit.com/r/xyz/1",
                             "comment_url": "https://www.reddit.com/r/xyz/1/a1",
                             "quote": "struggling with long edits"}]}
    bad_card = dict(ok_card)
    bad_card["evidence"] = [{"source": "HN",
                             "discussion_url": "https://news.ycombinator.com/item?id=99",
                             "comment_url": "https://news.ycombinator.com/item?id=404",
                             "quote": "неизвестная выдуманная цитата"}]
    res = pain_digest.analyze_fragments(frags, _fake_client(_cards_raw([ok_card, bad_card])))
    assert res["mode"] == "ai"
    assert [c["problem"] for c in res["cards"]] == ["Контекст кончается"]

    # цитата есть, но url не из переданного набора → карточка выброшена
    forged = dict(bad_card)
    forged["evidence"] = [{"source": "HN",
                           "discussion_url": "https://news.ycombinator.com/item?id=11",
                           "comment_url": "https://news.ycombinator.com/item?id=11",
                           "quote": "выдумка"}]
    res2 = pain_digest.analyze_fragments(frags, _fake_client(_cards_raw([forged])))
    assert res2["cards"] == []


def test_analyze_normalizes_unknown_category_and_recounts_evidence():
    import pain_digest
    frags = _frags()
    card = {"problem": "p", "category": "квант", "essence": "e", "hook": "h",
            "content_angle": "инструкция", "solution_status": "мб",
            "potential": "pot",
            "evidence": [{"source": "HN", "discussion_url": "https://d1",
                          "comment_url": "https://x/c1", "quote": "is there a way"},
                         {"source": "Reddit", "discussion_url": "https://d1",
                          "comment_url": "https://www.reddit.com/r/xyz/1/a1",
                          "quote": "struggling with long edits"},
                         {"source": "HN", "discussion_url": "https://d2",
                          "comment_url": "https://x/c1", "quote": "is there a way"}]}
    res = pain_digest.analyze_fragments(frags, _fake_client(_cards_raw([card])))
    c = res["cards"][0]
    assert c["category"] == "другое"
    assert c["solution_status"] == "не найдено"
    assert c["evidence_count"] == 2        # уникальные discussion_url, не 3
    assert c["single"] is False


def test_analyze_caps_six_cards_and_fallback_without_client():
    import pain_digest
    frags = _frags()
    card_tmpl = {"problem": "p{i}", "category": "выбор инструмента",
                 "essence": "e", "hook": "h", "content_angle": "разбор",
                 "solution_status": "предложено", "potential": "pot",
                 "evidence": [{"source": "HN", "discussion_url": "https://d1",
                               "comment_url": "https://x/c1", "quote": "is there a way"}]}
    cards = [dict(card_tmpl, problem=f"p{i}") for i in range(8)]
    res = pain_digest.analyze_fragments(frags, _fake_client(_cards_raw(cards)))
    assert len(res["cards"]) == 6
    res2 = pain_digest.analyze_fragments(frags, None)
    assert res2["mode"] == "fallback" and res2["cards"] == []
    # мусорная структура JSON → фолбэк, не краш
    res3 = pain_digest.analyze_fragments(frags, _fake_client("[1,2,3]"))
    assert res3["mode"] == "fallback"


# --- Task 5: rendering and statuses ----------------------------------------

def _card(problem, single=False, status="new"):
    return {"problem": problem, "category": "контекст/память",
            "essence": "суть", "hook": "хук про <b>жирный</b>",
            "content_angle": "разбор", "solution_status": "не найдено",
            "potential": "потенциал",
            "evidence": [{"source": "HN", "discussion_url": "https://d1",
                          "comment_url": f"https://news.ycombinator.com/item?id={i}",
                          "quote": "цитата <script>x</script>"}
                         for i in range(4)],
            "evidence_count": 9 if not single else 1,
            "single": single, "status": status}


def test_render_pain_cards_format_and_status():
    import pain_digest
    res = {"cards": [_card("Проблема одна"), _card("Единичная", single=True,
                                                      status="posted")],
           "notes": "заметки", "mode": "ai"}
    out = pain_digest.render_pain_cards(res)
    assert "🎯 <b>1. Проблема одна</b>" in out
    assert "обсуждений: 9" in out and "решения нет" in out
    assert "пока единичное" in out
    assert "Статус: опубликована" in out
    # не больше 3 proof-ссылок на карточку
    blocks = out.split("\n\n")
    assert blocks[0].count("news.ycombinator.com/item?id=") == 3


def test_build_pain_messages_limits_and_escape():
    import pain_digest
    card = _card("Инъекция <img src=x onerror=alert(1)>")
    res = {"cards": [card], "notes": "notes", "mode": "ai"}
    msgs = pain_digest.build_pain_messages(res)
    assert msgs and all(len(m) <= 4096 for m in msgs)
    assert "<img" not in " ".join(msgs)
    assert "&lt;img" in " ".join(msgs)
    fb = pain_digest.build_pain_messages({"cards": [], "notes": "", "mode": "fallback"})
    assert any("режим без AI" in m for m in fb)
    empty = pain_digest.build_pain_messages({"cards": [], "notes": "", "mode": "ai"})
    assert len(empty) == 1 and "не нашлось" in empty[0]


def test_set_pain_status_validates_number_and_vocab():
    import pain_digest
    store = {"pain_display_map": {1: "p3", 2: "p4"},
             "pain_status": {"p3": "new"}}
    assert pain_digest.set_pain_status(store, 1, "taken") is None
    assert store["pain_status"]["p3"] == "taken"
    assert isinstance(pain_digest.set_pain_status(store, 9, "taken"), str)
    assert isinstance(pain_digest.set_pain_status(store, 1, "banished"), str)
    assert isinstance(pain_digest.set_pain_status({"pain_display_map": {}}, 1, "new"), str)