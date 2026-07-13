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

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "продажи Спб")
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
PORT = int(os.environ.get("PORT", 8080))

CATEGORY_SHEETS = {
    "Игрушки":         ("D", "H", "I"),
    "Одежда":          ("B", "F", "G"),
    "Обувь":           ("A", "E", "F"),
    "Крупное":         ("A", "J", "K"),
    "Канцтовары":      ("A", "E", "F"),
    "Книги":           ("A", "E", "F"),
    "Украшения":       ("A", "E", "F"),
    "Спорт":           ("C", "G", "H"),
    "Детские товары":  ("A", "D", "E"),
    "Сумки и рюкзаки": ("A", "D", "E"),
}

# Ключевые слова для определения категории БЕЗ Gemini
CATEGORY_KEYWORDS = {
    "Обувь": ["ботинк", "туфл", "сапог", "кроссовк", "кед", "мокасин", "полуботинк", "сандал", "балетк", "слипон", "тапк", "галош", "валенк", "угг", "кроссовк"],
    "Одежда": ["куртк", "пальто", "платье", "брюк", "джинс", "костюм", "комбинез", "свитер", "кофт", "футболк", "шорт", "юбк", "водолазк", "толстовк", "рубашк", "варежк", "рукавиц", "шапк", "шарф", "перчатк", "колготк", "носк", "боди", "комплект курт", "комплект одежд"],
    "Книги": ["книг", "букварь", "азбук", "энциклопед", "сказк", "рассказ", "стих", "литератур", "учебник", "пособ", "раскраск"],
    "Канцтовары": ["ручк", "карандаш", "тетрад", "альбом", "краск", "фломастер", "пластилин", "ножниц", "клей", "линейк", "пенал", "папк", "блокнот"],
    "Украшения": ["украшен", "браслет", "серьг", "колье", "ободок", "заколк", "резинк для волос", "бусы", "кольцо"],
    "Сумки и рюкзаки": ["рюкзак", "сумк", "портфель", "мешок для обув", "пенал"],
    "Спорт": ["велосипед", "самокат", "ролик", "коньк", "лыж", "мяч", "ракетк", "скейт", "беговел"],
    "Крупное": ["коляск", "кроватк", "манеж", "стул детск", "автокресл", "велосипед", "стол детск"],
    "Детские товары": ["пустышк", "бутылочк", "подгузник", "горшок", "ванночк", "термометр", "молокоотсос", "конверт", "пеленк", "слинг"],
    "Игрушки": ["игрушк", "кукл", "пупс", "машинк", "конструктор", "мозаик", "пазл", "набор игров", "мягк", "плюш", "погремушк", "каталк", "качалк", "зайк", "мишк", "лисичк", "собачк", "кошечк", "тигр", "слон", "жираф", "единорог"],
}

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

def gemini_generate(prompt_or_content, retries=3, wait=30):
    for attempt in range(retries):
        try:
            response = gemini_model.generate_content(prompt_or_content)
            return response
        except Exception as e:
            if "429" in str(e) and attempt < retries - 1:
                logger.warning(f"Лимит Gemini, жду {wait} сек (попытка {attempt+1}/{retries})")
                time.sleep(wait)
            else:
                raise
    return None

def sheets_call_with_retry(func, retries=4, wait=20):
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            if "429" in str(e) and attempt < retries - 1:
                logger.warning(f"Лимит Sheets, жду {wait} сек")
                time.sleep(wait)
            else:
                raise

def normalize(s: str) -> str:
    s = s.lower()
    replacements = {'а':'a','е':'e','о':'o','р':'p','с':'c','х':'x','у':'y','в':'b','к':'k','м':'m','т':'t'}
    return ''.join(replacements.get(c, c) for c in s)

def detect_category(item_name: str) -> str | None:
    """Определяем категорию товара по ключевым словам в названии. Без Gemini!"""
    name_lower = item_name.lower()
    for cat_name, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in name_lower:
                logger.info(f"Категория определена как '{cat_name}' по слову '{kw}'")
                return cat_name
    logger.info(f"Категория не определена для: {item_name}")
    return None

def extract_article_raw(name: str):
    m = re.search(r'арт[.\s]*\(?\s*([^\s\),]+(?:\s+[^\s\),]+)*)', name, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(')')
    return None

def get_article_parts(article: str) -> list:
    article_norm = normalize(article)
    parts = [article_norm]
    subparts = re.split(r'[-\s]', article_norm)
    for part in subparts:
        if len(part) >= 4 and part not in parts:
            parts.append(part)
    digits = re.sub(r'[^0-9]', '', article_norm)
    if len(digits) >= 4 and digits not in parts:
        parts.append(digits)
    return parts

def find_in_category(sheet, item: dict, name_col: str = "A"):
    all_values = sheets_call_with_retry(lambda: sheet.col_values(col_index(name_col)))
    item_name = item.get("name", "")
    article_raw = extract_article_raw(item_name)

    if not article_raw:
        logger.info(f"Артикул не найден в названии: {item_name}")
        return None, None

    article_parts = get_article_parts(article_raw)
    logger.info(f"Ищем артикул: '{article_raw}', части: {article_parts}")

    candidates = []
    for i, cell in enumerate(all_values):
        if i == 0 or not cell.strip():
            continue
        cell_norm = normalize(cell)
        for part in article_parts:
            if part in cell_norm:
                candidates.append((i + 1, cell))
                break

    logger.info(f"Найдено кандидатов: {len(candidates)}")

    if not candidates:
        return None, None

    if len(candidates) == 1:
        logger.info(f"Единственный кандидат: {candidates[0][1]}")
        return candidates[0][0], candidates[0][1]

    if len(candidates) > 50:
        candidates = candidates[:50]

    candidates_text = "\n".join([f"{row}. {name}" for row, name in candidates])
    prompt = f"""Найди в списке товар который соответствует запросу.
Сопоставляй по артикулу, названию, цвету и размеру.
Игнорируй различия в регистре, пунктуации, порядке слов, русских/латинских буквах.

Товар: {item_name}

Кандидаты (номер строки. название):
{candidates_text}

Верни ТОЛЬКО номер строки подходящего товара, или null если ни один не подходит."""

    try:
        response = gemini_generate(prompt)
        result = response.text.strip()
        logger.info(f"Gemini выбрал: {result}")
        if result.lower() == "null":
            return None, None
        m = re.search(r'\d+', result)
        if not m:
            return None, None
        row_num = int(m.group())
        found_name = next((name for row, name in candidates if row == row_num), None)
        return row_num, found_name
    except Exception as e:
        logger.error(f"Ошибка Gemini при выборе кандидата: {e}")
        return None, None

def update_category(sheet, row: int, qty: int, price, col_qty: str, col_price: str):
    qty_idx = col_index(col_qty)
    price_idx = col_index(col_price)

    current_qty = sheets_call_with_retry(lambda: sheet.cell(row, qty_idx).value)
    try:
        new_qty = int(current_qty or 0) + qty
    except:
        new_qty = qty
    sheets_call_with_retry(lambda: sheet.update_cell(row, qty_idx, new_qty))

    current_price = sheets_call_with_retry(lambda: sheet.cell(row, price_idx).value) or ""
    try:
        if isinstance(price, (int, float)) and float(price) == int(float(price)):
            price_str = str(int(float(price)))
        else:
            price_str = str(price).replace('.', ',')
    except:
        price_str = str(price).replace('.', ',')
    if current_price.strip():
        new_price = current_price.strip() + "+" + price_str
    else:
        new_price = price_str
    sheets_call_with_retry(lambda: sheet.update_cell(row, price_idx, new_price))

def append_sales(items: list):
    main_sheet = get_main_sheet()
    next_num = get_next_sale_number(main_sheet)
    rows = []
    category_results = []

    for i, item in enumerate(items):
        found_category = None
        found_name_in_registry = None

        # Определяем категорию по ключевым словам (без Gemini!)
        cat_name = detect_category(item.get("name", ""))

        if cat_name and cat_name in CATEGORY_SHEETS:
            col_name, col_qty, col_price = CATEGORY_SHEETS[cat_name]
            cat_sheet = get_category_sheet(cat_name)
            if cat_sheet:
                try:
                    row, registry_name = find_in_category(cat_sheet, item, col_name)
                    if row:
                        found_category = cat_name
                        found_name_in_registry = registry_name
                        update_category(cat_sheet, row, item["qty"], item["price"], col_qty, col_price)
                        logger.info(f"Обновлена категория '{cat_name}', строка {row}")
                except Exception as e:
                    logger.error(f"Ошибка поиска/обновления в категории {cat_name}: {e}")

        final_name = found_name_in_registry if found_name_in_registry else item["name"]
        note = "" if found_category else "не найдено в описи"
        rows.append([
            next_num + i,
            item["date"],
            final_name,
            item["qty"],
            "",
            item["price"],
            "", "", "", "", "", "", "", note
        ])
        category_results.append((found_category is not None, found_category or ""))

    main_sheet.append_rows(rows, value_input_option="USER_ENTERED")
    logger.info(f"Добавлено {len(rows)} строк начиная с № {next_num}")
    return next_num, len(rows), category_results

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-3.1-flash-lite-preview")

PROMPT_TEMPLATE = """Ты помощник, который извлекает данные о продажах из изображений для магазина детских товаров.

На входе — фото чека, скриншот кассовой программы, или фото этикетки товара с ценой в подписи. Чек может быть разбит на несколько фото — обрабатывай их все как одну покупку.

Верни ТОЛЬКО валидный JSON массив объектов без пояснений и без ```json блоков.
Каждый объект:
{{
  "name": "полное наименование товара как на этикетке/чеке, включая размер и артикул если есть (формат: Наименование, р.XX, арт. XXXXX)",
  "qty": число (количество штук),
  "price": число (ИТОГОВАЯ сумма по позиции = количество x цена за штуку, может быть дробным с копейками)
}}

Правила извлечения цены:
- В чеке строка выглядит так: КОЛ-ВО х ЦЕНА_ЗА_ШТ = ИТОГ
- Пример: "2.000 х 390.00=780.00" -> qty=2, price=780 (берём ИТОГ, не цену за штуку!)
- В поле price всегда пиши ИТОГОВУЮ сумму по позиции (правая часть после знака =)
- ВАЖНО: не теряй цифры! 590.00 -> price=590, 1000.00 -> price=1000, 260.00 -> price=260

Правила карточной оплаты:
- Если рядом с напечатанным итогом чека написана другая сумма от руки — разница вычитается из самой дорогой позиции
- Разница = напечатанный итог МИНУС рукописная сумма

Другие правила:
1. СКРИНШОТ КАССЫ: брать наименование и итоговую сумму по позиции.
2. ФОТО ЭТИКЕТКИ: название с этикетки включая размер и артикул, цена из подписи, qty=1.
3. ФОТО БЕЗ ЭТИКЕТКИ: если на фото просто товар без ярлыка — определи что это за товар по внешнему виду, запиши описательное название, цену возьми из подписи к фото, qty=1.
4. Если несколько товаров — вернуть массив из нескольких объектов.
5. Количество всегда 1, если не указано иное.
6. Дату НЕ включай.{caption_part}{date_part}

Извлеки данные о продажах из всех переданных фото."""

def extract_sales_from_images(images: list, current_date: str = "") -> list:
    captions = [cap for _, cap in images if cap]
    caption_part = f"\nПодписи к фото: {'; '.join(captions)}" if captions else ""
    date_part = f"\nДата продажи: {current_date}" if current_date else ""
    prompt = PROMPT_TEMPLATE.format(caption_part=caption_part, date_part=date_part)

    content = [prompt]
    for image_bytes, _ in images:
        content.append({"mime_type": "image/jpeg", "data": image_bytes})

    response = gemini_generate(content)
    raw = response.text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    items = json.loads(raw)
    return items if isinstance(items, list) else [items]

chat_dates: dict = {}
media_group_buffer: dict = {}

def parse_date_from_text(text: str):
    patterns = [
        r"\b(\d{2}\.\d{2}\.\d{4})\b",
        r"\b(\d{2}\.\d{2}\.\d{2})\b",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None

async def process_images(images: list, current_date: str, reply_msg):
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
