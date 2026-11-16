"""Handler errors must reach the user instead of dying silently
(PTB default 'No error handlers are registered' logging)."""

import types

import bot


def test_register_handlers_wires_error_handler():
    handlers, errors = [], []
    fake = types.SimpleNamespace(
        add_handler=lambda h: handlers.append(h),
        add_error_handler=lambda h: errors.append(h))
    bot._register_handlers(fake)
    assert len(handlers) >= 12          # все команды на месте
    assert errors == [bot._on_error]    # и обработчик ошибок зарегистрирован
