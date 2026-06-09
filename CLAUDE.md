# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

Автоматический мониторинг ИИ-новостей для контент-менеджера Telegram-канала **Zerocoder** (тематика: ИИ и вайбкодинг). Два режима работы:

- **`monitor.py`** — ежедневный дайджест в 9:00 МСК, отправляется в Telegram
- **`notifier.py`** — алерты каждые 2 часа: каждая горячая новость → готовый пост для канала

## Запуск

```bash
pip install -r requirements.txt
cp .env.example .env  # заполнить ключи

python3 monitor.py        # дайджест за последние 24ч
python3 monitor.py 48     # дайджест за 48ч
python3 notifier.py       # проверить новости за последние 2ч и отправить горячие
```

## Переменные окружения (`.env`)

| Переменная | Где взять |
|---|---|
| `GROQ_API_KEY` | console.groq.com (бесплатно) |
| `TELEGRAM_BOT_TOKEN` | @BotFather в Telegram |
| `TELEGRAM_CHAT_ID` | запустить `python3 get_chat_id.py` |

## Архитектура

Оба скрипта используют одну и ту же цепочку:

```
RSS-ленты → keyword filter → score_article() → AI (Groq) → Telegram
```

**Скоринг** (`score_article`) — чисто ключевые слова, без AI-запросов. Слова из `HIGH_VALUE` дают +2, остальные +1. Порог: `monitor.py` использует оценку AI (Groq), `notifier.py` отправляет статьи с оценкой ≥ 2.

**AI-анализ** — Groq API, модели:
- `notifier.py` → `meta-llama/llama-4-scout-17b-16e-instruct` (лучший русский язык)
- `monitor.py` → `llama-3.3-70b-versatile` (scoring + verdict для дайджеста)

**Фолбэк**: если `GROQ_API_KEY` не задан, оба скрипта работают только на keyword-скоринге без AI-описаний.

## Ключевые слова и тематика

Канал Zerocoder — аудитория: специалисты по **ИИ и вайбкодингу**. Термин «no-code» в текстах постов не используется — актуальная повестка канала: вайбкодинг.

При добавлении новых ключевых слов обновлять в **обоих файлах** (`monitor.py` и `notifier.py`) — списки `KEYWORDS` и `HIGH_VALUE` должны быть синхронизированы.

`HIGH_VALUE` — ключевые слова удвоенного веса: ChatGPT, Claude, GPT, LLM, Gemini, OpenAI, Anthropic, вайбкодинг, no-code и др.

## RSS-источники (10 лент)

Zerocoder, ZDNet, Forbes AI, TechCrunch AI, OpenAI Blog, HuggingFace Blog, DeepMind Blog, Karpathy (Substack), One Useful Thing (Ethan Mollick), DAIR AI (Medium).

## Генерация постов (`notifier.py`)

Стиль: тёплый, от первого лица («я нашла», «мне кажется»). Умеренный энтузиазм, не кричащий. No-code в постах не упоминать — только ИИ и вайбкодинг.

Концовка поста случайно выбирается из 4 стилей вовлечения (`_ENGAGEMENT_STYLES`):
- вопрос про последствия
- провокационный тезис
- личное сомнение + приглашение поспорить
- мягкий призыв поделиться опытом

Стиль «поделиться кейсом» подходит только для статей про конкретные инструменты, не для бизнес-новостей.

`clean_text()` убирает: иероглифы, польские диакритики, слова с перемешанной латиницей и кириллицей (`specialistам`), исправляет слипшиеся заглавные, правит написание брендов (`Open AI` → `OpenAI`).

## Автоматические рутины (claude.ai/code/routines)

| Рутина | ID | Расписание |
|---|---|---|
| AI News Digest — Daily | `trig_0146BWUC6AVE7F8trPuiY7pA` | 9:00 МСК ежедневно |
| AI News Notifier — Hourly | `trig_011j3PkWwJk4kdLH9kDtMyxd` | каждые 2 часа |

Рутины скачивают скрипты через `curl` из публичного GitHub-репо (не `git clone` — блокируется в облаке Anthropic). После изменений в коде рутины подхватывают новую версию автоматически при следующем запуске.

Если рутина отключилась с причиной `auto_disabled_repo_access` — это значит что в `sources` осталась ссылка на git-репо. Убрать через `RemoteTrigger update` с `"sources": []`.
