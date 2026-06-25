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
import anthropic
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
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
        pass  # отключаем лишние логи

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

# ─── ANTHROPIC VISION ──────────────────────────────────────────────────────────
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Ты помощник, который извлекает данные о продажах из изображений для магазина детских товаров.

На входе — фото чека, скриншот кассовой программы, или фото этикетки товара с ценой в подписи.

Верни ТОЛЬКО валидный JSON массив объектов без пояснений и без ```json блоков.
Каждый объект:
{
  "name": "полное наименование товара как на чеке/этикетке, включая размер если есть",
  "qty": число (количество штук),
  "price": число (цена в рублях, целое число)
}

Правила:
1. ЧЕК С ОПЛАТОЙ КАРТОЙ: если рядом с итогом от руки написана другая сумма — это реальная сумма к получению. Разница (напечатанная минус рукописная) вычитается из цены САМОЙ ДОРОГОЙ позиции.
2. СКРИНШОТ КАССЫ: брать наименование и цену из строк товаров.
3. ФОТО ЭТИКЕТКИ: брать наименование с этикетки (включая размер), цена будет в подписи к фото.
4. Если на чеке несколько товаров — вернуть массив из нескольких объектов.
5. Количество всегда 1, если не указано иное.
6. Дату НЕ включай — она передаётся отдельно."""

def extract_sales_from_image(image_bytes: bytes, caption: str = "", current_date: str = "") -> list[dict]:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    user_text = "Извлеки данные о продажах из этого изображения."
    if caption:
        user_text += f"\nПодпись к фото: {caption}"
    if current_date:
        user_text += f"\nДата продажи: {current_date}"

    response = claude_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": user_text}
            ]
        }]
    )
    raw = response.content[0].text.strip()
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
    # Запускаем health check сервер в отдельном потоке
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    logger.info(f"Health check сервер запущен на порту {PORT}")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
