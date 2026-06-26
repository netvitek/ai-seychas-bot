#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ИИ Сейчас — автопостер для Telegram-канала @ai_seychas (Google Gemini + картинка, бесплатно).
"""

import os
import re
import sys
import json
import html
import time
import random
import datetime as dt
from urllib.parse import urlparse, quote

import requests
import feedparser

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:
    HAVE_BS4 = False

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
POSTS_PER_RUN = int(os.environ.get("POSTS_PER_RUN", "1"))
TZ_OFFSET = int(os.environ.get("TIMEZONE_OFFSET", "3"))
IMAGES_ENABLED = os.environ.get("IMAGES_ENABLED", "1").strip() not in ("0", "false", "False", "no")
DRY_RUN = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")

STATE_FILE = "posted.json"
FEEDS_FILE = "feeds.txt"
MAX_AGE_HOURS = 24
MAX_STATE = 800
TG_CAPTION_LIMIT = 1024

DEFAULT_FEEDS = [
    "https://news.google.com/rss/search?q=%22artificial%20intelligence%22%20OR%20%22AI%20model%22%20when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=%22AI%20agents%22%20OR%20LLM%20OR%20OpenAI%20OR%20Anthropic%20when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.artificialintelligence-news.com/feed/",
]

HOT_TERMS = [
    "openai", "anthropic", "claude", "gpt", "gemini", "google", "deepmind",
    "llama", "meta", "mistral", "qwen", "deepseek", "release", "launch",
    "agent", "model", "nvidia", "reasoning", "benchmark", "microsoft", "xai", "grok",
]

POLITICAL_TERMS = [
    "trump", "biden", "harris", "election", "president", "senate", "congress",
    "white house", "putin", "zelensky", "ukraine", " war ", "warfare", "sanction",
    "republican", "democrat", "parliament", "minister", "kremlin", "geopolit",
    "immigration", "protest", "political", "politician", "campaign trail",
]


def is_political(item):
    text = (item["title"] + " " + item["summary"]).lower()
    return any(t in text for t in POLITICAL_TERMS)


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
                return set(json.load(f).get("posted", []))
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
            if not link or not title or link in seen or link in items:
                continue
            ts = entry_time(entry) or now
            age_h = (now - ts).total_seconds() / 3600.0
            if age_h > MAX_AGE_HOURS:
                continue
            summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
            items[link] = {
                "title": title, "link": link, "summary": summary[:1200],
                "ts": ts, "age_h": age_h, "source": source_name(link),
            }
    return list(items.values())


def score(item):
    text = (item["title"] + " " + item["summary"]).lower()
    hot = sum(1 for term in HOT_TERMS if term in text)
    recency = max(0.0, MAX_AGE_HOURS - item["age_h"]) / MAX_AGE_HOURS
    return hot + recency * 3


def fetch_article_text(url):
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; AISeychasBot/1.0)"})
        if r.status_code != 200 or not r.text:
            return ""
        if HAVE_BS4:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.extract()
            paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
            return re.sub(r"\s+", " ", " ".join(p for p in paras if len(p) > 40)).strip()[:4000]
    except Exception as e:
        log(f"  не смог открыть статью: {e}")
    return ""


def time_label():
    local_hour = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)).hour
    if 5 <= local_hour < 12:
        return "утро — человек только проснулся, тон бодрый и заряжающий, кратко что важного"
    if 12 <= local_hour < 18:
        return "день — деловой энергичный дайджест, по сути"
    return "вечер — спокойный, но захватывающий разбор главного за день"


def gemini_generate(system_prompt, user_msg, max_tokens=800, temperature=0.8):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if system_prompt:
        payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini API error {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        raise RuntimeError(f"Неожиданный ответ Gemini: {json.dumps(data)[:300]}")


SYSTEM_PROMPT = (
    "Ты — редактор Telegram-канала «ИИ Сейчас» (@ai_seychas) про искусственный интеллект, "
    "нейросети и ИИ-агентов для широкой аудитории. Пишешь на русском.\n"
    "ОЧЕНЬ ВАЖНО: твой ответ — это ИСКЛЮЧИТЕЛЬНО готовый текст поста на РУССКОМ языке. "
    "Запрещены любые комментарии, пояснения, рассуждения, редакторские правки и любой текст "
    "на английском. Никаких «here is», «is better». Сразу выдай сам пост и больше ничего.\n"
    "ЦЕЛЬ: пост, который человек захочет прочитать ДО КОНЦА и оставить комментарий.\n"
    "ПОДАЧА: пиши как новость — сначала суть (что произошло и у кого), потом почему это важно и "
    "что меняется. НЕ оформляй как цитату, НЕ начинай с кавычек. Это новостной пост про технологии и гаджеты.\n"
    "СТИЛЬ:\n"
    "- Мощная первая строка-хук, которая цепляет с первой секунды.\n"
    "- Живой доступный язык; жаргон объясняй простыми словами.\n"
    "- Обращение на «ты». Энергично, увлекательно, без кликбейта-вранья.\n"
    "- Длина 500–900 знаков. Эмодзи 1–3.\n"
    "- В конце — цепляющий вопрос для комментариев.\n"
    "- БЕЗ хэштегов. БЕЗ ссылок и слова «Источник». БЕЗ markdown-звёздочек и решёток.\n"
    "- НЕ банально: конкретика и цифры, неожиданный угол, почему это важно ЛИЧНО читателю. "
    "Избегай дежурных фраз («открывает новые горизонты», «меняет всё»). Первая строка должна удивлять.\n"
    "ФАКТЫ: только по тексту источника, не выдумывай; числа, даты, имена — точно как в источнике; слухи помечай."
)


def looks_broken(text):
    if len(text) < 180:
        return True
    latin = sum(1 for c in text if "a" <= c.lower() <= "z")
    cyr = sum(1 for c in text if "а" <= c.lower() <= "я")
    if cyr < 80 or latin > cyr:
        return True
    low = text.lower()
    bad = ["is better", "in the source", "is a bit", "i would", "as an ai",
           "here is", "here's", "sure,", "i cannot", "i can't"]
    return any(b in low for b in bad)


IMG_SYSTEM = (
    "You are an art director for a tech news channel. Given a news item about AI, write ONE "
    "vivid English prompt for a striking cover illustration that visually represents THIS "
    "specific news (the product, company theme, robot, chip, gadget or concept involved). "
    "Make it concrete and eye-catching, not a generic blue brain. Rules: purely visual; "
    "modern cinematic digital editorial illustration / concept art; NO text, NO words, NO "
    "letters, NO logos, NO watermarks, NO UI; one sentence, max 45 words. Return ONLY the prompt."
)


def write_post(item, article_text, tlabel):
    context = article_text if len(article_text) > len(item["summary"]) else item["summary"]
    user_msg = (
        f"Время суток: {tlabel}\n"
        f"Заголовок новости: {item['title']}\n"
        f"Источник: {item['source']}\n"
        f"Текст источника (опирайся только на него):\n{context}\n\n"
        f"Напиши пост по правилам системного сообщения."
    )
    for attempt in range(2):
        temp = 0.75 if attempt == 0 else 0.4
        text = gemini_generate(SYSTEM_PROMPT, user_msg, max_tokens=800, temperature=temp)
        text = text.replace("**", "").replace("__", "").strip()
        if not looks_broken(text):
            return text
        log(f"  пост бракованный (попытка {attempt + 1}), пробую снова")
    raise RuntimeError("Не удалось получить нормальный пост — пропускаю, чтобы не публиковать брак.")


def make_image_url(item, post_text):
    try:
        prompt = gemini_generate(
            IMG_SYSTEM,
            f"News title: {item['title']}\nTopic context: {item['summary'][:600]}\nWrite the illustration prompt.",
            max_tokens=120, temperature=0.7,
        )
    except Exception as e:
        log(f"  не смог сгенерировать промпт картинки: {e}")
        prompt = "modern digital illustration about artificial intelligence and neural networks, glowing circuits, concept art"
    prompt = prompt.replace("\n", " ").strip().strip('"')[:280]
    prompt += ", highly detailed, cinematic lighting, vibrant colors, sharp focus, professional editorial illustration, 4k"
    seed = random.randint(1, 1_000_000)
    url = ("https://image.pollinations.ai/prompt/" + quote(prompt)
           + f"?width=1280&height=720&nologo=true&model=flux&seed={seed}")
    log(f"  промпт картинки: {prompt}")
    return url


def tg_send_photo(photo_url, caption):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        json={"chat_id": CHANNEL, "photo": photo_url, "caption": caption[:TG_CAPTION_LIMIT]},
        timeout=60,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto error: {data}")
    return data


def tg_send_message(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHANNEL, "text": text[:4096], "disable_web_page_preview": False},
        timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage error: {data}")
    return data


def publish(post_full, image_url):
    if image_url:
        try:
            tg_send_photo(image_url, post_full)
            log("Отправлено в Telegram с картинкой ✅")
            return
        except Exception as e:
            log(f"  не удалось отправить с картинкой, шлю текстом: {e}")
    tg_send_message(post_full)
    log("Отправлено в Telegram (текст) ✅")


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
    tlabel = time_label()
    log(f"Время суток: {tlabel}. Нужно постов: {POSTS_PER_RUN}")

    posted_now = 0
    for item in candidates[:10]:
        if posted_now >= POSTS_PER_RUN:
            break
        if is_political(item):
            log(f"Пропускаю политическую тему: {item['title'][:60]}")
            continue
        try:
            article = fetch_article_text(item["link"])
            post = write_post(item, article, tlabel)
            post_full = post
            image_url = make_image_url(item, post) if IMAGES_ENABLED else None
            log("----- ПОСТ -----")
            log(post_full)
            log(f"----- КАРТИНКА -----\n{image_url}")
            if DRY_RUN:
                log("(DRY_RUN: не отправляю)")
            else:
                publish(post_full, image_url)
            seen.add(item["link"])
            posted_now += 1
            time.sleep(2)
        except Exception as e:
            log(f"Пропускаю «{item['title'][:60]}»: {e}")

    save_state(seen)
    log(f"Готово. Опубликовано в этот запуск: {posted_now}")


if __name__ == "__main__":
    main()
