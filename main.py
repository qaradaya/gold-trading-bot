import os
import asyncio
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

bot = Bot(token=TELEGRAM_TOKEN)

async def send_signal(message_text):
    await bot.send_message(chat_id=CHAT_ID, text=message_text, parse_mode="Markdown")

async def main():
    test_msg = (
        "🚀 **توصية جديدة من المحلل الآلي**\n\n"
        "🔹 **الزوج:** XAUUSD (الذهب)\n"
        "🔹 **نوع الأمر:** Sell Stop @ 4385\n"
        "🎯 **الهدف الأول:** 4350\n"
        "🛑 **وقف الخسارة:** 4422\n\n"
        "⚠️ _تنبيه تجريبي: تم ربط السيرفر بنجاح!_"
    )
    await send_signal(test_msg)

if __name__ == "__main__":
    asyncio.run(main())
