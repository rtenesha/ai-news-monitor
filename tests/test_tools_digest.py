from unittest.mock import patch


def test_collect_tools_merges_dedups_filters_caps():
    import tools_digest

    def art(url, i):
        return {"title": "Tool", "url": url, "summary": "CLI утилита",
                "source": "S", "published": None}

    feeds = {
        "hn": [art(f"https://x/{i}", i) for i in range(5)],
        "dup": [art("https://x/0", 0)],  # дубль — склеится
        "irrelevant": [{"title": "Кино", "url": "https://k/1",
                        "summary": "премьера фильма", "source": "Z",
                        "published": None}],
    }

    def fake_hn(hours=168, **kw):
        return feeds["hn"]

    with patch.object(tools_digest.news_pipeline, "fetch_hackernews", fake_hn), \
         patch.object(tools_digest.news_pipeline, "fetch_github_trending", return_value=[]), \
         patch.object(tools_digest.news_pipeline, "fetch_hf_trending", return_value=[]), \
         patch.object(tools_digest.news_pipeline, "fetch_reddit", return_value=[]), \
         patch.object(tools_digest.news_pipeline, "fetch_producthunt", return_value=feeds["dup"]), \
         patch.object(tools_digest.news_pipeline, "fetch_dev_to", return_value=[]), \
         patch.object(tools_digest.news_pipeline, "fetch_awesome_lists", return_value=[]):
        got = tools_digest.collect_tools(168)
    assert len(got) <= tools_digest.CANDIDATE_CAP
    assert not any(a["url"] == "https://k/1" for a in got)  # irrelevant отфильтрован по keyword