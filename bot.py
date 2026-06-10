#!/usr/bin/env python3
"""Telegram bot — on-demand AI news digests via /news2, /news24, /news7."""

import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

from monitor import fetch_articles, filter_by_keywords, analyze_with_ai, format_telegram_message

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Запрашивай новости когда нужно:\n\n"
        "/news2 — за последние 2 часа\n"
        "/news24 — за последние 24 часа\n"
        "/news7 — за последние 7 дней"
    )


async def _send_digest(update: Update, hours: int) -> None:
    period = {2: "2 часа", 24: "24 часа", 168: "7 дней"}[hours]
    msg = await update.message.reply_text(f"Собираю новости за {period}…")

    articles = fetch_articles(hours)
    relevant = filter_by_keywords(articles)

    if not relevant:
        await msg.edit_text(f"За последние {period} ничего релевантного не нашлось.")
        return

    analyzed, _ = analyze_with_ai(relevant[:15])
    text = format_telegram_message(analyzed, hours)
    await msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=False)


async def cmd_news2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_digest(update, 2)


async def cmd_news24(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_digest(update, 24)


async def cmd_news7(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_digest(update, 168)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("news2", cmd_news2))
    app.add_handler(CommandHandler("news24", cmd_news24))
    app.add_handler(CommandHandler("news7", cmd_news7))

    logging.info("Бот запущен, жду команды…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
