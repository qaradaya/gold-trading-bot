import os
import time
import asyncio
import requests
from datetime import datetime
from threading import Thread
from flask import Flask
from telegram import Bot

app = Flask(__name__)

@app.route('/')
def home():
    return "M15 Synchronized Gold Bot is Live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

active_order = None

# دالة الانتظار حتى الإغلاق الفعلي لشمعة M15
async def wait_for_m15_candle_close():
    while True:
        now = datetime.utcnow()
        # فحص إغلاق شمعة 15 دقيقة (عند الدقيقة 00، 15، 30، 45)
        if now.minute % 15 == 0 and now.second < 10:
            break
        await asyncio.sleep(5)

def calculate_atr(highs, lows, closes, window=14):
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)
    return sum(tr_list[-window:]) / window if len(tr_list) >= window else 4.0

def calculate_rsi(closes, window=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        if delta > 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(delta))
    avg_gain = sum(gains[-window:]) / window
    avg_loss = sum(losses[-window:]) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(data, window):
    weights = [2 / (window + 1)]
    ema = [data[0]]
    for price in data[1:]:
        ema.append((price * weights[0]) + (ema[-1] * (1 - weights[0])))
    return ema

def get_institutional_gold_signal():
    global active_order
    api_key = os.environ.get("TWELVE_DATA_API_KEY")
    
    if not api_key:
        print("Error: TWELVE_DATA_API_KEY missing!")
        return None, 0, 0, 0, 0

    try:
        url_spot = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=15min&outputsize=40&apikey={api_key}"
        res_spot = requests.get(url_spot).json()

        url_futures = f"https://api.twelvedata.com/time_series?symbol=MGC&interval=15min&outputsize=40&apikey={api_key}"
        res_futures = requests.get(url_futures).json()

        if "values" not in res_spot:
            print("Twelve Data Error or limit reached.")
            return None, 0, 0, 0, 0

        spot_values = res_spot["values"]
        spot_values.reverse()

        spot_closes = [float(item["close"]) for item in spot_values]
        spot_highs = [float(item["high"]) for item in spot_values]
        spot_lows = [float(item["low"]) for item in spot_values]

        if "values" in res_futures:
            futures_values = res_futures["values"]
            futures_values.reverse()
            futures_closes = [float(item["close"]) for item in futures_values]
            futures_volumes = [float(item.get("volume", 0)) for item in futures_values]
        else:
            futures_closes = spot_closes
            futures_volumes = [100] * len(spot_closes)

        spot_price = round(spot_closes[-1], 2)
        futures_price = round(futures_closes[-1], 2)

        basis_current = round(futures_price - spot_price, 2)
        basis_prev = round(futures_closes[-2] - spot_closes[-2], 2)
        basis_expansion = round(basis_current - basis_prev, 2)

        volume_current = futures_volumes[-1]
        volume_avg = sum(futures_volumes[-5:-1]) / 4 if len(futures_volumes) >= 5 else volume_current
        volume_surging = volume_current > volume_avg

        ema20 = calculate_ema(spot_closes, 20)[-1]
        ema50 = calculate_ema(spot_closes, 50)[-1]
        rsi = calculate_rsi(spot_closes, 14)
        atr = calculate_atr(spot_highs, spot_lows, spot_closes, 14)

        recent_high = max(spot_highs[-4:-1])
        recent_low = min(spot_lows[-4:-1])

        # إدارة الصفقة الحالية
        if active_order is not None:
            if active_order['type'] == 'Buy Stop' and spot_price >= active_order['tp']:
                active_order = None
            elif active_order['type'] == 'Sell Stop' and spot_price <= active_order['tp']:
                active_order = None
            elif active_order['type'] == 'Buy Stop' and spot_price <= active_order['sl']:
                active_order = None
            elif active_order['type'] == 'Sell Stop' and spot_price >= active_order['sl']:
                active_order = None
            
            return active_order, spot_price, basis_current, rsi, atr

        # شرط Buy Stop على إغلاق M15
        if ema20 > ema50 and rsi < 68 and (basis_expansion >= 0.10 or volume_surging):
            proposed_entry = round(max(recent_high, spot_price) + 1.20, 2)
            sl_price = round(proposed_entry - max(4.5, atr * 1.2), 2)
            tp_price = round(proposed_entry + ((proposed_entry - sl_price) * 1.8), 2)

            active_order = {
                "type": "Buy Stop",
                "entry": proposed_entry,
                "tp": tp_price,
                "sl": sl_price,
                "rsi": rsi,
                "reason": "تأكيد إغلاق شمعة M15 مع تدفق سيولة شرائية"
            }

        # شرط Sell Stop على إغلاق M15
        elif ema20 < ema50 and rsi > 32 and (basis_expansion <= -0.10 or volume_surging):
            proposed_entry = round(min(recent_low, spot_price) - 1.20, 2)
            sl_price = round(proposed_entry + max(4.5, atr * 1.2), 2)
            tp_price = round(proposed_entry - ((sl_price - proposed_entry) * 1.8), 2)

            active_order = {
                "type": "Sell Stop",
                "entry": proposed_entry,
                "tp": tp_price,
                "sl": sl_price,
                "rsi": rsi,
                "reason": "تأكيد إغلاق شمعة M15 مع ضغوط بيعية مؤسسية"
            }

        return active_order, spot_price, basis_current, rsi, atr

    except Exception as e:
        print(f"Execution Error: {e}")
        return None, 0, 0, 0, 0

async def main_loop():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID environment variables!")
        return

    bot = Bot(token=token)

    while True:
        # المزامنة مع نهاية شمعة M15
        await wait_for_m15_candle_close()
        
        order, spot_price, basis, rsi, atr = get_institutional_gold_signal()
        
        if order:
            emoji = "🟢" if order['type'] == "Buy Stop" else "🔴"
            msg = (
                f"🚨 **إشارة مؤكدة (M15 Candle Close)**\n"
                f"⏱ **التوقيت:** إغلاق شمعة 15 دقيقة | **الرمز:** XAUUSD\n\n"
                f"📊 **سعر المنصة المباشر:** {spot_price}\n"
                f"📏 **مؤشر ATR:** {round(atr, 2)}\n"
                f"📐 **فارق الآجل/الفوري:** {basis}\n"
                f"📈 **مؤشر RSI:** {rsi}\n\n"
                f"{emoji} **نوع الأمر المعلق:** {order['type']}\n"
                f"🎯 **سعر الدخول (Entry):** {order['entry']}\n"
                f"🟢 **الهدف (TP):** {order['tp']}\n"
                f"🔴 **وقف الخسارة (SL):** {order['sl']}\n\n"
                f"📌 *توصية صادرة فور إغلاق الشمعة.*"
            )

            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                print(f"[{time.strftime('%H:%M:%S')}] M15 Signal sent successfully.")
            except Exception as e:
                print(f"Send Error: {e}")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] M15 candle closed with no signal setup.")

        # انتظار دقيقة لتفادي التكرار في نفس الشمعة
        await asyncio.sleep(60)

if __name__ == "__main__":
    Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main_loop())
