import os
import time
import asyncio
from threading import Thread
from flask import Flask
from telegram import Bot
import yfinance as yf
import pandas as pd

app = Flask(__name__)

@app.route('/')
def home():
    return "Professional Gold Trading Bot is Live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# متغيرة لتثبيت الصفقة الحالية وعدم تغييرها مع كل حركة للسعر
active_order = None

def get_pro_gold_signal():
    global active_order
    try:
        # جلب بيانات الذهب بفريم 15 دقيقة لصفقات أقوى واحترافية
        ticker = yf.Ticker("GC=F")
        df = ticker.history(period="5d", interval="15m")
        
        if df.empty or len(df) < 50:
            return None, "بيانات غير كافية"

        # المؤشرات الفنية الأساسية
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
        
        # حساب RSI
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        latest = df.iloc[-1]
        current_price = round(latest['Close'], 2)
        
        # حساب أعلى وأقل سعر لآخر 4 شمعات (ساعة كاملة) لتحديد كسر قوي
        last_4_high = df['High'].iloc[-5:-1].max()
        last_4_low = df['Low'].iloc[-5:-1].min()

        # 1. إذا كانت هناك صفقة معلقة حالية، نتحقق فقط من السعر دون تغيير أرقام الصفقة
        if active_order is not None:
            # إذا وصل السعر للهدف أو الستوب يتم إلغاء الصفقة لتوليد غيرها
            if active_order['type'] == 'Buy Stop' and current_price >= active_order['tp']:
                active_order = None # تحقق الهدف
            elif active_order['type'] == 'Sell Stop' and current_price <= active_order['tp']:
                active_order = None # تحقق الهدف
            elif active_order['type'] == 'Buy Stop' and current_price <= active_order['sl']:
                active_order = None # ضرب الستوب
            elif active_order['type'] == 'Sell Stop' and current_price >= active_order['sl']:
                active_order = None # ضرب الستوب
            
            return active_order, current_price

        # 2. توليد صفقة جديدة تثبت أرقامها تماماً
        ema20 = latest['EMA20']
        ema50 = latest['EMA50']
        rsi = latest['RSI']

        # شرط Buy Stop قوي (الاتجاه صاعد + RSI ممتازة + كسر قمة 1 ساعة)
        if ema20 > ema50 and 50 < rsi < 68:
            entry = round(last_4_high + 1.20, 2)
            active_order = {
                "type": "Buy Stop 🟢",
                "entry": entry,
                "tp": round(entry + 10.0, 2),  # هدف قوي: 10 دولار (100 نقطة)
                "sl": round(entry - 5.0, 2),   # ستوب: 5 دولار (50 نقطة)
                "rsi": round(rsi, 1)
            }

        # شرط Sell Stop قوي (الاتجاه هابط + RSI ممتازة + كسر قاع 1 ساعة)
        elif ema20 < ema50 and 32 < rsi < 50:
            entry = round(last_4_low - 1.20, 2)
            active_order = {
                "type": "Sell Stop 🔴",
                "entry": entry,
                "tp": round(entry - 10.0, 2),  # هدف قوي: 10 دولار (100 نقطة)
                "sl": round(entry + 5.0, 2),   # ستوب: 5 دولار (50 نقطة)
                "rsi": round(rsi, 1)
            }

        return active_order, current_price

    except Exception as e:
        print(f"Error: {e}")
        return None, 0

async def main_loop():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    bot = Bot(token=token)

    while True:
        order, current_price = get_pro_gold_signal()
        
        if order:
            msg = (
                f"🏆 **صفقة ذهب احترافية معلقة (XAUUSD)**\n"
                f"⏱ **الفريم المستخدم:** 15 دقيقة (M15)\n\n"
                f"📍 **السعر الحالي بالسوق:** {current_price}\n"
                f"ريقة **نوع الأمر:** {order['type']}\n"
                f"🎯 **سعر الدخول الثابت (Entry):** {order['entry']}\n"
                f"🟢 **الهدف الثابت (TP):** {order['tp']}\n"
                f"🔴 **وقف الخسارة الثابت (SL):** {order['sl']}\n\n"
                f"📌 *تأكيد: هذه الصفقة ثابته ومستمرة حتى التفعيل أو الضرب.*"
            )
            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                print(f"Send Error: {e}")
        else:
            print("السوق في حالة تذبذب - لا توجد فرصة احترافية حالياً.")

        await asyncio.sleep(300) # فحص وتأكيد كل 5 دقائق

if __name__ == "__main__":
    Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main_loop())
