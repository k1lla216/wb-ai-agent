import asyncio
import json
import os
import re
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from google import genai
from google.genai import types as genai_types

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = genai.Client(api_key=GEMINI_KEY)

FINANCE_FILE = "finance.json"

def get_finance():
    with open(FINANCE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def verify_unit_economics(claimed_sku: str, claimed_profit: float):
    """Точный детерминированный пересчет Unit-экономики по формуле."""
    raw_data = get_finance()
    target_item = None
    for item in raw_data:
        if item["sku"].lower() == claimed_sku.lower() or item["name"].lower() in claimed_sku.lower():
            target_item = item
            break
            
    if not target_item:
        return None

    # Формула: Выручка - Себестоимость - Логистика - Комиссия - Реклама
    actual_profit = (
        target_item["revenue"]
        - target_item["cogs"]
        - target_item["logistics"]
        - target_item["commission"]
        - target_item["ads"]
    )
    
    diff = round(claimed_profit - actual_profit, 2)
    has_discrepancy = abs(diff) >= 100.0

    return {
        "sku": target_item["sku"],
        "name": target_item["name"],
        "revenue": target_item["revenue"],
        "cogs": target_item["cogs"],
        "logistics": target_item["logistics"],
        "commission": target_item["commission"],
        "ads": target_item["ads"],
        "actual_profit": actual_profit,
        "claimed_profit": claimed_profit,
        "diff": diff,
        "has_discrepancy": has_discrepancy
    }

SYSTEM_PROMPT = """
Ты — AI-аудитор финансовой отчётности селлера на Wildberries.
Твоя задача — извлечь из текстового отчета менеджера заявленный артикул и заявленную прибыль.

ВЫДАВАЙ СТРОГО JSON:
{
  "sku": "артикул товара (например WB-101)",
  "claimed_profit": 150000.0
}
"""

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "📊 <b>Агент финансового аудита и фактчекинга запущен!</b>\n\n"
        "Отправьте мне отчёт менеджера на сверку с первичными данными.\n\n"
        "<i>Пример отчёта:</i>\n"
        "<code>Отчёт за неделю по артикулу WB-101: выручка отличная, чистая прибыль составила 165 000 руб.</code>",
        parse_mode="HTML"
    )

@dp.message()
async def process_financial_report(message: types.Message):
    wait_msg = await message.answer("🔎 Извлекаю данные и произвожу аудит...")
    
    try:
        # Извлекаем сущности через Gemini
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=f"{SYSTEM_PROMPT}\n\nТекст отчёта менеджера:\n{message.text}",
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                )
            ),
            timeout=25.0
        )
        
        clean_text = re.sub(r"^```json\s*|\s*```$", "", response.text.strip(), flags=re.MULTILINE)
        extracted = json.loads(clean_text)
        
        sku = extracted.get("sku", "")
        claimed_profit = float(extracted.get("claimed_profit", 0.0))
        
        # Сверяем с сырыми данными формулой
        audit = verify_unit_economics(sku, claimed_profit)
        
        if not audit:
            await wait_msg.edit_text(f"⚠️ Товар с артикулом <b>{sku}</b> не найден в сырой финансовой базе данных.", parse_mode="HTML")
            return

        if audit["has_discrepancy"]:
            status_header = "🚨 <b>[ОБНАРУЖЕНО РАСХОЖДЕНИЕ В ОТЧЁТЕ]</b>"
            diff_sign = "+" if audit["diff"] > 0 else ""
            verdict = (
                f"⚠️️ <b>Внимание:</b> Заявленная прибыль завышена/занижена на <b>{diff_sign}{audit['diff']:,.2f} ₽</b>!\n"
                f"Отчёт не сходится с первичными данными маркетплейса."
            )
        else:
            status_header = "✅ <b>[ДАННЫЕ ОТЧЁТА ВЕРНЫ]</b>"
            verdict = "Все цифры сходятся с первичными финансовыми выгрузками до рубля."

        report_card = (
            f"{status_header}\n\n"
            f"📦 <b>Товар:</b> {audit['name']} (<code>{audit['sku']}</code>)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• Выручка: <b>{audit['revenue']:,.0f} ₽</b>\n"
            f"• Себестоимость: <b>-{audit['cogs']:,.0f} ₽</b>\n"
            f"• Комиссия WB: <b>-{audit['commission']:,.0f} ₽</b>\n"
            f"• Логистика: <b>-{audit['logistics']:,.0f} ₽</b>\n"
            f"• Реклама (ДРР): <b>-{audit['ads']:,.0f} ₽</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>Фактическая чистая прибыль:</b> <code>{audit['actual_profit']:,.2f} ₽</code>\n"
            f"📋 <b>Заявлено менеджером:</b> <code>{audit['claimed_profit']:,.2f} ₽</code>\n\n"
            f"{verdict}"
        )
        
        await wait_msg.edit_text(report_card, parse_mode="HTML")

    except asyncio.TimeoutError:
        await wait_msg.edit_text("⏳ Сервер аудита временно не ответил, попробуйте еще раз.")
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка проверки: {str(e)}")

async def main():
    print(">>> Агент финансового аудита запущен... <<<")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())