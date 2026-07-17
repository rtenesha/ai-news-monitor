#!/usr/bin/env python3
"""Telegram bot — on-demand AI news posts via /news2, /news24, /news7,
plus /rewrite and /reply for ad-hoc community-manager text help."""

from __future__ import annotations

import os
import re
import logging
from datetime import datetime, timezone, timedelta
from groq import Groq
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

from monitor import fetch_articles, filter_by_keywords
from notifier import score_article, generate_post, _NATURAL_TEXT_RULES

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
MSK = timezone(timedelta(hours=3))

# label, lo знаков, hi знаков, задача этапа, пример нужного уровня краткости
_STAGES = {
    "анонс": ("основной анонс (5-7 дней)", 700, 1200,
              "расскажи что будет, для кого, какую проблему решает, почему стоит прийти",
              None),
    "напоминание": ("напоминание (2-3 дня)", 300, 700,
                     "НЕ пересказывай анонс — дай новую причину прийти: программу, кейс, бонус, результат",
                     None),
    "день1": ("за 1 день", 150, 400, "просто верни событие в поле зрения, коротко",
               "Уже завтра в 19:00 встречаемся на эфире про ИИ.\n"
               "Покажем реальные сценарии использования нейросетей в работе и ответим на вопросы участников.\n"
               "Если ещё не зарегистрировались — ссылка ниже 👇"),
    "день": ("в день мероприятия", 50, 200, "максимально коротко: время + ссылка",
              "Сегодня в 19:00 стартуем.\nПрисоединяйтесь по ссылке 👇"),
    "час": ("за 1 час до начала", 20, 80, "почти пуш-уведомление, например «Начинаем через час 🚀»",
             "Начинаем через час 🚀\nВстречаемся в 19:00. Ссылка ниже 👇"),
}

_MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _extract_event_date(text: str, today: datetime) -> datetime | None:
    m = re.search(r"(\d{1,2})\s+(" + "|".join(_MONTHS_RU) + r")(?:\s+(\d{4}))?", text, re.IGNORECASE)
    if not m:
        return None
    day = int(m.group(1))
    month = _MONTHS_RU[m.group(2).lower()]
    year = int(m.group(3)) if m.group(3) else today.year
    try:
        event = datetime(year, month, day, tzinfo=MSK)
    except ValueError:
        return None
    if event.date() < today.date() and not m.group(3):
        event = event.replace(year=year + 1)
    return event


def _extract_ad_disclaimer(text: str) -> str | None:
    """Pulls the mandatory ad-marking block (e.g. 'РЕКЛАМА ООО...' + ИНН) so it can be
    force-appended to every generated variant — the model doesn't reliably repeat it
    across 3 variants even when told to, and dropping it is a legal-compliance issue,
    not just a style nit."""
    m = re.search(r"(?im)^.*РЕКЛАМА.*(?:\n.*)*$", text)
    return m.group(0).strip() if m else None


def _ensure_disclaimer(body: str, disclaimer: str | None) -> str:
    if disclaimer and "реклама" not in body.lower():
        return f"{body}\n\n{disclaimer}"
    return body


def _extract_cta_label(text: str) -> str | None:
    """Finds the registration/CTA button label (e.g. 'ЗАРЕГИСТРИРОВАТЬСЯ', 'УЧАСТВОВАТЬ')
    that appears next to a link, so it can be force-appended if the model drops it —
    same rationale as _extract_ad_disclaimer: the model doesn't reliably keep it in
    every one of 3 variants even when told to."""
    m = re.search(r"(?i)ссылка[:\s]*[^\wа-яё]*([А-ЯЁ]{4,})", text)
    return m.group(1) if m else None


def _ensure_cta(body: str, cta_label: str | None) -> str:
    if cta_label and cta_label not in body:
        return f"{body}\n\nСсылка: {cta_label}"
    return body


def _suggested_tier(text: str) -> str | None:
    """Pure informational hint based on a date found in the text — doesn't restrict
    which variant gets generated, just flags which one is probably the right pick."""
    today = datetime.now(MSK)
    event = _extract_event_date(text, today)
    if not event:
        return None
    days = max(0, (event.date() - today.date()).days)
    if days >= 4:
        return "Длинный"
    if days >= 2:
        return "Покороче"
    return "Самый короткий"


def _parse_stage_override(text: str) -> tuple[str, int, int, str, str | None, str] | None:
    """Honors an explicit 'анонс:'/'напоминание:'/'день1:'/'день:'/'час:' prefix
    for when the default 3-variant output isn't granular enough (e.g. 'час')."""
    m = re.match(r"^\s*(анонс|напоминание|день1|день|час)\s*:\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    label, lo, hi, guidance, example = _STAGES[m.group(1).lower()]
    return label, lo, hi, guidance, example, m.group(2).strip()


def _groq_client() -> Groq | None:
    api_key = os.getenv("GROQ_API_KEY")
    return Groq(api_key=api_key) if api_key else None


_FORMATTING_RULES = """Формат текста — ОБЯЗАТЕЛЬНО, это не рекомендация:
- Первая строка — короткий цепляющий хук/заголовок (до 8-10 слов), с конкретным крючком, а не пересказ темы.
  Не «Открытый урок для родителей» (это тема, не хук), а что-то вроде «Родители часто не знают, куда...» —
  конкретная мысль или вопрос, который заставляет дочитать. Дальше — пустая строка.
- Дальше текст разбит на короткие абзацы: 1-3 предложения на абзац, между абзацами — ПУСТАЯ СТРОКА.
  Сплошной текст одним блоком без разбивки — брак, так делать нельзя, даже в самом коротком варианте."""


def _rewrite_single(client: Groq, text: str, label: str, lo: int, hi: int,
                     guidance: str, example: str | None) -> str:
    example_block = (
        f"\n\nПример нужного уровня краткости для этого этапа (не копируй, только ориентир на объём и стиль):\n«{example}»"
        if example else ""
    )
    prompt = (
        "Перепиши текст ниже под tone of voice Telegram-канала Zerocoder (тема: ИИ и вайбкодинг).\n\n"
        f"{_NATURAL_TEXT_RULES}\n\n"
        f"{_FORMATTING_RULES}\n\n"
        f"Этап анонса: «{label}». ЖЁСТКИЙ лимит длины — {lo}-{hi} знаков (без учёта рекламной маркировки и ссылок), "
        f"это главный критерий успеха, важнее чем сохранить побольше деталей. Задача этапа: {guidance}."
        f"{example_block}\n\n"
        f"Исходный текст ниже наверняка длиннее {hi} знаков — это нормально, он написан для другого этапа воронки. "
        "Не пытайся вместить в короткий формат все факты из него. Оставь только то, что нужно для задачи этого "
        "этапа (обычно: тема/суть, дата и время, ссылка на регистрацию), а буллеты, списки бонусов и подробные "
        "объяснения выбрасывай целиком — это ожидаемое сокращение по формату этапа, а не потеря фактов. "
        "Ничего не выдумывай и не добавляй от себя сверх исходного текста.\n\n"
        "ВАЖНО — сохрани БЕЗ ИЗМЕНЕНИЙ, дословно: рекламную маркировку (например «РЕКЛАМА ООО...», ИНН), "
        "текст на кнопках (например «ЗАРЕГИСТРИРОВАТЬСЯ», «УЧАСТВОВАТЬ») и все ссылки — их менять, "
        "сокращать или убирать нельзя, это юридическое и интерфейсное ограничение, а не часть текста для редактуры.\n\n"
        f"Перед ответом посчитай знаки в получившемся тексте (без маркировки и ссылок). Если вышло больше {hi} — "
        "это провал задачи, сократи ещё раз, вырезая содержание, а не подбирая более короткие слова.\n\n"
        f"Исходный текст:\n{text}"
    )
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": "Пиши исключительно на русском языке. Не смешивай латиницу и кириллицу в одном слове. Верни только переписанный текст, без пояснений и вступлений."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=700,
        temperature=0.6,
    )
    body = response.choices[0].message.content.strip()
    body = _ensure_cta(body, _extract_cta_label(text))
    body = _ensure_disclaimer(body, _extract_ad_disclaimer(text))
    return f"\U0001f4cf Этап: {label} ({lo}-{hi} знаков)\n\n{body}"


_TIER_MARKERS = ["ДЛИННЫЙ", "ПОКОРОЧЕ", "КОРОТКИЙ"]


def _rewrite_three_tiers(client: Groq, text: str, hint: str | None) -> str:
    tiers = [_STAGES["анонс"], _STAGES["напоминание"], _STAGES["день1"]]
    tier_desc = "\n".join(
        f"{marker}: целевая длина {lo}-{hi} знаков ({label}). Задача: {guidance}."
        + (f" Пример нужного уровня краткости (не копируй, только ориентир):\n«{example}»" if example else "")
        for marker, (label, lo, hi, guidance, example) in zip(_TIER_MARKERS, tiers)
    )
    prompt = (
        "Перепиши текст ниже под tone of voice Telegram-канала Zerocoder (тема: ИИ и вайбкодинг) "
        "в ТРЁХ вариантах длины — под разные этапы анонса.\n\n"
        f"{_NATURAL_TEXT_RULES}\n\n"
        f"{_FORMATTING_RULES}\n"
        "(Хук и разбивка на абзацы нужны в КАЖДОМ из трёх вариантов, включая самый короткий — "
        "у него просто будет 1-2 коротких абзаца вместо 3-4, но не сплошной текст.)\n\n"
        f"{tier_desc}\n\n"
        "КРИТИЧНО — это должны быть ТРИ РАЗНЫХ ЗАХОДА, а не один текст в разной длине. "
        "Частая ошибка — сделать ПОКОРОЧЕ просто сжатой копией ДЛИННОГО с тем же списком бонусов "
        "и тем же порядком мыслей. Так делать нельзя. У каждого варианта своя задача:\n"
        "- ДЛИННЫЙ: начни с КОНКРЕТНОЙ боли или вопроса, который волнует адресата — не абстракция "
        "(«профессии будущего», «важные изменения»), а что-то конкретное и узнаваемое из его жизни. "
        "Дай полную картину: что будет, для кого, какую проблему решает, почему стоит прийти именно сейчас.\n"
        "- ПОКОРОЧЕ: это НЕ сокращение длинного. Веди с НОВЫМ углом, но ТОЛЬКО из фактов, которые уже "
        "есть в исходном тексте — возьми бонус, деталь программы или цифру, которая в длинном варианте "
        "была не на первом месте, и выведи её в начало как главную причину. НЕЛЬЗЯ придумывать примеры, "
        "истории, кейсы или цитаты людей, которых нет в исходном тексте, — это будет ложь читателю, а не "
        "смена угла. Открывающая фраза должна отличаться от ДЛИННОГО по смыслу, а не только по длине.\n"
        "- КОРОТКИЙ: без аргументации заново — тема, дата/время, ссылка. Просто напоминание, не продажа.\n\n"
        "Для более коротких вариантов не пытайся втиснуть все факты из исходника — оставляй только "
        "самое важное для задачи этапа, а буллеты, списки бонусов и подробные объяснения выбрасывай "
        "целиком или используй только один самый сильный пункт. Это ожидаемое сокращение по формату "
        "этапа, а не потеря фактов. Ничего не выдумывай и не добавляй от себя.\n\n"
        "ЗАПРЕЩЕНО во всех трёх вариантах: «Регистрация обязательна!», «приглашает вас», "
        "«не упустите возможность» и любые похожие канцелярские фразы-паразиты объявлений — "
        "кнопка и так говорит, что нужно сделать, дублировать это текстом не нужно.\n\n"
        "ВАЖНО — сохрани БЕЗ ИЗМЕНЕНИЙ, дословно, во ВСЕХ трёх вариантах: рекламную маркировку "
        "(например «РЕКЛАМА ООО...», ИНН), текст на кнопках (например «ЗАРЕГИСТРИРОВАТЬСЯ», «УЧАСТВОВАТЬ») "
        "и все ссылки — их менять, сокращать или убирать нельзя.\n\n"
        "Перед ответом посчитай знаки в каждом варианте (без маркировки и ссылок) — если вариант вышел длиннее "
        "целевого диапазона, это провал, сократи ещё раз, вырезая содержание.\n\n"
        "Ответ дай СТРОГО в этом формате, с этими маркерами в начале строки:\n"
        "ДЛИННЫЙ: <текст>\n"
        "ПОКОРОЧЕ: <текст>\n"
        "КОРОТКИЙ: <текст>\n\n"
        f"Исходный текст:\n{text}"
    )
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": "Пиши исключительно на русском языке. Не смешивай латиницу и кириллицу в одном слове. Строго следуй формату с маркерами ДЛИННЫЙ:, ПОКОРОЧЕ:, КОРОТКИЙ:, без лишних пояснений."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=1400,
        temperature=0.6,
    )
    raw = response.choices[0].message.content.strip()

    pattern = "|".join(_TIER_MARKERS)
    matches = list(re.finditer(rf"(?:{pattern}):", raw))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        key = m.group(0)[:-1]
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections[key] = raw[start:end].strip()

    if not all(k in sections for k in _TIER_MARKERS):
        return raw  # parsing failed — better to show the raw model output than nothing

    cta_label = _extract_cta_label(text)
    disclaimer = _extract_ad_disclaimer(text)
    for key in _TIER_MARKERS:
        sections[key] = _ensure_cta(sections[key], cta_label)
        sections[key] = _ensure_disclaimer(sections[key], disclaimer)

    titles = {
        "ДЛИННЫЙ": f"Длинный ({tiers[0][1]}-{tiers[0][2]} знаков, {tiers[0][0]})",
        "ПОКОРОЧЕ": f"Покороче ({tiers[1][1]}-{tiers[1][2]} знаков, {tiers[1][0]})",
        "КОРОТКИЙ": f"Самый короткий ({tiers[2][1]}-{tiers[2][2]} знаков, {tiers[2][0]})",
    }
    blocks = [f"\U0001f4cf {titles[m]}\n\n{sections[m]}" for m in _TIER_MARKERS]
    header = f"(Судя по дате в тексте, вероятно подходит «{hint}» — но выбирай сама.)\n\n" if hint else ""
    return header + ("\n\n" + "─" * 20 + "\n\n").join(blocks)


def rewrite_text(text: str) -> str:
    """Rewrite text in the Zerocoder tone of voice. By default returns 3 length
    variants (длинный/покороче/самый короткий) so the caller can just pick one.
    An explicit 'анонс:'/'напоминание:'/'день1:'/'день:'/'час:' prefix locks it
    to a single exact stage instead (useful for 'день'/'час', which the 3-tier
    default doesn't cover)."""
    client = _groq_client()
    if not client:
        return "GROQ_API_KEY не задан — не могу переписать текст."

    override = _parse_stage_override(text)
    try:
        if override:
            label, lo, hi, guidance, example, body_text = override
            return _rewrite_single(client, body_text, label, lo, hi, guidance, example)
        hint = _suggested_tier(text)
        return _rewrite_three_tiers(client, text, hint)
    except Exception as e:
        return f"Не получилось переписать текст: {e}"


def draft_reply(text: str) -> str:
    """Draft a community-manager style reply to a subscriber comment or question."""
    client = _groq_client()
    if not client:
        return "GROQ_API_KEY не задан — не могу написать ответ."

    prompt = (
        "Ты — комьюнити-менеджер Telegram-канала Zerocoder (тема: ИИ и вайбкодинг). "
        "Ниже — комментарий или вопрос от подписчика. Напиши короткий живой ответ от первого лица, "
        "как будто отвечаешь ему в комментариях под постом.\n\n"
        f"{_NATURAL_TEXT_RULES}\n\n"
        "Обращение — строго на «Вы» (с большой буквы), никогда не «ты». Без разговорных форм и сленга "
        "(«короче», «го», «шарить», «жиза», молодёжный интернет-сленг и т.п.) — простая грамотная речь, "
        "живая, но не панибратская.\n\n"
        "Тон: дружелюбный, но не подобострастный, по существу, 2-4 предложения. "
        "Если в комментарии есть вопрос — ответь на него конкретно, не отделывайся общими словами.\n\n"
        f"Комментарий подписчика:\n{text}"
    )
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Пиши исключительно на русском языке. Не смешивай латиницу и кириллицу в одном слове. Обращайся к собеседнику только на «Вы» с большой буквы, без разговорных форм и сленга. Верни только текст ответа, без пояснений и вступлений."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Не получилось написать ответ: {e}"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Запрашивай свежие новости когда нужно:\n\n"
        "/news2 — горячее за последние 2 часа\n"
        "/news24 — лучшее за сутки\n"
        "/news7 — главное за неделю\n\n"
        "А ещё я умею работать с текстом:\n"
        "/rewrite <текст> — переписать под tone of voice канала. Даю сразу 3 варианта длины: "
        "длинный, покороче, самый короткий — выбирай, какой подходит под момент публикации. "
        "Если нужен конкретный этап («день» или «час» — их 3 варианта не покрывают), "
        "укажи явно: «/rewrite анонс: ...», «напоминание: ...», «день1: ...», «день: ...», «час: ...»\n"
        "/reply <текст> — набросать ответ комьюнити-менеджера на комментарий\n\n"
        "Можно и просто ответить (reply) на сообщение с текстом этими командами вместо того чтобы копировать текст."
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


def _extract_target_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    if context.args:
        return " ".join(context.args)
    if update.message.reply_to_message and update.message.reply_to_message.text:
        return update.message.reply_to_message.text
    return None


async def cmd_rewrite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _extract_target_text(update, context)
    if not text:
        await update.message.reply_text(
            "Пришли текст: /rewrite <текст>, или ответь этой командой на сообщение с текстом."
        )
        return
    status = await update.message.reply_text("Переписываю…")
    result = rewrite_text(text)
    await status.edit_text(result)


async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _extract_target_text(update, context)
    if not text:
        await update.message.reply_text(
            "Пришли комментарий: /reply <текст>, или ответь этой командой на сообщение с текстом."
        )
        return
    status = await update.message.reply_text("Пишу ответ…")
    result = draft_reply(text)
    await status.edit_text(result)


async def on_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Не поняла, что сделать с текстом. Ответь на это сообщение командой:\n"
        "/rewrite — переписать под tone of voice канала\n"
        "/reply — набросать ответ комьюнити-менеджера"
    )


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("news2",   "🔥 Горячее за последние 2 часа"),
        BotCommand("news24",  "📰 Лучшее за сутки"),
        BotCommand("news7",   "📅 Главное за неделю"),
        BotCommand("rewrite", "✍️ Переписать текст под tone of voice"),
        BotCommand("reply",   "💬 Ответ комьюнити-менеджера на комментарий"),
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
    app.add_handler(CommandHandler("rewrite", cmd_rewrite))
    app.add_handler(CommandHandler("reply", cmd_reply))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_plain_text))

    logging.info("Бот запущен, жду команды…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
