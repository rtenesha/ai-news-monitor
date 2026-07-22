from news_pipeline import normalize_url, dedup_cross_source


def test_normalize_url_strips_tracking_params():
    a = normalize_url("https://example.com/article?utm_source=tg&utm_campaign=x")
    b = normalize_url("https://example.com/article")
    assert a == b


def test_normalize_url_ignores_scheme_www_and_trailing_slash():
    a = normalize_url("http://www.example.com/article/")
    b = normalize_url("https://example.com/article")
    assert a == b


def test_normalize_url_keeps_real_query_params():
    a = normalize_url("https://example.com/search?q=claude")
    b = normalize_url("https://example.com/search?q=gpt")
    assert a != b


def test_dedup_cross_source_merges_same_normalized_url():
    articles = [
        {"title": "A", "url": "https://example.com/x?utm_source=tg", "summary": "short",
         "source": "ZDNet", "published": None},
        {"title": "A", "url": "https://www.example.com/x", "summary": "a much longer summary text",
         "source": "Forbes", "published": None},
    ]
    result = dedup_cross_source(articles)
    assert len(result) == 1
    assert result[0]["summary"] == "a much longer summary text"
    assert result[0]["source"] == "Forbes + ZDNet"


def test_dedup_cross_source_keeps_distinct_urls():
    articles = [
        {"title": "A", "url": "https://example.com/1", "summary": "s1", "source": "X", "published": None},
        {"title": "B", "url": "https://example.com/2", "summary": "s2", "source": "Y", "published": None},
    ]
    result = dedup_cross_source(articles)
    assert len(result) == 2
