"""Tests for /scout bot wiring: pure arg-resolution rules in scout.py,
so no Telegram machinery is needed."""

import scout


def test_resolve_scout_command_rules():
    # нет аргумента — дефолт
    assert scout.resolve_scout_command([], 5) == {"mode": "hours", "value": 48}
    # есть список, номер валиден — карточка
    assert scout.resolve_scout_command(["3"], 5) == {"mode": "card", "n": 3}
    # списка нет — число трактуется как окно
    assert scout.resolve_scout_command(["3"], 0) == {"mode": "hours", "value": 3}
    # больше максимума списка — окно
    assert scout.resolve_scout_command(["72"], 5) == {"mode": "hours", "value": 72}
    # кламп вниз до минимума
    assert scout.resolve_scout_command(["1"], 0) == {"mode": "hours", "value": 2}
    # не число — дефолт
    assert scout.resolve_scout_command(["abc"], 5) == {"mode": "hours", "value": 48}
    # 11 > n_topics=10 — окно, не номер
    assert scout.resolve_scout_command(["11"], 10) == {"mode": "hours", "value": 11}
    # кламп вверх до максимума
    assert scout.resolve_scout_command(["500"], 0) == {"mode": "hours", "value": 168}