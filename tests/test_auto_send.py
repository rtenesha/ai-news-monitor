"""Автоотправка в Telegram — рубильник TELEGRAM_AUTO_SEND (по умолчанию выкл).
Проверяет, что send_alert молчит при выключенной отправке и шлёт при включённой."""

import time
from unittest.mock import patch

import news_pipeline


def test_auto_send_disabled_by_default_and_send_alert_silent(monkeypatch):
    monkeypatch.delenv("TELEGRAM_AUTO_SEND", raising=False)
    assert news_pipeline.auto_send_enabled() is False
    with patch("urllib.request.urlopen", side_effect=AssertionError("нельзя")):
        assert news_pipeline.send_alert("тест") is False


def test_auto_send_enabled_by_env_and_send_alert_sends(monkeypatch):
    monkeypatch.setenv("TELEGRAM_AUTO_SEND", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test")
    assert news_pipeline.auto_send_enabled() is True

    calls = []
    def fake_urlopen(req, timeout=10):
        calls.append(req)
    with patch("urllib.request.urlopen", fake_urlopen):
        assert news_pipeline.send_alert("тест") is True
    assert len(calls) == 1
