import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

bot = Bot(token=TELEGRAM_TOKEN)

# خادم وهمي لإرضاء منصة Render مجاناً
class DummyServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Gold Bot is Running 24/7!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), DummyServer)
    server.serve_forever()

async def send_signal(message_text):
    await bot.send_message(chat_id=CHAT_ID, text=message_text, parse_mode="Markdown")

async def main():
    test_msg = (
        "🚀 **توصية جديدة من المحلل الآلي**\n\n"
        "🔹 **الزوج:** XAUUSD (الذهب)\n"
        "🔹 **نوع الأمر:** Sell Stop @ 4385\n"
        "🎯 **الهدف الأول:** 4350\n"
        "🛑 **وقف الخسارة:** 4422\n\n"
        "⚠️ _تنبيه تجريبي: تم الربط بنجاح على الخطة المجانية 100%!_"
    )
    await send_signal(test_msg)

if __name__ == "__main__":
    # تشغيل الخادم الوهمي في الخلفية
    threading.Thread(target=run_dummy_server, daemon=True).start()
    asyncio.run(main())
