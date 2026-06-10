#!/usr/bin/env python3
"""Telegram bot — on-demand AI news posts via /news2, /news24, /news7."""

import os
import logging
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

from monitor import fetch_articles, filter_by_keywords
from notifier import score_article, generate_post

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Запрашивай свежие новости когда нужно:\n\n"
        "/news2 — горячее за последние 2 часа\n"
        "/news24 — лучшее за сутки\n"
        "/news7 — главное за неделю"
    )


async def _send_posts(update: Update, hours: int) -> None:
    period = {2: "2 часа", 24: "24 часа", 168: "7 дней"}[hours]
    # Лимит на количество постов чтобы не спамить
    limit = {2: 5, 24: 7, 168: 10}[hours]

    status = await update.message.reply_text(f"Ищу новости за {period}…")

    articles = fetch_articles(hours)
    relevant = filter_by_keywords(articles)
    hot = sorted(
        [a for a in relevant if score_article(a) >= 2],
        key=lambda a: score_article(a),
        reverse=True,
    )[:limit]

    if not hot:
        await status.edit_text(f"За последние {period} ничего горячего не нашлось.")
        return

    await status.edit_text(f"Нашла {len(hot)} материалов, генерирую посты…")

    for article in hot:
        post = generate_post(article)
        await update.message.reply_html(post, disable_web_page_preview=False)

    await status.delete()


async def cmd_news2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_posts(update, 2)


async def cmd_news24(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_posts(update, 24)


async def cmd_news7(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_posts(update, 168)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("news2",  "🔥 Горячее за последние 2 часа"),
        BotCommand("news24", "📰 Лучшее за сутки"),
        BotCommand("news7",  "📅 Главное за неделю"),
    ])


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("news2", cmd_news2))
    app.add_handler(CommandHandler("news24", cmd_news24))
    app.add_handler(CommandHandler("news7", cmd_news7))

    logging.info("Бот запущен, жду команды…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
