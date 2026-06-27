import os
import json
import base64
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

# Листы категорий и столбцы (количество, цена)
CATEGORY_SHEETS = {
    "Игрушки":        ("H", "I"),
    "Одежда":         ("F", "G"),
    "Обувь":          ("E", "F"),
    "Крупное":        ("J", "K"),
    "Канцтовары":     ("E", "F"),
    "Книги":          ("E", "F"),
    "Украшения":      ("E", "F"),
    "Спорт":          ("G", "H"),
    "Детские товары": ("D", "E"),
    "Сумки и рюкзаки":("D", "E"),
}

def col_letter_to_index(letter: str) -> int:
    """A=1, B=2, ..."""
    return ord(letter.upper()) - ord('A') + 1

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

def extract_article(name: str) -> str | None:
    """Извлекаем артикул из названия товара — всё что после 'арт'"""
    m = re.search(r'арт[.\s]*[\(]?\s*([^\s\),]+)', name, re.IGNORECASE)
    if m:
        # Оставляем только буквы и цифры для нечёткого сравнения
        return re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', m.group(1)).lower()
    return None

def fuzzy_article_match(art1: str, art2: str) -> bool:
    """Сравниваем артикулы нечётко - только буквы и цифры, без учёта регистра и кириллица=латиница"""
    # Заменяем похожие кириллические на латинские
    def normalize(s):
        s = s.lower()
        replacements = {'а':'a','е':'e','о':'o','р':'p','с':'c','х':'x','у':'y','в':'b'}
        return ''.join(replacements.get(c, c) for c in s)
    a1 = normalize(re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', art1))
    a2 = normalize(re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', art2))
    return a1 == a2

def find_in_category(sheet, item: dict):
    """
    Ищем строку в листе категории.
    1. Фильтруем по артикулу (нечёткое совпадение)
    2. Если кандидатов мало — Gemini выбирает финальный ответ
    Возвращает номер строки (1-based) или None.
    """
    all_values = sheet.col_values(1)
    item_name = item.get("name", "")
    item_art = extract_article(item_name)

    # Шаг 1: фильтруем кандидатов по артикулу
    candidates = []
    for i, cell in enumerate(all_values):
        if i == 0 or not cell.strip():
            continue
        row = i + 1
        if item_art:
            cell_art = extract_article(cell)
            if cell_art and fuzzy_article_match(item_art, cell_art):
                candidates.append((row, cell))
        else:
            # Нет артикула — берём все непустые строки (Gemini разберётся)
            candidates.append((row, cell))

    if not candidates:
        return None

    # Если один кандидат — возвращаем сразу
    if len(candidates) == 1:
        return candidates[0][0]

    # Если кандидатов много и нет артикула — ограничиваем до 50
    if len(candidates) > 50:
        candidates = candidates[:50]

    # Шаг 2: Gemini выбирает из кандидатов
    candidates_text = "\n".join([f"{row}. {name}" for row, name in candidates])
    prompt = f"""Найди в списке товар который соответствует запросу.
Сопоставляй по артикулу, названию, цвету и размеру. Игнорируй различия в регистре, пунктуации, порядке слов, русских/латинских буквах (а/a, е/e, о/o, с/c, р/p, х/x).

Товар: {item_name}

Список (номер. название):
{candidates_text}

Верни ТОЛЬКО номер строки если нашёл совпадение, или null если не нашёл."""

    try:
        response = gemini_model.generate_content(prompt)
        result = response.text.strip()
        if result.lower() == "null":
            return None
        return int(re.search(r'\d+', result).group())
    except Exception as e:
        logger.error(f"Ошибка поиска через Gemini: {e}")
        return None

def update_category(sheet, row: int, qty: int, price, col_qty: str, col_price: str):
    """Прибавляем количество и дописываем +цена в листе категории."""
    # Количество
    qty_idx = col_index(col_qty)
    price_idx = col_index(col_price)

    current_qty = sheet.cell(row, qty_idx).value
    try:
        new_qty = int(current_qty or 0) + qty
    except:
        new_qty = qty
    sheet.update_cell(row, qty_idx, new_qty)

    # Цена
    current_price = sheet.cell(row, price_idx).value or ""
    price_str = str(int(price) if price == int(price) else price)
    if current_price.strip():
        new_price = current_price.strip() + "+" + price_str
    else:
        new_price = price_str
    sheet.update_cell(row, price_idx, new_price)

def append_sales(items: list[dict]):
    main_sheet = get_main_sheet()
    next_num = get_next_sale_number(main_sheet)
    rows = []
    category_results = []  # (found: bool, category_name: str)

    for i, item in enumerate(items):
        # Ищем в категориях
        found_category = None
        found_row = None
        for cat_name, (col_qty, col_price) in CATEGORY_SHEETS.items():
            cat_sheet = get_category_sheet(cat_name)
            if cat_sheet is None:
                continue
            row = find_in_category(cat_sheet, item)
            if row:
                found_category = cat_name
                found_row = row
                # Обновляем категорию
                try:
                    update_category(cat_sheet, row, item["qty"], item["price"], col_qty, col_price)
                    logger.info(f"Обновлена категория '{cat_name}', строка {row}")
                except Exception as e:
                    logger.error(f"Ошибка обновления категории: {e}")
                break

        # Формируем строку для основного листа
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
        time.sleep(1)  # пауза чтобы не превысить лимиты API Google

    main_sheet.append_rows(rows, value_input_option="USER_ENTERED")
    logger.info(f"Добавлено {len(rows)} строк начиная с № {next_num}")
    return next_num, len(rows), category_results

# ─── GEMINI VISION ─────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash")

PROMPT_TEMPLATE = """Ты помощник, который извлекает данные о продажах из изображений для магазина детских товаров.

На входе — фото чека, скриншот кассовой программы, или фото этикетки товара с ценой в подписи. Иногда чек занимает два фото — тогда тебе передают оба.

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
- Пример: "1.000 х 180.00=180.00" → qty=1, price=180
- В поле price всегда пиши ИТОГОВУЮ сумму по позиции (правая часть после знака =)

Правила карточной оплаты:
- Если рядом с напечатанным итогом чека написана другая сумма от руки — это реальная сумма наличными/к получению
- Разница = напечатанный итог МИНУС рукописная сумма
- Эту разницу вычти из price САМОЙ ДОРОГОЙ позиции по всем товарам
- Пример: напечатано 1530, написано от руки 1468.8 → разница 61.2 → вычти 61.2 из самой дорогой позиции

Другие правила:
1. СКРИНШОТ КАССЫ: брать наименование и итоговую сумму по позиции.
2. ФОТО ЭТИКЕТКИ: брать наименование с этикетки включая размер и артикул (если есть), цена будет в подписи к фото, qty=1.
3. Если на чеке несколько товаров — вернуть массив из нескольких объектов.
4. Количество всегда 1, если не указано иное.
5. Дату НЕ включай — она передаётся отдельно.{caption_part}{date_part}

Извлеки данные о продажах из этого изображения (или двух фото одного чека)."""

def extract_sales_from_images(images: list[tuple[bytes, str]], current_date: str = "") -> list[dict]:
    """images — список (image_bytes, caption)"""
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
# Буфер для альбомов (media_group): media_group_id -> {photos, first_msg, date, task}
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
    """Обрабатываем одно или несколько фото, вносим в таблицу."""
    try:
        items = extract_sales_from_images(images, current_date)

        if not items:
            await reply_msg.reply_text("❌ Не удалось распознать товары на изображении.")
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
        await reply_msg.reply_text("❌ Ошибка распознавания. Попробуй ещё раз или добавь вручную.")
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

    # Если фото часть альбома (media_group) — собираем все фото вместе
    if msg.media_group_id:
        group_id = msg.media_group_id
        if group_id not in media_group_buffer:
            media_group_buffer[group_id] = {
                "photos": [],
                "first_msg": msg,
                "date": current_date,
            }
            # Запускаем отложенную обработку через 3 секунды
            async def process_group(gid=group_id):
                await asyncio.sleep(3)
                if gid not in media_group_buffer:
                    return
                group = media_group_buffer.pop(gid)
                await group["first_msg"].reply_text(f"⏳ Распознаю продажу ({len(group['photos'])} фото)...")
                await process_images(group["photos"], group["date"], group["first_msg"])
            asyncio.create_task(process_group())

        media_group_buffer[group_id]["photos"].append((image_bytes, caption))
        return

    # Одиночное фото — обрабатываем сразу
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
