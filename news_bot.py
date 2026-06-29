#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ИИ Сейчас — автопостер @ai_seychas. 4 агента + окна + жирный/эмодзи + vision-проверка картинки.
GitHub запускает каждые 30 минут, пост выходит один раз за окно (утро/день/вечер).
"""

import os
import re
import sys
import json
import html
import time
import base64
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
FORCE = os.environ.get("FORCE", "").strip() in ("1", "true", "True", "yes")

STATE_FILE = "posted.json"
FEEDS_FILE = "feeds.txt"
MAX_AGE_HOURS = 24
MAX_STATE = 800
TG_CAPTION_LIMIT = 1024

WINDOWS = {
    "morning": (8, 11, "утро — человек только проснулся, тон бодрый и заряжающий"),
    "day": (14, 16, "день — деловой энергичный дайджест, по сути"),
    "evening": (19, 21, "вечер — спокойный, но захватывающий разбор главного за день"),
}

DEFAULT_FEEDS = [
    "https://news.google.com/rss/search?q=%22artificial%20intelligence%22%20OR%20%22AI%20model%22%20when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=%22AI%20agents%22%20OR%20LLM%20OR%20OpenAI%20OR%20Anthropic%20OR%20Google%20when:1d&hl=en-US&gl=US&ceid=US:en",
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

COMPANIES = [
    "openai", "chatgpt", "gpt", "anthropic", "claude", "google", "gemini", "deepmind",
    "meta", "llama", "microsoft", "copilot", "nvidia", "mistral", "qwen", "alibaba",
    "deepseek", "xai", "grok", "apple", "amazon", "perplexity", "midjourney", "stability", "huggingface",
]


def log(msg):
    print(f"[ai-seychas] {msg}", flush=True)


def local_now():
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)


def current_window():
    h = local_now().hour
    for name, (start, end, _label) in WINDOWS.items():
        if start <= h <= end:
            return name
    return None


def is_political(item):
    text = (item["title"] + " " + item["summary"]).lower()
    return any(t in text for t in POLITICAL_TERMS)


def main_subject(item):
    t = (item["title"] + " " + item["summary"]).lower()
    for c in COMPANIES:
        if c in t:
            return c
    return ""


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
                d = json.load(f)
                return (set(d.get("posted", [])),
                        list(d.get("recent_subjects", [])),
                        list(d.get("done_windows", [])))
        except Exception:
            return set(), [], []
    return set(), [], []


def save_state(posted_set, recent_subjects, done_windows):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "posted": list(posted_set)[-MAX_STATE:],
            "recent_subjects": recent_subjects[-6:],
            "done_windows": done_windows[-12:],
        }, f, ensure_ascii=False, indent=2)


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


def gemini_generate(system_prompt, user_msg, max_tokens=800, temperature=0.8, image_bytes=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    parts = [{"text": user_msg}]
    if image_bytes:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(image_bytes).decode()}})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens,
                             "thinkingConfig": {"thinkingBudget": 0}},
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


# ===================== АГЕНТ 1 — СБОР =====================
def agent1_collect(feeds, seen):
    now = dt.datetime.now(dt.timezone.utc)
    items = {}
    for feed_url in feeds:
        try:
            log(f"[Агент 1] читаю фид: {feed_url}")
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
            items[link] = {"title": title, "link": link, "summary": summary[:1200],
                           "ts": ts, "age_h": age_h, "source": source_name(link)}
    log(f"[Агент 1] собрано свежих новостей: {len(items)}")
    return list(items.values())


# ===================== АГЕНТ 2 — ОТБОР =====================
def _score(item):
    text = (item["title"] + " " + item["summary"]).lower()
    hot = sum(1 for term in HOT_TERMS if term in text)
    recency = max(0.0, MAX_AGE_HOURS - item["age_h"]) / MAX_AGE_HOURS
    return hot + recency * 3


def agent2_select(candidates, recent_subjects):
    fresh = [it for it in candidates if not is_political(it)]
    fresh.sort(key=_score, reverse=True)
    fresh.sort(key=lambda it: 1 if (main_subject(it) and main_subject(it) in recent_subjects) else 0)
    log(f"[Агент 2] кандидатов после отбора: {len(fresh)}")
    return fresh


# ===================== АГЕНТ 3 — АВТОР =====================
POST_SYSTEM = (
    "Ты — редактор Telegram-канала «ИИ Сейчас» (@ai_seychas) про ИИ, нейросети и агентов "
    "для широкой аудитории. Пишешь на русском.\n"
    "ОТВЕТ — ТОЛЬКО готовый текст поста на русском, без комментариев и без английского.\n"
    "ЦЕЛЬ: чтобы человек прочитал ДО КОНЦА и захотел оставить комментарий.\n"
    "ФОРМАТ:\n"
    "- Подача как новость: суть (что и у кого), потом почему это важно.\n"
    "- Мощный хук в первой строке.\n"
    "- ГЛАВНОЕ выделяй жирным, оборачивая в двойные звёздочки: **главная мысль**, "
    "**названия**, **цифры**. Выдели так 2–4 ключевых места.\n"
    "- Эмодзи активно: 4–7 штук, по смыслу, в начале абзацев и у ключевых мыслей.\n"
    "- Обращение на «ты», энергично, без вранья. Жаргон объясняй простыми словами.\n"
    "- Длина 500–900 знаков. В конце — цепляющий вопрос для комментариев.\n"
    "- БЕЗ хэштегов, БЕЗ ссылок, БЕЗ слова «Источник», БЕЗ заголовков-решёток.\n"
    "ФАКТЫ: только по источнику, не выдумывай; числа/имена точно как в источнике."
)


def _looks_broken(text):
    if len(text) < 180:
        return True
    latin = sum(1 for c in text if "a" <= c.lower() <= "z")
    cyr = sum(1 for c in text if "а" <= c.lower() <= "я")
    if cyr < 80 or latin > cyr:
        return True
    low = text.lower()
    bad = ["is better", "in the source", "is a bit", "i would", "as an ai", "here is", "i cannot"]
    return any(b in low for b in bad)


def agent3_write_post(item, article_text, tlabel):
    context = article_text if len(article_text) > len(item["summary"]) else item["summary"]
    user_msg = (f"Время суток: {tlabel}\nЗаголовок: {item['title']}\nИсточник: {item['source']}\n"
                f"Текст источника:\n{context}\n\nНапиши пост по правилам.")
    for attempt in range(2):
        text = gemini_generate(POST_SYSTEM, user_msg, 800, 0.75 if attempt == 0 else 0.4)
        text = text.replace("__", "").strip()
        if not _looks_broken(text):
            log("[Агент 3] пост готов")
            return text
        log(f"  [Агент 3] брак (попытка {attempt + 1}), пробую снова")
    raise RuntimeError("Не удалось получить нормальный пост.")


IMG_SYSTEM = (
    "You are an art director. Read the Russian post and write ONE English image prompt for a cover "
    "that LITERALLY depicts the concrete subject of THIS news as a clear cinematic photo-real scene: "
    "the actual robot, gadget, phone, chip, device, datacenter, or people using the technology. "
    "STRICTLY FORBIDDEN: brains, glowing blue brain, abstract neural-network nodes, circuit-board "
    "patterns, generic 'AI' blobs, wireframe heads. Concrete real-world scene only. "
    "NO text, NO words, NO letters, NO logos, NO watermarks. One sentence, max 45 words. Return ONLY the prompt."
)


def _build_image_url(prompt):
    prompt = prompt.replace("\n", " ").strip().strip('"')[:280]
    prompt += ", cinematic photography, highly detailed, dramatic lighting, sharp focus, 4k"
    seed = random.randint(1, 1_000_000)
    return ("https://image.pollinations.ai/prompt/" + quote(prompt)
            + f"?width=1280&height=720&nologo=true&model=flux&seed={seed}")


def agent3_make_image(item, post):
    try:
        prompt = gemini_generate(IMG_SYSTEM,
                                 f"Russian post:\n{post[:900]}\n\nWrite the cover image prompt.",
                                 120, 0.7)
    except Exception as e:
        log(f"  [Агент 3] промпт картинки не вышел: {e}")
        prompt = "a sleek modern robot using a laptop in a bright office, cinematic photography"
    prompt = prompt.replace("\n", " ").strip().strip('"')
    log(f"[Агент 3] промпт картинки: {prompt}")
    return prompt, _build_image_url(prompt)


# ===================== АГЕНТ 4 — КОНТРОЛЬ (vision) + ОТПРАВКА =====================
VISION_CHECK = (
    "You see a generated cover image and a Russian news post. If the image clearly matches the post's "
    "main subject and is NOT a generic brain/abstract-AI picture, reply EXACTLY: OK. Otherwise reply "
    "with ONE improved English image prompt (concrete real scene, no text/logos/brains). Reply ONLY "
    "'OK' or the new prompt."
)


def _to_html(text):
    esc = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc, flags=re.S)


def _tg(method, payload):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=60)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} error: {data}")
    return data


def _publish(post, img_url):
    html_post = _to_html(post)
    plain = post.replace("**", "")
    if img_url:
        try:
            _tg("sendPhoto", {"chat_id": CHANNEL, "photo": img_url,
                              "caption": html_post[:TG_CAPTION_LIMIT], "parse_mode": "HTML"})
            log("[Агент 4] опубликовано с картинкой ✅")
            return
        except Exception as e:
            log(f"  [Агент 4] HTML/картинка не прошли ({e}), пробую проще")
            try:
                _tg("sendPhoto", {"chat_id": CHANNEL, "photo": img_url, "caption": plain[:TG_CAPTION_LIMIT]})
                log("[Агент 4] опубликовано с картинкой (без разметки) ✅")
                return
            except Exception as e2:
                log(f"  [Агент 4] картинка не ушла ({e2}), шлю текстом")
    try:
        _tg("sendMessage", {"chat_id": CHANNEL, "text": html_post[:4096], "parse_mode": "HTML"})
    except Exception:
        _tg("sendMessage", {"chat_id": CHANNEL, "text": plain[:4096]})
    log("[Агент 4] опубликовано текстом ✅")


def agent4_check_and_publish(item, post, img_prompt, img_url):
    if img_url and img_prompt:
        try:
            img_bytes = requests.get(img_url, timeout=70).content
            verdict = gemini_generate(VISION_CHECK, f"Пост:\n{post[:800]}", 80, 0.2,
                                      image_bytes=img_bytes).strip()
            if verdict and not verdict.upper().startswith("OK") and len(verdict) > 15:
                log(f"[Агент 4] картинка не подошла — перегенерирую: {verdict[:80]}")
                img_url = _build_image_url(verdict)
            else:
                log("[Агент 4] картинка проверена (vision) и подходит ✅")
        except Exception as e:
            log(f"  [Агент 4] vision-проверку пропускаю: {e}")
    log("----- ПОСТ -----")
    log(post)
    log(f"----- КАРТИНКА -----\n{img_url}")
    if DRY_RUN:
        log("(DRY_RUN: не отправляю)")
        return
    _publish(post, img_url)


# ===================== ГЛАВНЫЙ КОНВЕЙЕР =====================
def main():
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", BOT_TOKEN), ("TELEGRAM_CHANNEL", CHANNEL),
                              ("GEMINI_API_KEY", GEMINI_API_KEY)] if not v]
    if missing:
        log(f"НЕ заданы секреты: {', '.join(missing)}")
        sys.exit(1)

    seen, recent_subjects, done_windows = load_state()
    window = current_window()
    today = local_now().strftime("%Y-%m-%d")
    if FORCE and not window:
        window = "manual"
    if not window:
        log(f"Сейчас {local_now().strftime('%H:%M')} — вне окна постинга. Выходим.")
        return
    wkey = f"{today}-{window}"
    if not FORCE and wkey in done_windows:
        log(f"В окно «{window}» ({today}) уже постили — выходим.")
        return

    tlabel = WINDOWS.get(window, (0, 0, "дайджест по ИИ"))[2]
    log(f"Окно: {window} ({local_now().strftime('%H:%M')}). Нужно постов: {POSTS_PER_RUN}")

    candidates = agent1_collect(load_feeds(), seen)
    if not candidates:
        log("Нечего постить — свежих новостей нет. Выходим.")
        return
    ordered = agent2_select(candidates, recent_subjects)

    posted_now = 0
    for item in ordered[:10]:
        if posted_now >= POSTS_PER_RUN:
            break
        try:
            post = agent3_write_post(item, fetch_article_text(item["link"]), tlabel)
            if IMAGES_ENABLED:
                img_prompt, img_url = agent3_make_image(item, post)
            else:
                img_prompt, img_url = "", None
            agent4_check_and_publish(item, post, img_prompt, img_url)
            seen.add(item["link"])
            subj = main_subject(item)
            if subj:
                recent_subjects.append(subj)
            posted_now += 1
            time.sleep(2)
        except Exception as e:
            log(f"Пропускаю «{item['title'][:60]}»: {e}")

    if posted_now > 0 and wkey not in done_windows:
        done_windows.append(wkey)
    save_state(seen, recent_subjects, done_windows)
    log(f"Готово. Опубликовано: {posted_now}")


if __name__ == "__main__":
    main()
