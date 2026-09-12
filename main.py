import os
import time
import requests
from datetime import datetime
from flask import Flask
import threading

# استيراد مكتبة تليجرام بالطريقة الصحيحة المتوافقة مع Render
try:
    import telebot
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
except ImportError:
    import sys
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyTelegramBotAPI"])
    import telebot
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================= ================= =================
# 1. الإعدادات والمتغيرات العامة (Bot Configuration)
# ================= ================= =================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "YOUR_TWELVEDATA_KEY")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
app = Flask(__name__)

USER_SETTINGS = {
    "account_balance_cents": 200000,
    "risk_percentage": 0.5,
    "news_filter_active": True,
    "send_reports": True
}

# ================= ================= =================
# 2. خادم الإيقاظ (Flask Server)
# ================= ================= =================

@app.route('/')
def home():
    return "Pro Scalper M15 Gold Bot is Live and Active!", 200

def run_flask():
    app.run(host='0.0.0.0', port=10000)

# ================= ================= =================
# 3. فحص عطلة نهاية الأسبوع والأخبار
# ================= ================= =================

def is_market_closed():
    """حظر التداول يومي السبت والأحد"""
    weekday = datetime.utcnow().weekday()
    return weekday in [5, 6]

def is_high_impact_news_near():
    if not USER_SETTINGS["news_filter_active"]:
        return False, ""
    try:
        url = "https://nfp.ourforecast.com/api/v1/events"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            events = response.json()
            now = datetime.utcnow()
            for event in events:
                if event.get("currency") == "USD" and event.get("impact") == "High":
                    event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00")).replace(tzinfo=None)
                    if abs((event_time - now).total_seconds()) <= 1800:
                        return True, f"{event.get('title')} ({event_time.strftime('%H:%M')} UTC)"
    except Exception as e:
        print(f"News API Error: {e}")
    return False, ""

# ================= ================= =================
# 4. حساب اللوت الذكي (Lot Calculator)
# ================= ================= =================

def calculate_recommended_lot(entry_price, stop_loss_price):
    try:
        balance = USER_SETTINGS["account_balance_cents"]
        risk_pct = USER_SETTINGS["risk_percentage"]
        risk_amount_cents = balance * (risk_pct / 100.0)
        pips_at_risk = abs(entry_price - stop_loss_price)
        if pips_at_risk == 0:
            return 0.10
        pip_value_per_cent_lot = 10.0
        raw_lot = risk_amount_cents / (pips_at_risk * pip_value_per_cent_lot)
        return max(0.01, round(raw_lot, 2))
    except Exception as e:
        print(f"Error calculating lot: {e}")
        return 0.10

# ================= ================= =================
# 5. تحليل السوق وقراءة الأسعار (Market Analysis)
# ================= ================= =================

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return round(ema, 2)

def analyze_gold_market():
    if is_market_closed():
        print("⏸️ السوق مغلق (عطلة نهاية الأسبوع). لا توجد عمليات تحليل.")
        return None

    has_news, news_title = is_high_impact_news_near()
    if has_news:
        print(f"⚠️ متوقف بسبب خبر: {news_title}")
        return None

    url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=15min&outputsize=60&apikey={TWELVE_DATA_API_KEY}"
    try:
        res = requests.get(url, timeout=10).json()
        if "values" not in res:
            return None
        
        candles = list(reversed(res["values"]))
        closes = [float(c["close"]) for c in candles]
        
        current_price = closes[-1]
        rsi = calculate_rsi(closes, 14)
        ema20 = calculate_ema(closes, 20)
        ema50 = calculate_ema(closes, 50)
        
        signal_type = None
        if ema20 > ema50 and 30 <= rsi <= 75:
            signal_type = "BUY"
            entry = current_price + 0.30
            sl = entry - 1.50
            tp = entry + 2.25
        elif ema20 < ema50 and 25 <= rsi <= 70:
            signal_type = "SELL"
            entry = current_price - 0.30
            sl = entry + 1.50
            tp = entry - 2.25

        if signal_type:
            recommended_lot = calculate_recommended_lot(entry, sl)
            return {
                "type": signal_type,
                "price": round(entry, 2),
                "sl": round(sl, 2),
                "tp": round(tp, 2),
                "rsi": rsi,
                "lot": recommended_lot
            }
    except Exception as e:
        print(f"Market Analysis Error: {e}")
        
    return None

# ================= ================= =================
# 6. لوحة الإعدادات وتليجرام (Telegram Commands)
# ================= ================= =================

def build_settings_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    
    news_btn = "🟢 مفعل" if USER_SETTINGS["news_filter_active"] else "🔴 معطل"
    kb.add(InlineKeyboardButton(f"📰 فلتر الأخبار الاقتصادية: {news_btn}", callback_data="toggle_news"))
    
    risk_text = f"🎯 نسبة المخاطرة الحالية: {USER_SETTINGS['risk_percentage']}%"
    kb.add(InlineKeyboardButton(risk_text, callback_data="change_risk"))
    
    bal_text = f"💰 رصيد الحساب: {USER_SETTINGS['account_balance_cents']} سنت"
    kb.add(InlineKeyboardButton(bal_text, callback_data="change_balance"))
    
    rep_btn = "🟢 شغال" if USER_SETTINGS["send_reports"] else "🔴 متوقف"
    kb.add(InlineKeyboardButton(f"📊 التقارير الدورية: {rep_btn}", callback_data="toggle_reports"))
    
    return kb

@bot.message_handler(commands=['start', 'settings'])
def send_settings(message):
    bot.send_message(
        message.chat.id,
        "⚙️ **لوحة التحكم بالنظام المطور (XAUUSD)**\n\nاضغط على الأزرار للتعديل المباشر:",
        parse_mode="Markdown",
        reply_markup=build_settings_keyboard()
    )

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if call.data == "toggle_news":
        USER_SETTINGS["news_filter_active"] = not USER_SETTINGS["news_filter_active"]
    elif call.data == "toggle_reports":
        USER_SETTINGS["send_reports"] = not USER_SETTINGS["send_reports"]
    elif call.data == "change_risk":
        current = USER_SETTINGS["risk_percentage"]
        USER_SETTINGS["risk_percentage"] = 1.0 if current == 0.5 else (2.0 if current == 1.0 else 0.5)
    elif call.data == "change_balance":
        current = USER_SETTINGS["account_balance_cents"]
        if current == 200000:
            USER_SETTINGS["account_balance_cents"] = 300000
        elif current == 300000:
            USER_SETTINGS["account_balance_cents"] = 100000
        else:
            USER_SETTINGS["account_balance_cents"] = 200000

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_settings_keyboard())
    except:
        pass

# ================= ================= =================
# 7. الحلقة الرئيسية (Scanner Loop)
# ================= ================= =================

def market_scanner_loop():
    while True:
        try:
            signal = analyze_gold_market()
            if signal:
                msg = (
                    f"🚀 **إشارة سكالبينج جديدة على الذهب (XAUUSD)**\n\n"
                    f"🔹 **النوع:** {signal['type']} STOP\n"
                    f"📍 **سعر الدخول:** {signal['price']}\n"
                    f"🛑 **وقف الخسارة (SL):** {signal['sl']}\n"
                    f"🎯 **الهدف (TP):** {signal['tp']}\n\n"
                    f"📊 **اللوت المقترح (حساب سنت):** `{signal['lot']}`\n"
                    f"⚖️ **المخاطرة المحددة:** {USER_SETTINGS['risk_percentage']}%\n"
                    f"📈 **مؤشر RSI:** {signal['rsi']}"
                )
                bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        except Exception as e:
            print(f"Scanner Loop Error: {e}")
        
        time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=market_scanner_loop, daemon=True).start()
    bot.infinity_polling()
