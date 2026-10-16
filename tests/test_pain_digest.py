"""Tests for pain_digest: discussion collection, comment fetching, fragments,
AI analysis, rendering, cache. Fixtures patched at network boundary."""
from unittest.mock import patch

import pytest

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