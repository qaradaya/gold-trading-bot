import os
import time
import asyncio
from threading import Thread
from flask import Flask
from telegram import Bot
import yfinance as yf
import pandas as pd

# --- خادم ويب بسيط لإرضاء Render على الخطة المجانية ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Gold Trading Bot is Active & Running!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- دالة التحليل الفني للذهب ---
def analyze_gold():
    try:
        # جلب بيانات الذهب (XAUUSD) بفريم 5 دقائق
        ticker = yf.Ticker("GC=F") # عقود الذهب الآجلة كممثل للذهب
        df = ticker.history(period="1d", interval="5m")
        
        if df.empty or len(df) < 50:
            return None, "لا توجد بيانات كافية حالياً."

        # حساب المؤشرات الفنية
        df['SMA20'] = df['Close'].rolling(window=20).mean()
        df['SMA50'] = df['Close'].rolling(window=50).mean()
        
        # حساب RSI (14)
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        latest = df.iloc[-1]
        price = round(latest['Close'], 2)
        sma20 = latest['SMA20']
        sma50 = latest['SMA50']
        rsi = latest['RSI']

        # منطق الإشارات الفنية
        signal = None
        if sma20 > sma50 and rsi < 70:
            signal = "BUY"
            entry = price
            tp = round(price + 8.0, 2)
            sl = round(price - 5.0, 2)
        elif sma20 < sma50 and rsi > 30:
            signal = "SELL"
            entry = price
            tp = round(price - 8.0, 2)
            sl = round(price + 5.0, 2)

        return signal, {
            "price": price,
            "entry": entry if signal else price,
            "tp": tp if signal else 0,
            "sl": sl if signal else 0,
            "rsi": round(rsi, 2)
        }
    except Exception as e:
        print(f"Error in technical analysis: {e}")
        return None, None

# --- الحلقة الرئيسية للإرسال المجدول كل 5 دقائق ---
async def trading_loop():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    
    if not token or not chat_id:
        print("Error: TELEGRAM_TOKEN or CHAT_ID is missing.")
        return

    bot = Bot(token=token)
    last_signal = None

    while True:
        signal, data = analyze_gold()
        
        if signal and data:
            action_text = "شراء 🟢 (BUY)" if signal == "BUY" else "بيع 🔴 (SELL)"
            
            # في حال وجود فرصة مستمرة أو جديدة
            message = (
                f"🚀 **توصية وتحليل الذهب (XAUUSD)**\n\n"
                f"🔷 **السعر الحالي:** {data['price']}\n"
                f"🔷 **نوع الأمر:** {action_text}\n"
                f"🎯 **الهدف (TP):** {data['tp']}\n"
                f"🛑 **وقف الخسارة (SL):** {data['sl']}\n"
                f"📊 **مؤشر RSI:** {data['rsi']}\n\n"
                f"⏰ *تنبيه دوري: الفرصة ما زالت متاحة وتحت المتابعة (تحديث كل 5 دقائق)*"
            )
            
            try:
                await bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")
                print(f"[{time.strftime('%H:%M:%S')}] Message sent successfully!")
            except Exception as e:
                print(f"Failed to send message: {e}")

        else:
            print(f"[{time.strftime('%H:%M:%S')}] No clear signal found. Waiting...")

        # الانتظار لمدة 5 دقائق (300 ثانية) قبل التحليل القادم
        await asyncio.sleep(300)

if __name__ == "__main__":
    # تشغيل خادم الويب
    server_thread = Thread(target=run_web_server)
    server_thread.daemon = True
    server_thread.start()

    # تشغيل حلقة التداول والتحليل الفني
    asyncio.run(trading_loop())
