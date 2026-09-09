import os
import asyncio
from threading import Thread
from flask import Flask
from telegram import Bot

# --- خادم ويب بسيط لإرضاء Render على الخطة المجانية ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- كود البوت الخاص بك ---
async def send_telegram_signal():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    
    if not token or not chat_id:
        print("Error: TELEGRAM_TOKEN or CHAT_ID is missing.")
        return

    bot = Bot(token=token)
    message = (
        "🚀 توصية جديدة من المحلل الآلي\n\n"
        "🔷 الزوج: XAUUSD (الذهب)\n"
        "🔷 نوع الأمر: Sell Stop @ 4385\n"
        "🎯 الهدف الأول: 4350\n"
        "🛑 وقف الخسارة: 4422\n\n"
        "⚠️ تنبيه تجريبي: تم الربط بنجاح على الخطة المجانية 100%!"
    )
    
    await bot.send_message(chat_id=chat_id, text=message)
    print("Message sent successfully!")

if __name__ == "__main__":
    # تشغيل خادم الويب في المسار الخفي
    server_thread = Thread(target=run_web_server)
    server_thread.daemon = True
    server_thread.start()

    # تشغيل البوت وإرسال الرسالة
    asyncio.run(send_telegram_signal())

    # إبقاء السكربت يعمل بشكل مستمر
    while True:
        pass
