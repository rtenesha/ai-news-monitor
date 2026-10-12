"""Handler-level tests for bot.py /digest and /card (fix-pass verification)."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import editorial


def _topic_at(url):
    import tests.test_editorial as te
    topic = te._topic()
    topic["article"] = te._article(url=url)
    return topic


def _make_update_ctx(args):
    status = Mock()
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()
    update = Mock()
    update.message = Mock()
    update.message.reply_text = AsyncMock(return_value=status)
    update.message.reply_html = AsyncMock()
    ctx = Mock()
    ctx.args = args
    ctx.bot_data = {}
    return update, ctx


def test_cmd_digest_stores_filtered_list_for_card_numbering():
    import bot
    seen_url = "https://a.example/1"
    topics = [_topic_at(seen_url), _topic_at("https://a.example/2")]
    update, ctx = _make_update_ctx([])

    with patch.object(bot.editorial, "collect_topics", return_value=[]), \
         patch.object(bot.editorial, "select_topics", return_value=(topics, True)), \
         patch.object(bot, "_groq_client", return_value=None):
        ctx.bot_data["editorial_seen_cards"] = {seen_url}
        asyncio.run(bot.cmd_digest(update, ctx))

        # храним ИМЕННО отфильтрованный список — нумерация /card совпадает с отрисовкой
        stored = ctx.bot_data["editorial_topics"]
        assert len(stored) == 1
        assert stored[0]["article"]["url"] == "https://a.example/2"


def test_cmd_card_one_resolves_same_topic_as_rendered():
    import bot
    topics = [_topic_at("https://a.example/1"), _topic_at("https://a.example/2")]
    update, ctx = _make_update_ctx(["1"])
    ctx.bot_data = {"editorial_topics": topics}

    built = {}
    def fake_get_or_build(topic, client):
        built["t"] = topic
        from tests.test_editorial import _CARD_RAW, _fake_client
        return editorial.build_card(topic, _fake_client(_CARD_RAW))
    with patch.object(editorial, "get_or_build_card", side_effect=fake_get_or_build), \
         patch.object(bot, "_groq_client", return_value=None):
        asyncio.run(bot.cmd_card(update, ctx))
    assert built["t"]["article"]["url"] == "https://a.example/1"


def test_cmd_card_reply_html_failure_falls_back_to_plain_text():
    import bot
    topics = [_topic_at("https://a.example/1")]
    update, ctx = _make_update_ctx(["1"])
    ctx.bot_data = {"editorial_topics": topics}
    update.message.reply_html = AsyncMock(side_effect=RuntimeError("BadRequest: too long"))

    def fake_get_or_build(topic, client):
        from tests.test_editorial import _CARD_RAW, _fake_client
        return editorial.build_card(topic, _fake_client(_CARD_RAW))
    with patch.object(editorial, "get_or_build_card", side_effect=fake_get_or_build), \
         patch.object(bot, "_groq_client", return_value=None):
        asyncio.run(bot.cmd_card(update, ctx))

    assert update.message.reply_text.await_count >= 1  # plain-text фолбэк состоялся
