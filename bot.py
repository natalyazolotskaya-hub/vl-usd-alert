import os
import re
import json
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
SOURCE_URL = os.getenv("SOURCE_URL", "https://www.vl.ru/dengi/")
THRESHOLD = float(os.getenv("THRESHOLD", "80"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Vladivostok")
DB_PATH = os.getenv("DB_PATH", "state.sqlite3")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("vl-usd-bot")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; VLRateMonitor/1.0; +https://www.vl.ru/)"
}

def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    return con

def get_state(key, default=""):
    with db() as con:
        row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def set_state(key, value):
    with db() as con:
        con.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )

def number_from_text(value):
    value = value.replace("\\xa0", " ").replace(",", ".")
    match = re.search(r"(?<!\\d)(\\d{1,3}(?:[ \\u00a0]\\d{3})*(?:\\.\\d{1,4})?|\\d+(?:\\.\\d{1,4})?)", value)
    if not match:
        return None
    try:
        return float(re.sub(r"\\s+", "", match.group(1)))
    except ValueError:
        return None

def extract_usd_sell_rates(html):
    """
    Эвристический парсер таблиц VL.ru.
    Возвращает список (банк, курс продажи USD).
    Если структура сайта изменилась или заголовки неоднозначны,
    ничего не возвращает — бот не будет угадывать курс.
    """
    soup = BeautifulSoup(html, "html.parser")
    found = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_cells = []
        header_row_index = None
        for i, row in enumerate(rows[:4]):
            cells = row.find_all(["th", "td"])
            text = " ".join(c.get_text(" ", strip=True) for c in cells).lower()
            if ("продаж" in text and ("usd" in text or "доллар" in text)):
                header_cells = [c.get_text(" ", strip=True).lower() for c in cells]
                header_row_index = i
                break

        if not header_cells:
            continue

        sell_idx = None
        bank_idx = 0
        for i, h in enumerate(header_cells):
            if "продаж" in h:
                sell_idx = i
            if "банк" in h:
                bank_idx = i
        if sell_idx is None:
            continue

        for row in rows[header_row_index + 1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) <= sell_idx:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]
            joined = " ".join(texts).lower()
            if not ("usd" in joined or "доллар" in joined):
                # In a bank-per-row table USD may be implied by the table header.
                pass
            rate = number_from_text(texts[sell_idx])
            bank = texts[bank_idx] if bank_idx < len(texts) else ""
            if rate and 10 < rate < 1000 and bank:
                found.append((bank, rate))

    # Fallback for responsive layouts: only accept rows that explicitly
    # mention both USD/dollar and sale, and contain a plausible rate.
    if not found:
        for row in soup.find_all(["tr", "li", "article"]):
            text = row.get_text(" ", strip=True)
            low = text.lower()
            if ("usd" not in low and "доллар" not in low):
                continue
            if "продаж" not in low:
                continue
            nums = [number_from_text(x) for x in re.findall(
                r"\\d{1,3}(?:[ \\u00a0]\\d{3})*(?:[,.]\\d{1,4})?", text
            )]
            nums = [n for n in nums if n and 10 < n < 1000]
            if nums:
                bank = text[:100]
                found.append((bank, nums[-1]))

    # Remove duplicates
    unique = {}
    for bank, rate in found:
        unique[(bank, rate)] = (bank, rate)
    return list(unique.values())

def fetch_rates():
    response = requests.get(SOURCE_URL, headers=HEADERS, timeout=25)
    response.raise_for_status()
    return extract_usd_sell_rates(response.text)

async def send(context, message):
    if CHAT_ID:
        await context.bot.send_message(chat_id=CHAT_ID, text=message)

async def check_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        rates = fetch_rates()
        if not rates:
            log.warning("Не удалось уверенно извлечь курсы. Проверьте HTML/селекторы VL.ru.")
            # Do not send a false alert and do not alter alert state.
            return

        min_bank, min_rate = min(rates, key=lambda x: x[1])
        log.info("Найдено курсов: %s; минимум: %s %.4f", len(rates), min_bank, min_rate)

        was_below = get_state("was_below", "0") == "1"
        if min_rate < THRESHOLD:
            if not was_below:
                now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y %H:%M")
                await send(
                    context,
                    f"🔔 Доллар на VL.ru ниже порога!\\n"
                    f"Продажа: {min_rate:.2f} ₽\\n"
                    f"Банк: {min_bank}\\n"
                    f"Ваш порог: {THRESHOLD:.2f} ₽\\n"
                    f"Время: {now}\\n"
                    f"Источник: {SOURCE_URL}"
                )
            set_state("was_below", "1")
        else:
            set_state("was_below", "0")

    except Exception:
        log.exception("Ошибка проверки курса")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text(
        "Бот отслеживает продажу USD на VL.ru.\\n"
        f"Порог: ниже {THRESHOLD:.2f} ₽; проверка каждые {CHECK_INTERVAL_MINUTES} мин.\\n"
        f"Ваш chat_id: {chat_id}\\n"
        "Добавьте этот chat_id в файл .env как CHAT_ID и перезапустите бота."
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    below = get_state("was_below", "0") == "1"
    await update.message.reply_text(
        f"Источник: {SOURCE_URL}\\nВалюта: USD\\nТип: продажа банком\\n"
        f"Порог: ниже {THRESHOLD:.2f} ₽\\n"
        f"Интервал: {CHECK_INTERVAL_MINUTES} мин.\\n"
        f"Состояние порога: {'ниже порога' if below else 'ожидание'}"
    )

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Тестовое сообщение: Telegram-бот работает.")

async def post_init(app: Application):
    app.job_queue.run_repeating(
        check_job,
        interval=CHECK_INTERVAL_MINUTES * 60,
        first=5,
        name="vl-usd-rate-check"
    )

def main():
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в файле .env")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("test", test))
    app.run_polling()

if __name__ == "__main__":
    main()
