"""KEYWORDS/HIGH_VALUE must be identical in monitor.py and notifier.py
(CLAUDE.md sync rule). qwen/deepseek added for big model releases — and
matching must survive «Qwen4-Max» (digit after the keyword)."""

import monitor
import notifier


def test_keywords_lists_synced():
    assert monitor.KEYWORDS == notifier.KEYWORDS


def test_high_value_synced():
    assert monitor.HIGH_VALUE == notifier.HIGH_VALUE


def test_qwen_deepseek_present():
    for mod in (monitor, notifier):
        for kw in ("qwen", "deepseek"):
            assert any(str(k).lower() == kw for k in mod.KEYWORDS)
            assert kw in mod.HIGH_VALUE


def test_kw_matches_allows_digit_after_keyword():
    m = notifier._kw_matches
    # «Qwen4-Max» — цифра сразу после ключевого слова не рвёт совпадение
    assert m("qwen", "qwen4-max: новый флагман")
    assert m("qwen", "релиз qwen")
    assert m("gpt", "gpt-5 вышел")
    # буквенное продолжение по-прежнему не совпадает
    assert not m("qwen", "qwerty больше не про ии")
    assert not m("gpt", "gptx ничего не значит")
