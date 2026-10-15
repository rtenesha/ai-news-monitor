import json

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


def test_fetch_official_page_github_enrichment():
    """GitHub URL: README + license + release pulled; page with readme → checked."""
    import tools_digest
    pages = {
        "https://raw.githubusercontent.com/a/b/main/README.md":
            b"# TOOL A readme body with install steps",
        "https://api.github.com/repos/a/b/releases/latest":
            json.dumps({"tag_name": "v1.2.0"}).encode(),
        "https://api.github.com/repos/a/b/license":
            json.dumps({"license": {"spdx_id": "MIT"}}).encode(),
    }

    def fake_urlopen(req, timeout=15):
        class R:
            status = 200

            def read(self): return pages[req.full_url]

            def __enter__(self): return self

            def __exit__(self, *a): return False
        return R()

    with patch.object(tools_digest.urllib.request, "urlopen", fake_urlopen), \
         patch.object(tools_digest.news_pipeline, "extract_full_text", return_value=None):
        art = {"title": "TOOL A", "url": "https://github.com/a/b",
               "summary": "CLI утилита", "source": "X", "published": None}
        info = tools_digest.fetch_official_page(art)
    assert "readme body" in info["readme"]
    assert info["license"] == "MIT"
    assert info["last_release"] == "v1.2.0"
    assert info["status"] == "checked"


def test_github_rate_limited_marks_uncertain():
    import tools_digest
    with patch.object(tools_digest.urllib.request, "urlopen",
                      side_effect=Exception("HTTP Error 403")), \
         patch.object(tools_digest.news_pipeline, "extract_full_text", return_value=None):
        art = {"title": "t", "url": "https://github.com/a/b", "summary": "s",
               "source": "X", "published": None}
        info = tools_digest.fetch_official_page(art)
    assert info["status"] == "uncertain"  # НЕ отбрасывается