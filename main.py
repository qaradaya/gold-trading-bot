import os
import time
import asyncio
from threading import Thread
from flask import Flask
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
import yfinance as yf
import pandas as pd

app = Flask(__name__)

@app.route('/')
def home():
    return "Professional Gold Trading Bot & MT5 Bridge is Live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

active_order = None

def get_pro_gold_signal():
    global active_order
    try:
        ticker = yf.Ticker("GC=F")
        df = ticker.history(period="2d", interval="15m")
        
        if df.empty or len(df) < 30:
            print("yfinance returned empty data")
            return None, 0

        # حساب المؤشرات الفنية
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
        
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        latest = df.iloc[-1]
        current_price = round(latest['Close'], 2)
        
        last_4_high = df['High'].iloc[-5:-1].max()
        last_4_low = df['Low'].iloc[-5:-1].min()

        # إرجاع الصفقة الحالية إذا كانت قائمة
        if active_order is not None:
            if active_order['type'] == 'Buy Stop' and current_price >= active_order['tp']:
                active_order = None
            elif active_order['type'] == 'Sell Stop' and current_price <= active_order['tp']:
                active_order = None
            elif active_order['type'] == 'Buy Stop' and current_price <= active_order['sl']:
                active_order = None
            elif active_order['type'] == 'Sell Stop' and current_price >= active_order['sl']:
                active_order = None
            
            return active_order, current_price

        ema20 = latest['EMA20']
        ema50 = latest['EMA50']
        rsi = latest['RSI']

        # شرط دخول Buy Stop
        if ema20 >= ema50:
            entry = round(last_4_high + 0.80, 2)
            active_order = {
                "type": "Buy Stop",
                "entry": entry,
                "tp": round(entry + 8.0, 2),
                "sl": round(entry - 4.0, 2),
                "rsi": round(rsi, 1)
            }
        # شرط دخول Sell Stop
        elif ema20 < ema50:
            entry = round(last_4_low - 0.80, 2)
            active_order = {
                "type": "Sell Stop",
                "entry": entry,
                "tp": round(entry - 8.0, 2),
                "sl": round(entry + 4.0, 2),
                "rsi": round(rsi, 1)
            }

        return active_order, current_price

    except Exception as e:
        print(f"Error analyzing gold data: {e}")
        return None, 0

async def main_loop():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID environment variables!")
        return

    bot = Bot(token=token)

    while True:
        order, current_price = get_pro_gold_signal()
        
        if order:
            emoji = "🟢" if order['type'] == "Buy Stop" else "🔴"
            msg = (
                f"🏆 **توصية ذهب معلقة (XAUUSD)**\n"
                f"⏱ **الفريم:** 15 دقيقة (M15)\n\n"
                f"📍 **السعر الحالي:** {current_price}\n"
                f"{emoji} **نوع الأمر:** {order['type']}\n"
                f"🎯 **سعر الدخول (Entry):** {order['entry']}\n"
                f"🟢 **الهدف (TP):** {order['tp']}\n"
                f"🔴 **وقف الخسارة (SL):** {order['sl']}\n\n"
                f"👇 **اضغط الأسفل للتنفيذ المباشر على MT5:**"
            )
            
            # أزرار التفاعلية لتنفيذ الصفقة بنقرة واحدة
            keyboard = [
                [
                    InlineKeyboardButton("🚀 تنفيذ معلق الآن (Pending)", callback_data=f"EXEC_PENDING_{order['type']}"),
                    InlineKeyboardButton("⚡ تنفيذ بالسعر الحالي (Market)", callback_data="EXEC_MARKET")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown", reply_markup=reply_markup)
                print(f"[{time.strftime('%H:%M:%S')}] Signal sent with MT5 Action buttons.")
            except Exception as e:
                print(f"Send Error: {e}")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] Waiting for technical confirmation...")

        await asyncio.sleep(300)

if __name__ == "__main__":
    Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main_loop())
