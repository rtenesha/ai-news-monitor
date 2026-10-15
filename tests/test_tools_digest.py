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


# --- select_tools ---

def _fake_client(content: str):
    """Minimal Groq client stand-in, same pattern as tests/test_editorial.py."""
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


def _fake_articles(n):
    urls = [f"https://github.com/a/tool-{i}" if i % 2 == 0 else f"https://x.dev/app-{i}"
            for i in range(n)]
    return [{"title": f"Tool {i}", "url": urls[i],
             "summary": "CLI утилита для автоответов бесплатно", "source": "Test",
             "published": None} for i in range(n)]


def _prep(n):
    """prepare_candidates on fakes without real network."""
    import tools_digest
    with patch.object(tools_digest.news_pipeline, "extract_full_text", return_value=None), \
         patch.object(tools_digest.urllib.request, "urlopen", side_effect=OSError("no net")):
        return tools_digest.prepare_candidates(_fake_articles(n))


_TOOLS_RAW = json.dumps({
    "title": "Инструменты недели",
    "items": [
        {"name": "Tool A", "url": "https://github.com/a/tool-0",
         "category": "CLI", "platforms": "macOS, Linux", "free_tier": "open-source",
         "desc": "Быстро чистит папки проектов от артефактов сборки.",
         "limits": "Нужен Python 3.8+", "proof_url": "https://github.com/a/tool-0#readme",
         "verdict": "ok"},
        {"name": "Tool B", "url": "https://x.dev/app-1", "category": "Web",
         "platforms": "Web", "free_tier": "free tier",
         "desc": "Сканер безопасности.", "limits": "", "proof_url": "https://x.dev/app-1/docs",
         "verdict": "uncertain"},
        {"name": "Tool C", "url": "https://c.app", "category": "Web",
         "platforms": "Web", "free_tier": "не проверено",
         "desc": "Выдуман.", "limits": "", "proof_url": "",
         "verdict": "wrong"}],
    "notes": "лимитов нет"}, ensure_ascii=False)


def test_select_tools_verdicts_and_cap():
    import tools_digest
    cands = _prep(8)
    result = tools_digest.select_tools(cands, _fake_client(_TOOLS_RAW))
    names = [it["name"] for it in result["items"]]
    assert "Tool C" not in names            # wrong отсеян
    assert result["items"][0]["verdict"] == "ok"
    assert result["mode"] == "ai"
    assert len(result["items"]) <= 5


def test_select_tools_all_wrong_falls_back():
    """Every item wrong → no items → fallback candidates, not an empty post."""
    import tools_digest
    all_wrong = json.dumps({"title": "T",
                            "items": [{"name": "X", "url": "https://x.dev", "category": "Web",
                                       "platforms": "Web", "free_tier": "?", "desc": "d",
                                       "limits": "", "proof_url": "", "verdict": "wrong"}] * 3},
                           ensure_ascii=False)
    result = tools_digest.select_tools(_prep(4), _fake_client(all_wrong))
    assert result["mode"] == "fallback"
    assert result["items"]


def test_select_tools_without_ai_fallback():
    import tools_digest
    result = tools_digest.select_tools(_prep(8), client=None)
    assert result["mode"] == "fallback"
    assert all("name" in it and "url" in it for it in result["items"])


def test_select_tools_parse_garbage_falls_back():
    import tools_digest
    result = tools_digest.select_tools(_prep(4), _fake_client("```json\n{bad"))
    assert result["mode"] == "fallback"


# --- render blocks ---

def _ok_item(desc="Важная утилита, которую можно попробовать сразу.", i=0):
    return {"name": f"Tool {chr(65 + i)}", "url": f"https://t{i}.example",
            "category": "CLI", "platforms": "macOS, Linux",
            "free_tier": "open-source", "desc": desc,
            "limits": "Нужен Python 3.8+", "proof_url": f"https://t{i}.example/doc",
            "verdict": "ok"}


def test_render_tools_post_format_and_escape():
    import tools_digest
    result = tools_digest.select_tools(_prep(8), _fake_client(_TOOLS_RAW))
    post = tools_digest.render_tools_post(result)
    assert "<b>Инструменты недели</b>" in post
    assert "›" in post and "<a href=" in post
    assert "⚠️" in post  # limits непустые → пометка есть
    # эскейп
    result2 = dict(result)
    result2["items"] = [dict(result["items"][0], name="Скрипт <b>жирный</b>")]
    post2 = tools_digest.render_tools_post(result2)
    assert "<b>жирный</b>" not in post2 and "&lt;b&gt;жирный" in post2


def test_render_tools_malicious_url_cannot_inject():
    import tools_digest
    url = 'https://a.example"></a><a href="https://phish.example">'
    result = {"title": "T", "mode": "ai", "notes": "n",
              "items": [{"name": "H", "url": url,
                         "category": "Web", "platforms": "Web",
                         "free_tier": "free", "desc": "d", "limits": "",
                         "proof_url": "https://safe.example/doc",
                         "verdict": "ok"}]}
    post = tools_digest.render_tools_post(result)
    assert 'href="https://phish.example' not in post
    sources = tools_digest.render_tools_sources(result)
    assert 'href="https://phish.example' not in sources


def test_render_tools_insufficient_warning():
    import tools_digest
    result = {"title": "T", "mode": "ai", "notes": "n",
              "items": [_ok_item()]}  # ровно 1 проверенный
    post = tools_digest.render_tools_post(result)
    assert "недостаточно" in post


def test_render_tools_split_at_limit():
    """Long posts cut by _split_by_paragraphs like editor cards do."""
    import tools_digest
    items = [_ok_item(desc="x" * 4500, i=i) for i in range(5)]
    result = {"title": "T", "mode": "ai", "notes": "n", "items": items}
    msgs = tools_digest.build_tools_messages(result)
    assert len(msgs) >= 2
    assert all(len(m) <= 4096 for m in msgs)


# --- get_or_build_tools (bot integration pieces) ---

def _fake_article(url, i):
    return {"title": f"New Tool {i}", "url": url,
            "summary": "CLI утилита для автоответов бесплатно", "source": "Test",
            "published": None}


def test_get_or_build_tools_cache_and_seen():
    import tools_digest
    store: dict = {}
    client = _fake_client(_TOOLS_RAW)

    def fresh_cands(hours, **kw):
        # кандидаты включают url'ы из _TOOLS_RAW, чтобы AI-пункты прошли
        # provenance-проверку, плюс лишние, которые AI не взял
        base = [_fake_article("https://github.com/a/tool-0", 0),
                _fake_article("https://x.dev/app-1", 1)]
        return base + [_fake_article(f"https://new{i}.example/1", i)
                       for i in range(2, 6)]

    with patch.object(tools_digest, "collect_tools", side_effect=fresh_cands) as ct, \
         patch.object(tools_digest.news_pipeline, "extract_full_text", return_value=None), \
         patch.object(tools_digest.urllib.request, "urlopen", side_effect=OSError("no net")):
        msgs1 = tools_digest.get_or_build_tools(store, 168, client)
        assert ct.call_count == 1
        assert store["tools_cache"]["hours"] == 168
        # повторный вызов — то же окно → кэш, сбор не запускается второй раз
        msgs2 = tools_digest.get_or_build_tools(store, 168, client)
        assert ct.call_count == 1
        assert msgs1 == msgs2
        # другое окно → пересбор; уже показанные url исключены через tools_seen
        import news_pipeline
        ok_urls = {news_pipeline.normalize_url(it["url"])
                   for it in json.loads(_TOOLS_RAW)["items"]}
        assert store["tools_seen"] & ok_urls == ok_urls
    with patch.object(tools_digest, "collect_tools",
                      side_effect=lambda h, **kw: [_fake_article(u, i)
                                                   for i, u in enumerate(ok_urls)]):
        msgs3 = tools_digest.get_or_build_tools(store, 24, client)
    # все кандидаты уже показаны → явное сообщение, а не пустой пост
    assert msgs3 == ["Инструментов не нашлось — попробуй: /tools 720"]


# --- final-review fix pass (findings from the branch review) ---

def test_tool_score_matches_word_forms():
    """Stem keywords must match natural word forms ('утилита', 'бесплатная') —
    reviewer finding: _kw_matches with \\b...\\b never matched stems."""
    import tools_digest
    assert tools_digest._stem_matches("утилит", "бесплатная утилита для CLI") is True
    assert tools_digest._stem_matches("бесплатн", "бесплатный доступ") is True
    assert tools_digest._stem_matches("расширени", "расширение браузера") is True
    assert tools_digest._stem_matches("утилита", "кино вечер") is False
    assert tools_digest._tool_score({"title": "Tool", "summary": "бесплатная утилита"}) > 0


def test_select_tools_coerces_malformed_items():
    """Valid JSON with one item missing 'desc' and a non-dict item must not
    burn the run: items coerced to full string dicts, junk dropped."""
    import tools_digest
    raw = json.dumps({
        "title": "T",
        "items": [
            {"name": "A", "url": "https://github.com/a/tool-0",
             "category": "CLI", "verdict": "ok"},  # нет desc/platforms/...
            "junk string item",  # не dict — выбрасывается
        ]}, ensure_ascii=False)
    cands = _prep(8)
    result = tools_digest.select_tools(cands, _fake_client(raw))
    assert result["mode"] == "ai"
    item = result["items"][0]
    assert item["desc"] == "" and item["platforms"] == ""
    tools_digest.render_tools_post(result)  # не должен упасть
    tools_digest.build_tools_messages(result)


def test_select_tools_items_dict_falls_back():
    """items as dict (not list) → garbage → fallback, not AttributeError."""
    import tools_digest
    raw = json.dumps({"title": "T", "items": {"a": 1}}, ensure_ascii=False)
    result = tools_digest.select_tools(_prep(4), _fake_client(raw))
    assert result["mode"] == "fallback"


def test_select_tools_drops_fabricated_url():
    """AI item with a url that is not one of the candidates → dropped
    (anti-hallucination provenance check)."""
    import tools_digest
    raw = json.dumps({
        "title": "T",
        "items": [
            {"name": "Real", "url": "https://github.com/a/tool-0",
             "category": "CLI", "platforms": "Web", "free_tier": "open-source",
             "desc": "d", "limits": "", "proof_url": "", "why": "w",
             "verdict": "ok"},
            {"name": "Fake", "url": "https://fabricated.example/tool",
             "category": "Web", "platforms": "Web", "free_tier": "free",
             "desc": "d", "limits": "", "proof_url": "", "why": "",
             "verdict": "ok"},  # ссылки на кандидата нет — выкинуть
        ]}, ensure_ascii=False)
    result = tools_digest.select_tools(_prep(8), _fake_client(raw))
    names = [it["name"] for it in result["items"]]
    assert "Fake" not in names and "Real" in names


def test_render_tools_notes_has_item_status_and_free_tier():
    """Spec §3.4 block 3: per-item status, free tier, limits, why."""
    import tools_digest
    result = {"title": "T", "mode": "ai", "notes": "черновик",
              "items": [{"name": "A", "url": "https://a.example", "category": "CLI",
                         "platforms": "Web", "free_tier": "open-source",
                         "desc": "d", "limits": "Нужен Python",
                         "proof_url": "", "why": "можно применить сразу",
                         "verdict": "ok"}]}
    notes = tools_digest.render_tools_notes(result)
    assert "проверен" in notes
    assert "open-source" in notes
    assert "Нужен Python" in notes
    assert "можно применить сразу" in notes


def test_render_tools_post_kept_uncertain_reworded():
    """The warning no longer promises 'only checked will be shown' while the
    code renders every item — wording matches behavior."""
    import tools_digest
    uncertain = dict(_ok_item(), verdict="uncertain")
    result = {"title": "T", "mode": "ai", "notes": "n", "items": [uncertain]}
    post = tools_digest.render_tools_post(result)
    assert "недостаточно" in post
    assert "покажу только их" not in post