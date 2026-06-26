#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ИИ Сейчас — автопостер для Telegram-канала @ai_seychas (на Google Gemini, бесплатно).

Что делает за один запуск (конвейер из 4 этапов):
  1) СБОР     — тянет свежие новости про ИИ, агентов и нейросети из нескольких RSS-источников.
  2) ОТБОР    — отсеивает уже опубликованное и старое, ранжирует, берёт топ-N.
  3) НАПИСАНИЕ — пишет готовый пост в живом, доступном стиле (через Google Gemini API).
  4) ФАКТЧЕК  — пишет строго по тексту источника, добавляет ссылку на оригинал,
                не выдумывает фактов; числа/даты/названия берутся из источника.

Запускается в облаке GitHub Actions 3 раза в день (см. .github/workflows/post.yml),
поэтому работает даже когда твой компьютер выключен.

Переменные окружения (задаются как Secrets/Variables в GitHub):
  TELEGRAM_BOT_TOKEN   — токен бота от @BotFather (обязательно, Secret)
  TELEGRAM_CHANNEL     — @ai_seychas или числовой chat_id канала (обязательно, Secret)
  GEMINI_API_KEY       — ключ Google AI Studio (обязательно, Secret)
  GEMINI_MODEL         — модель, по умолчанию gemini-2.0-flash
  POSTS_PER_RUN        — сколько постов за запуск, по умолчанию 1
  TIMEZONE_OFFSET      — смещение часов от UTC для подписи утро/день/вечер, по умолчанию 3 (МСК)
  DRY_RUN              — "1" => ничего не отправлять, только напечатать (для теста)
"""

import os
import re
import sys
import json
import html
import time
import datetime as dt
from urllib.parse import urlparse

import requests
import feedparser

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:
    HAVE_BS4 = False

# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip()
POSTS_PER_RUN = int(os.environ.get("POSTS_PER_RUN", "1"))
TZ_OFFSET = int(os.environ.get("TIMEZONE_OFFSET", "3"))
DRY_RUN = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")

STATE_FILE = "posted.json"
FEEDS_FILE = "feeds.txt"
MAX_AGE_HOURS = 36          # насколько старые новости ещё считаем «свежими»
MAX_STATE = 800             # сколько последних ссылок храним, чтобы не повторяться

# RSS-источники по умолчанию (можно переопределить файлом feeds.txt — по одному URL в строке)
DEFAULT_FEEDS = [
    "https://news.google.com/rss/search?q=%22artificial%20intelligence%22%20OR%20%22AI%20model%22%20when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=%22AI%20agents%22%20OR%20LLM%20OR%20%22language%20model%22%20OR%20OpenAI%20OR%20Anthropic%20when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.artificialintelligence-news.com/feed/",
]

# «Горячие» термины — для приоритета при отборе
HOT_TERMS = [
    "openai", "anthropic", "claude", "gpt", "gemini", "google", "deepmind",
    "llama", "meta", "mistral", "qwen", "deepseek", "release", "launch",
    "agent", "model", "nvidia", "reasoning", "benchmark", "microsoft", "xai", "grok",
]


def log(msg):
    print(f"[ai-seychas] {msg}", flush=True)


def load_feeds():
    if os.path.exists(FEEDS_FILE):
        urls = []
        with open(FEEDS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        if urls:
            return urls
    return DEFAULT_FEEDS


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("posted", []))
        except Exception:
            return set()
    return set()


def save_state(posted_set):
    posted = list(posted_set)[-MAX_STATE:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"posted": posted}, f, ensure_ascii=False, indent=2)


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    return None


def clean_text(raw):
    if not raw:
        return ""
    if HAVE_BS4:
        raw = BeautifulSoup(raw, "html.parser").get_text(" ")
    else:
        raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


def source_name(url):
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def collect_candidates(feeds, seen):
    now = dt.datetime.now(dt.timezone.utc)
    items = {}
    for feed_url in feeds:
        try:
            log(f"Читаю фид: {feed_url}")
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            log(f"  ошибка фида: {e}")
            continue
        for entry in parsed.entries:
            link = entry.get("link", "").strip()
            title = clean_text(entry.get("title", ""))
            if not link or not title:
                continue
            if link in seen or link in items:
                continue
            ts = entry_time(entry)
            if ts is None:
                ts = now  # нет даты — считаем свежим
            age_h = (now - ts).total_seconds() / 3600.0
            if age_h > MAX_AGE_HOURS:
                continue
            summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
            items[link] = {
                "title": title,
                "link": link,
                "summary": summary[:1200],
                "ts": ts,
                "age_h": age_h,
                "source": source_name(link),
            }
    return list(items.values())


def score(item):
    text = (item["title"] + " " + item["summary"]).lower()
    hot = sum(1 for term in HOT_TERMS if term in text)
    recency = max(0.0, MAX_AGE_HOURS - item["age_h"]) / MAX_AGE_HOURS
    return hot * 2 + recency


def fetch_article_text(url):
    """Пробуем достать текст статьи для большего контекста. Не критично — есть фолбэк."""
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; AISeychasBot/1.0)"})
        if r.status_code != 200 or not r.text:
            return ""
        if HAVE_BS4:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.extract()
            paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
            text = " ".join(p for p in paras if len(p) > 40)
            return re.sub(r"\s+", " ", text).strip()[:4000]
    except Exception as e:
        log(f"  не смог открыть статью: {e}")
    return ""


def time_label():
    local_hour = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)).hour
    if 5 <= local_hour < 12:
        return "утро (бодрый тон, что важного за ночь)"
    if 12 <= local_hour < 18:
        return "день (деловой дайджест)"
    return "вечер (спокойный разбор главного за день)"


SYSTEM_PROMPT = (
    "Ты — редактор Telegram-канала «ИИ Сейчас» (@ai_seychas) про искусственный интеллект, "
    "нейросети и ИИ-агентов для широкой аудитории. Пишешь на русском.\n"
    "СТИЛЬ:\n"
    "- Живой, доступный язык. Любой жаргон (инференс, дистилляция, токены, агент, бенчмарк) "
    "объясняй простыми словами прямо в тексте.\n"
    "- Цепляющая первая строка-хук.\n"
    "- Обращение к читателю на «ты».\n"
    "- Длина 500–900 знаков.\n"
    "- Эмодзи умеренно: 1–3 на пост.\n"
    "- В конце — короткий вопрос или мысль для вовлечения и 2–3 хэштега.\n"
    "- БЕЗ markdown-звёздочек и решёток-заголовков. Только чистый текст, эмодзи и переносы строк.\n"
    "ФАКТЧЕК (строго):\n"
    "- Пиши ТОЛЬКО по предоставленному тексту источника. Не добавляй фактов, которых там нет.\n"
    "- Числа, даты, имена и названия бери точно как в источнике.\n"
    "- Ничего не выдумывай. Если данных мало — напиши короче, без домыслов.\n"
    "- Если что-то в источнике подано как слух/неофициально — сохрани эту оговорку.\n"
    "Верни ТОЛЬКО текст поста, без пояснений."
)


def write_post(item, article_text, tlabel):
    context = article_text if len(article_text) > len(item["summary"]) else item["summary"]
    user_msg = (
        f"Время суток: {tlabel}\n"
        f"Заголовок новости: {item['title']}\n"
        f"Источник: {item['source']}\n"
        f"Текст источника (опирайся только на него):\n{context}\n\n"
        f"Напиши пост для канала по правилам из системного сообщения."
    )
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 800},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini API error {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        raise RuntimeError(f"Неожиданный ответ Gemini: {json.dumps(data)[:300]}")
    # на всякий случай убираем markdown-звёздочки, если модель их вставила
    return text.replace("**", "").replace("__", "")


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": CHANNEL, "text": text[:4096], "disable_web_page_preview": False},
        timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")
    return data


def main():
    missing = [n for n, v in [
        ("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
        ("TELEGRAM_CHANNEL", CHANNEL),
        ("GEMINI_API_KEY", GEMINI_API_KEY),
    ] if not v]
    if missing:
        log(f"НЕ заданы обязательные секреты: {', '.join(missing)}")
        sys.exit(1)

    feeds = load_feeds()
    seen = load_state()
    log(f"Уже опубликовано ранее: {len(seen)} ссылок")

    candidates = collect_candidates(feeds, seen)
    log(f"Свежих кандидатов: {len(candidates)}")
    if not candidates:
        log("Нечего постить — свежих новостей не нашлось. Выходим без ошибки.")
        return

    candidates.sort(key=score, reverse=True)
    chosen = candidates[:POSTS_PER_RUN]
    tlabel = time_label()
    log(f"Время суток: {tlabel}. Готовлю постов: {len(chosen)}")

    posted_now = 0
    for item in chosen:
        try:
            article = fetch_article_text(item["link"])
            post = write_post(item, article, tlabel)
            post_full = f"{post}\n\nИсточник: {item['link']}"
            log("----- ПОСТ -----")
            log(post_full)
            if DRY_RUN:
                log("(DRY_RUN: не отправляю)")
            else:
                send_telegram(post_full)
                log("Отправлено в Telegram ✅")
            seen.add(item["link"])
            posted_now += 1
            time.sleep(2)
        except Exception as e:
            log(f"Ошибка на посте «{item['title'][:60]}»: {e}")

    save_state(seen)
    log(f"Готово. Опубликовано в этот запуск: {posted_now}")


if __name__ == "__main__":
    main()
