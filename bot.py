import os
import json
import base64
import logging
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "Лист1")
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
PORT = int(os.environ.get("PORT", 8080))

# ─── HEALTH CHECK сервер (нужен для Render) ────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# ─── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(SHEET_NAME)

def get_next_sale_number(sheet):
    values = sheet.col_values(1)
    nums = []
    for v in values[1:]:
        try:
            nums.append(int(v))
        except:
            pass
    return (max(nums) + 1) if nums else 1

def append_sales(items: list[dict]):
    sheet = get_sheet()
    next_num = get_next_sale_number(sheet)
    rows = []
    for i, item in enumerate(items):
        rows.append([
            next_num + i,
            item["date"],
            item["name"],
            item["qty"],
            "",
            item["price"]
        ])
    sheet.append_rows(rows, value_input_option="USER_ENTERED")
    logger.info(f"Добавлено {len(rows)} строк начиная с № {next_num}")
    return next_num, len(rows)

# ─── GEMINI VISION ─────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash")

PROMPT_TEMPLATE = """Ты помощник, который извлекает данные о продажах из изображений для магазина детских товаров.

На входе — фото чека, скриншот кассовой программы, или фото этикетки товара с ценой в подписи.

Верни ТОЛЬКО валидный JSON массив объектов без пояснений и без ```json блоков.
Каждый объект:
{{
  "name": "полное наименование товара как на чеке/этикетке, включая размер если есть",
  "qty": число (количество штук),
  "price": число (ИТОГОВАЯ сумма по позиции = количество × цена за штуку, может быть дробным с копейками)
}}

Правила извлечения цены:
- В чеке строка выглядит так: КОЛ-ВО х ЦЕНА_ЗА_ШТ = ИТОГ
- Пример: "2.000 х 390.00=780.00" → qty=2, price=780 (берём ИТОГ, не цену за штуку!)
- Пример: "1.000 х 180.00=180.00" → qty=1, price=180
- В поле price всегда пиши ИТОГОВУЮ сумму по позиции (правая часть после знака =)

Правила карточной оплаты:
- Если рядом с напечатанным итогом чека написана другая сумма от руки — это реальная сумма наличными/к получению
- Разница = напечатанный итог МИНУС рукописная сумма
- Эту разницу вычти из price САМОЙ ДОРОГОЙ позиции
- Пример: напечатано 1530, написано от руки 1468.8 → разница 61.2 → вычти 61.2 из самой дорогой позиции

Другие правила:
1. СКРИНШОТ КАССЫ: брать наименование и итоговую сумму по позиции.
2. ФОТО ЭТИКЕТКИ: брать наименование с этикетки (включая размер), цена будет в подписи к фото, qty=1.
3. Если на чеке несколько товаров — вернуть массив из нескольких объектов.
4. Количество всегда 1, если не указано иное.
5. Дату НЕ включай — она передаётся отдельно.{caption_part}{date_part}

Извлеки данные о продажах из этого изображения."""

def extract_sales_from_image(image_bytes: bytes, caption: str = "", current_date: str = "") -> list[dict]:
    caption_part = f"\nПодпись к фото: {caption}" if caption else ""
    date_part = f"\nДата продажи: {current_date}" if current_date else ""
    prompt = PROMPT_TEMPLATE.format(caption_part=caption_part, date_part=date_part)

    image_part = {
        "mime_type": "image/jpeg",
        "data": image_bytes
    }

    response = gemini_model.generate_content([prompt, image_part])
    raw = response.text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    items = json.loads(raw)
    return items if isinstance(items, list) else [items]

# ─── STATE ─────────────────────────────────────────────────────────────────────
chat_dates: dict[int, str] = {}

def parse_date_from_text(text: str) -> str | None:
    patterns = [
        r"\b(\d{2}\.\d{2}\.\d{4})\b",
        r"\b(\d{2}\.\d{2}\.\d{2})\b",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None

# ─── HANDLERS ──────────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat_id = msg.chat_id

    if msg.text:
        date = parse_date_from_text(msg.text)
        if date:
            chat_dates[chat_id] = date
            logger.info(f"Чат {chat_id}: установлена дата {date}")
            return

    if not msg.photo and not msg.document:
        return

    current_date = chat_dates.get(chat_id, datetime.now().strftime("%d.%m.%y"))
    caption = msg.caption or ""

    if msg.photo:
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
    else:
        file = await context.bot.get_file(msg.document.file_id)

    image_bytes = await file.download_as_bytearray()

    try:
        await msg.reply_text("⏳ Распознаю продажу...")
        items = extract_sales_from_image(bytes(image_bytes), caption, current_date)

        if not items:
            await msg.reply_text("❌ Не удалось распознать товары на изображении.")
            return

        for item in items:
            item["date"] = current_date

        start_num, count = append_sales(items)

        lines = [f"✅ Добавлено {count} позиц{'ия' if count==1 else 'ии' if count in [2,3,4] else 'ий'} (№{start_num}–{start_num+count-1}):"]
        for item in items:
            lines.append(f"  • {item['name']} — {item['price']} руб. × {item['qty']} шт.")

        await msg.reply_text("\n".join(lines))

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        await msg.reply_text("❌ Ошибка распознавания. Попробуй ещё раз или добавь вручную.")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await msg.reply_text(f"❌ Ошибка: {str(e)[:200]}")

# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    logger.info(f"Health check сервер запущен на порту {PORT}")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
