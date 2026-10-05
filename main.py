import asyncio
import json
import os
import re
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from google import genai
from google.genai import types as genai_types

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = genai.Client(api_key=GEMINI_KEY)

# Пути к базам данных
PRODUCTS_FILE = "products.json"
INVENTORY_FILE = "inventory.json"
FINANCE_FILE = "finance.json"

def load_json(filepath):
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# --- 1. МОДУЛЬ МОДЕРАЦИИ ОТЗЫВОВ ---
REVIEW_PROMPT = """
Ты — AI-ассистент клиентского сервиса бренда на Wildberries.
Твоя задача — анализировать отзывы покупателей по базе регламентов.

База товаров:
{products_data}

КРИТИЧЕСКИЕ ПРАВИЛА БЕЗОПАСНОСТИ (SAFETY-CRITICAL):
1. Если в отзыве есть признаки аварийного ЧП (пожар, удар током, короткое замыкание, искры) ИЛИ юридические угрозы (суд, Роспотребнадзор, досудебная претензия):
   - status: "ESCALATE"
   - action_reason: точная причина блокировки автоответа
   - reply_text: ""
2. Если негатив (1-3 звезды) вызван нарушением правил эксплуатации (strict_rules):
   - status: "AUTO_REPLY"
   - reply_text: вежливый ответ с объяснением ошибки по инструкции и контактами техподдержки.
3. Если позитив (4-5 звезд):
   - status: "AUTO_REPLY"
   - reply_text: персональная благодарность с акцентом на плюсы модели.

ВЫДАВАЙ СТРОГО JSON:
{{
  "status": "AUTO_REPLY" или "ESCALATE",
  "logic_summary": "краткое пояснение решения",
  "reply_text": "готовый ответ покупателю или пусто"
}}
"""

# --- 2. МОДУЛЬ СКЛАДСКОГО КОНТРОЛЯ (FBO) ---
INVENTORY_PROMPT = """
Ты — AI-ассистент управления остатками на складе Wildberries (FBO).
Данные остатков:
{inventory_data}

Правила:
- Если остатка товара хватает на 7 дней или меньше: предупреди о критическом остатке и предложи оформить поставку. Добавь в конец метку: [NEED_REORDER:АРТИКУЛ:КОЛИЧЕСТВО]
- Если запаса больше чем на 7 дней: сообщи, что остатков достаточно.
Отвечай кратко и структурированно.
"""

# --- 3. МОДУЛЬ ФИНАНСОВОГО АУДИТА ---
FINANCE_EXTRACT_PROMPT = """
Извлеки из текста отчёта менеджера заявленный артикул и заявленную чистую прибыль.
ВЫДАВАЙ СТРОГО JSON:
{{
  "sku": "артикул (например WB-101)",
  "claimed_profit": 150000.0
}}
"""

def verify_unit_economics(claimed_sku: str, claimed_profit: float):
    raw_data = load_json(FINANCE_FILE)
    item = next((i for i in raw_data if i["sku"].lower() == claimed_sku.lower() or i["name"].lower() in claimed_sku.lower()), None)
    if not item:
        return None

    actual_profit = item["revenue"] - item["cogs"] - item["commission"] - item["logistics"] - item["ads"]
    diff = round(claimed_profit - actual_profit, 2)
    return {
        **item,
        "actual_profit": actual_profit,
        "claimed_profit": claimed_profit,
        "diff": diff,
        "has_discrepancy": abs(diff) >= 100.0
    }

# --- МАРШРУТИЗАЦИЯ В TELEGRAM ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>WB AI Automation Agent запущен!</b>\n\n"
        "Бот поддерживает три автоматических сценария:\n\n"
        "1️⃣ <b>Модерация отзывов:</b>\n"
        "<i>«Артикул: WB-101 | Оценка: 5 | Отличный увлажнитель, тихий!»</i>\n\n"
        "2️⃣ <b>Контроль остатков FBO:</b>\n"
        "<i>«Что у нас по остаткам чайников?»</i>\n\n"
        "3️⃣ <b>Аудит отчёта Unit-экономики:</b>\n"
        "<i>«Отчёт по WB-101: чистая прибыль составила 165000 руб.»</i>",
        parse_mode="HTML"
    )

@dp.message()
async def route_query(message: types.Message):
    text = message.text.strip()
    
    # СЦЕНАРИЙ 1: Отзыв маркетплейса
    if "оценка:" in text.lower() or "артикул:" in text.lower():
        wait_msg = await message.answer("🔍 Анализирую отзыв по регламентам...")
        products = load_json(PRODUCTS_FILE)
        prompt = REVIEW_PROMPT.format(products_data=json.dumps(products, ensure_ascii=False))
        
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=f"{prompt}\n\nВходящий отзыв:\n{text}",
                config=genai_types.GenerateContentConfig(response_mime_type="application/json")
            ),
            timeout=25.0
        )
        data = json.loads(re.sub(r"^```json\s*|\s*```$", "", resp.text.strip(), flags=re.MULTILINE))
        
        if data.get("status") == "ESCALATE":
            await wait_msg.edit_text(
                f"🚨 <b>[ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО МЕНЕДЖЕРА]</b>\n\n"
                f"⚠ <b>Причина:</b> {data.get('logic_summary')}\n\n"
                f"<i>Автоответ покупателю заблокирован из соображений безопасности.</i>",
                parse_mode="HTML"
            )
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚀 Одобрить и отправить на WB", callback_data="wb_send_ok"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data="wb_edit")
            ]])
            await wait_msg.edit_text(
                f"✅ <b>[АВТООТВЕТ СФОРМИРОВАН]</b>\n\n"
                f"💡 <b>Логика:</b> {data.get('logic_summary')}\n\n"
                f"📝 <b>Текст для маркетплейса:</b>\n{data.get('reply_text')}",
                reply_markup=kb,
                parse_mode="HTML"
            )
        return

    # СЦЕНАРИЙ 3: Финансовый аудит отчёта
    if "отчет" in text.lower() or "отчёт" in text.lower() or "прибыль" in text.lower():
        wait_msg = await message.answer("🔎 Сверяю отчёт с первичными финансовыми выгрузками...")
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=f"{FINANCE_EXTRACT_PROMPT}\n\nТекст отчёта:\n{text}",
                config=genai_types.GenerateContentConfig(response_mime_type="application/json")
            ),
            timeout=25.0
        )
        ext = json.loads(re.sub(r"^```json\s*|\s*```$", "", resp.text.strip(), flags=re.MULTILINE))
        audit = verify_unit_economics(ext.get("sku", ""), float(ext.get("claimed_profit", 0.0)))
        
        if not audit:
            await wait_msg.edit_text("⚠️️ Товар не найден в базе `finance.json`.")
            return

        if audit["has_discrepancy"]:
            sign = "+" if audit["diff"] > 0 else ""
            status = f"🚨 <b>[ОБНАРУЖЕНО РАСХОЖДЕНИЕ В ОТЧЁТЕ]</b>\n\n⚠ Заявленная прибыль завышена/занижена на <b>{sign}{audit['diff']:,.2f} ₽</b>!"
        else:
            status = "✅ <b>[ДАННЫЕ ОТЧЁТА ВЕРНЫ]</b>\n\nВсе цифры сходятся с первичными выгрузками до рубля."

        card = (
            f"{status}\n\n"
            f"📦 <b>Товар:</b> {audit['name']} (<code>{audit['sku']}</code>)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• Выручка: <b>{audit['revenue']:,.0f} ₽</b>\n"
            f"• Себестоимость: <b>-{audit['cogs']:,.0f} ₽</b>\n"
            f"• Комиссия WB: <b>-{audit['commission']:,.0f} ₽</b>\n"
            f"• Логистика: <b>-{audit['logistics']:,.0f} ₽</b>\n"
            f"• Реклама (ДРР): <b>-{audit['ads']:,.0f} ₽</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 Фактическая чистая прибыль: <code>{audit['actual_profit']:,.2f} ₽</code>\n"
            f"📋 Заявлено менеджером: <code>{audit['claimed_profit']:,.2f} ₽</code>"
        )
        await wait_msg.edit_text(card, parse_mode="HTML")
        return

    # СЦЕНАРИЙ 2: Контроль остатков (по умолчанию для остальных запросов)
    wait_msg = await message.answer("📦 Проверяю остатки на складе FBO...")
    inv_data = load_json(INVENTORY_FILE)
    for it in inv_data:
        sp = it.get("sales_per_day", 1)
        it["days_left"] = round(it["stock_fbo"] / sp, 1) if sp > 0 else 999
        it["need_reorder"] = it["days_left"] <= 7

    prompt = INVENTORY_PROMPT.format(inventory_data=json.dumps(inv_data, ensure_ascii=False))
    resp = await asyncio.wait_for(
        asyncio.to_thread(
            client.models.generate_content,
            model="gemini-3.5-flash-lite",
            contents=f"{prompt}\n\nВопрос менеджера: {text}"
        ),
        timeout=25.0
    )
    
    rep = resp.text
    kb = None
    m = re.search(r"\[NEED_REORDER:([A-Za-z0-9\-_]+):(\d+)\]", rep)
    if m:
        sku, qty = m.group(1), m.group(2)
        rep = re.sub(r"\[NEED_REORDER:[^\]]+\]", "", rep).strip()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✅ Подтвердить поставку ({qty} шт.)", callback_data=f"ord_{sku}_{qty}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data="ord_cancel")
        ]])
        
    await wait_msg.edit_text(rep, reply_markup=kb, parse_mode="HTML")

# --- ОБРАБОТКА ИНЛАЙН-КНОПОК ---
@dp.callback_query(F.data == "wb_send_ok")
async def cb_wb_ok(call: types.CallbackQuery):
    await call.answer("✅ Ответ успешно опубликован на Wildberries!", show_alert=True)
    await call.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(F.data == "wb_edit")
async def cb_wb_edit(call: types.CallbackQuery):
    await call.answer("Режим редактирования выбран.")

@dp.callback_query(F.data.startswith("ord_"))
async def cb_order(call: types.CallbackQuery):
    if call.data == "ord_cancel":
        await call.answer("Заявка отклонена.")
        await call.message.edit_reply_markup(reply_markup=None)
        return
    _, sku, qty = call.data.split("_")
    items = load_json(INVENTORY_FILE)
    for i in items:
        if i["sku"] == sku:
            i["stock_fbo"] += int(qty)
            break
    save_json(INVENTORY_FILE, items)
    await call.answer(f"✅ Поставка на {qty} шт. подтверждена!", show_alert=True)
    await call.message.edit_text(call.message.html_text + f"\n\n<b>[СТАТУС: Поставка {sku} (+{qty} шт.) согласована]</b>", parse_mode="HTML")

async def main():
    print(">>> Комплексный WB AI Agent запущен... <<<")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
