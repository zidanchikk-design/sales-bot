import os
import json
import logging
import re
import threading
import time
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
SHEET_NAME = os.environ.get("SHEET_NAME", "продажи Спб")
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
PORT = int(os.environ.get("PORT", 8080))

CATEGORY_SHEETS = {
    "Игрушки":         ("H", "I"),
    "Одежда":          ("F", "G"),
    "Обувь":           ("E", "F"),
    "Крупное":         ("J", "K"),
    "Канцтовары":      ("E", "F"),
    "Книги":           ("E", "F"),
    "Украшения":       ("E", "F"),
    "Спорт":           ("G", "H"),
    "Детские товары":  ("D", "E"),
    "Сумки и рюкзаки": ("D", "E"),
}

# ─── HEALTH CHECK ─────────────────────────────────────────────────────────────
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
_gc = None
_spreadsheet = None

def get_client():
    global _gc, _spreadsheet
    if _gc is None:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        _gc = gspread.authorize(creds)
        _spreadsheet = _gc.open_by_key(SPREADSHEET_ID)
    return _spreadsheet

def get_main_sheet():
    return get_client().worksheet(SHEET_NAME)

def get_category_sheet(name: str):
    try:
        return get_client().worksheet(name)
    except Exception:
        return None

def get_next_sale_number(sheet):
    values = sheet.col_values(1)
    nums = []
    for v in values[1:]:
        try:
            nums.append(int(v))
        except:
            pass
    return (max(nums) + 1) if nums else 1

def col_index(letter):
    return ord(letter.upper()) - ord('A') + 1

def extract_article_raw(name: str) -> str | None:
    """Извлекаем артикул как есть — всё что после 'арт.' до скобки, запятой или конца строки."""
    m = re.search(r'арт[.\s]*\(?\s*([^\s\),]+(?:\s+[^\s\),]+)*)', name, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(')')
    return None

def normalize(s: str) -> str:
    """Нормализуем строку: нижний регистр, кириллица→латиница для похожих букв."""
    s = s.lower()
    replacements = {'а':'a','е':'e','о':'o','р':'p','с':'c','х':'x','у':'y','в':'b','к':'k','м':'m','т':'t'}
    return ''.join(replacements.get(c, c) for c in s)

def find_in_category(sheet, item: dict):
    """
    Поиск товара в листе категории:
    1. Берём артикул из названия товара
    2. Ищем все строки где этот артикул встречается как подстрока (нормализованно)
    3. Если один кандидат — возвращаем его
    4. Если несколько — Gemini выбирает наиболее похожий
    """
    all_values = sheet.col_values(1)
    item_name = item.get("name", "")
    article_raw = extract_article_raw(item_name)

    if not article_raw:
        logger.info(f"Артикул не найден в названии: {item_name}")
        return None

    article_norm = normalize(article_raw)
    logger.info(f"Ищем артикул: '{article_raw}' (норм: '{article_norm}')")

    candidates = []
    for i, cell in enumerate(all_values):
        if i == 0 or not cell.strip():
            continue
        if article_norm in normalize(cell):
            candidates.append((i + 1, cell))

    logger.info(f"Найдено кандидатов: {len(candidates)}")

    if not candidates:
        return None

    if len(candidates) == 1:
        logger.info(f"Единственный кандидат: {candidates[0][1]}")
        return candidates[0][0]

    # Несколько кандидатов — Gemini выбирает
    candidates_text = "\n".join([f"{row}. {name}" for row, name in candidates])
    prompt = f"""Найди в списке товар который соответствует запросу.
Сопоставляй по артикулу, названию, цвету и размеру.
Игнорируй различия в регистре, пунктуации, порядке слов, русских/латинских буквах.

Товар из чека: {item_name}

Кандидаты (номер строки. название):
{candidates_text}

Верни ТОЛЬКО номер строки подходящего товара, или null если ни один не подходит."""

    try:
        response = gemini_model.generate_content(prompt)
        result = response.text.strip()
        logger.info(f"Gemini выбрал: {result}")
        if result.lower() == "null":
            return None
        m = re.search(r'\d+', result)
        return int(m.group()) if m else None
    except Exception as e:
        logger.error(f"Ошибка Gemini при выборе кандидата: {e}")
        return None

def update_category(sheet, row: int, qty: int, price, col_qty: str, col_price: str):
    qty_idx = col_index(col_qty)
    price_idx = col_index(col_price)

    current_qty = sheet.cell(row, qty_idx).value
    try:
        new_qty = int(current_qty or 0) + qty
    except:
        new_qty = qty
    sheet.update_cell(row, qty_idx, new_qty)

    current_price = sheet.cell(row, price_idx).value or ""
    price_str = str(int(price) if isinstance(price, float) and price == int(price) else price)
    if current_price.strip():
        new_price = current_price.strip() + "+" + price_str
    else:
        new_price = price_str
    sheet.update_cell(row, price_idx, new_price)

def append_sales(items: list[dict]):
    main_sheet = get_main_sheet()
    next_num = get_next_sale_number(main_sheet)
    rows = []
    category_results = []

    for i, item in enumerate(items):
        found_category = None
        for cat_name, (col_qty, col_price) in CATEGORY_SHEETS.items():
            cat_sheet = get_category_sheet(cat_name)
            if cat_sheet is None:
                continue
            row = find_in_category(cat_sheet, item)
            if row:
                found_category = cat_name
                try:
                    update_category(cat_sheet, row, item["qty"], item["price"], col_qty, col_price)
                    logger.info(f"Обновлена категория '{cat_name}', строка {row}")
                except Exception as e:
                    logger.error(f"Ошибка обновления категории: {e}")
                break
            time.sleep(0.5)

        note = "" if found_category else "не найдено в описи"
        rows.append([
            next_num + i,
            item["date"],
            item["name"],
            item["qty"],
            "",
            item["price"],
            "", "", "", "", "", "", "", note
        ])
        category_results.append((found_category is not None, found_category or ""))

    main_sheet.append_rows(rows, value_input_option="USER_ENTERED")
    logger.info(f"Добавлено {len(rows)} строк начиная с № {next_num}")
    return next_num, len(rows), category_results

# ─── GEMINI ────────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash")

PROMPT_TEMPLATE = """Ты помощник, который извлекает данные о продажах из изображений для магазина детских товаров.

На входе — фото чека, скриншот кассовой программы, или фото этикетки товара с ценой в подписи. Чек может быть разбит на несколько фото — обрабатывай их все как одну покупку.

Верни ТОЛЬКО валидный JSON массив объектов без пояснений и без ```json блоков.
Каждый объект:
{{
  "name": "полное наименование товара как на этикетке/чеке, включая размер и артикул если есть (формат: Наименование, р.XX, арт. XXXXX)",
  "qty": число (количество штук),
  "price": число (ИТОГОВАЯ сумма по позиции = количество × цена за штуку, может быть дробным с копейками)
}}

Правила извлечения цены:
- В чеке строка выглядит так: КОЛ-ВО х ЦЕНА_ЗА_ШТ = ИТОГ
- Пример: "2.000 х 390.00=780.00" → qty=2, price=780 (берём ИТОГ, не цену за штуку!)
- В поле price всегда пиши ИТОГОВУЮ сумму по позиции (правая часть после знака =)

Правила карточной оплаты:
- Если рядом с напечатанным итогом чека написана другая сумма от руки — разница вычитается из самой дорогой позиции
- Разница = напечатанный итог МИНУС рукописная сумма

Другие правила:
1. СКРИНШОТ КАССЫ: брать наименование и итоговую сумму по позиции.
2. ФОТО ЭТИКЕТКИ: название с этикетки включая размер и артикул, цена из подписи, qty=1.
3. Если несколько товаров — вернуть массив из нескольких объектов.
4. Количество всегда 1, если не указано иное.
5. Дату НЕ включай.{caption_part}{date_part}

Извлеки данные о продажах из всех переданных фото."""

def extract_sales_from_images(images: list[tuple[bytes, str]], current_date: str = "") -> list[dict]:
    captions = [cap for _, cap in images if cap]
    caption_part = f"\nПодписи к фото: {'; '.join(captions)}" if captions else ""
    date_part = f"\nДата продажи: {current_date}" if current_date else ""
    prompt = PROMPT_TEMPLATE.format(caption_part=caption_part, date_part=date_part)

    content = [prompt]
    for image_bytes, _ in images:
        content.append({"mime_type": "image/jpeg", "data": image_bytes})

    response = gemini_model.generate_content(content)
    raw = response.text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    items = json.loads(raw)
    return items if isinstance(items, list) else [items]

# ─── STATE ─────────────────────────────────────────────────────────────────────
chat_dates: dict[int, str] = {}
media_group_buffer: dict[str, dict] = {}

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
async def process_images(images: list[tuple[bytes, str]], current_date: str, reply_msg):
    try:
        items = extract_sales_from_images(images, current_date)
        if not items:
            await reply_msg.reply_text("❌ Не удалось распознать товары.")
            return
        for item in items:
            item["date"] = current_date

        start_num, count, cat_results = append_sales(items)

        lines = [f"✅ Добавлено {count} позиц{'ия' if count==1 else 'ии' if count in [2,3,4] else 'ий'} (№{start_num}–{start_num+count-1}):"]
        for item, (found, cat_name) in zip(items, cat_results):
            cat_info = f" [📂 {cat_name}]" if found else " [⚠️ не найдено в описи]"
            lines.append(f"  • {item['name']} — {item['price']} руб. × {item['qty']} шт.{cat_info}")

        await reply_msg.reply_text("\n".join(lines))

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        await reply_msg.reply_text("❌ Ошибка распознавания. Попробуй ещё раз.")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await reply_msg.reply_text(f"❌ Ошибка: {str(e)[:200]}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
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
        file = await context.bot.get_file(msg.photo[-1].file_id)
    else:
        file = await context.bot.get_file(msg.document.file_id)

    image_bytes = bytes(await file.download_as_bytearray())

    if msg.media_group_id:
        group_id = msg.media_group_id
        if group_id not in media_group_buffer:
            media_group_buffer[group_id] = {
                "photos": [],
                "first_msg": msg,
                "date": current_date,
            }
            async def process_group(gid=group_id):
                await asyncio.sleep(5)
                if gid not in media_group_buffer:
                    return
                group = media_group_buffer.pop(gid)
                n = len(group["photos"])
                await group["first_msg"].reply_text(f"⏳ Распознаю продажу ({n} фото)...")
                await process_images(group["photos"], group["date"], group["first_msg"])
            asyncio.create_task(process_group())

        media_group_buffer[group_id]["photos"].append((image_bytes, caption))
        return

    await msg.reply_text("⏳ Распознаю продажу...")
    await process_images([(image_bytes, caption)], current_date, msg)

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
