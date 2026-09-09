import os
import time
import asyncio
import requests
from threading import Thread
from flask import Flask
from telegram import Bot

app = Flask(__name__)

@app.route('/')
def home():
    return "ATR Dynamic Gold Trading Bot is Live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

active_order = None

# حساب مؤشر ATR لحساب مسافات دخول وستوب ديناميكية
def calculate_atr(highs, lows, closes, window=14):
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)
    return sum(tr_list[-window:]) / window if len(tr_list) >= window else 3.5

# حساب RSI
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

# حساب EMA
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
        print("Error: TWELVE_DATA_API_KEY is missing!")
        return None, 0, 0, 0, 0

    try:
        # 1. جلب بيانات الذهب الفوري (Spot XAU/USD)
        url_spot = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=15min&outputsize=40&apikey={api_key}"
        res_spot = requests.get(url_spot).json()

        # 2. جلب بيانات العقود الآجلة (Futures MGC/GC)
        url_futures = f"https://api.twelvedata.com/time_series?symbol=MGC&interval=15min&outputsize=40&apikey={api_key}"
        res_futures = requests.get(url_futures).json()

        if "values" not in res_spot:
            print(f"Twelve Data Error: {res_spot.get('message', 'No Spot Data')}")
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

        # حساب الفارق والتذبذب
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

        # أعلى قمة وأقل قاع لآخر 3 شمعات
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

        # -------------------------------------------------------------
        # الشروط الجديدة الديناميكية باستخدام ATR
        # -------------------------------------------------------------

        # شرط Buy Stop الديناميكي
        if ema20 > ema50 and rsi < 68 and (basis_expansion >= 0.15 or volume_surging):
            # وضع الدخول فوق القمة القريبة بمسافة تنفس (0.3 * ATR)
            proposed_entry = round(recent_high + (0.3 * atr), 2)
            
            # التأكد من عدم البُعد المفرط عن السعر المباشر
            if proposed_entry <= (spot_price + (1.2 * atr)):
                sl_price = round(recent_low - (0.5 * atr), 2)
                risk_distance = proposed_entry - sl_price
                
                # إتاحة مسافة وقف خسارة لا تقل عن 4.5$
                if risk_distance < 4.5:
                    sl_price = round(proposed_entry - 5.0, 2)
                    risk_distance = 5.0

                active_order = {
                    "type": "Buy Stop",
                    "entry": proposed_entry,
                    "tp": round(proposed_entry + (2.0 * risk_distance), 2), # R:R = 1:2
                    "sl": sl_price,
                    "rsi": rsi,
                    "reason": "تدفق سيولة شرائية - مسافات معتمدة على تذبذب الذهب الحقيقي (ATR)"
                }

        # شرط Sell Stop الديناميكي
        elif ema20 < ema50 and rsi > 32 and (basis_expansion <= -0.15 or volume_surging):
            proposed_entry = round(recent_low - (0.3 * atr), 2)
            
            if proposed_entry >= (spot_price - (1.2 * atr)):
                sl_price = round(recent_high + (0.5 * atr), 2)
                risk_distance = sl_price - proposed_entry
                
                if risk_distance < 4.5:
                    sl_price = round(proposed_entry + 5.0, 2)
                    risk_distance = 5.0

                active_order = {
                    "type": "Sell Stop",
                    "entry": proposed_entry,
                    "tp": round(proposed_entry - (2.0 * risk_distance), 2), # R:R = 1:2
                    "sl": sl_price,
                    "rsi": rsi,
                    "reason": "تسارع ضغوط بيعية - مسافات معتمدة على تذبذب الذهب الحقيقي (ATR)"
                }

        return active_order, spot_price, basis_current, rsi, atr

    except Exception as e:
        print(f"ATR Logic Error: {e}")
        return None, 0, 0, 0, 0

async def main_loop():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID environment variables!")
        return

    bot = Bot(token=token)

    while True:
        order, spot_price, basis, rsi, atr = get_institutional_gold_signal()
        
        if order:
            emoji = "🟢" if order['type'] == "Buy Stop" else "🔴"
            msg = (
                f"🚨 **إشارة تداول متوازنة (Dynamic ATR Protection)**\n"
                f"⏱ **الفريم:** 15 دقيقة (M15) | **الرمز:** XAUUSD\n\n"
                f"📊 **سعر المنصة المباشر:** {spot_price}\n"
                f"📏 **تذبذب السوق (ATR):** {round(atr, 2)}\n"
                f"📐 **فارق الآجل/الفوري (Basis):** {basis}\n"
                f"📈 **مؤشر RSI:** {rsi}\n\n"
                f"{emoji} **نوع الأمر المعلق:** {order['type']}\n"
                f"🎯 **سعر الدخول (Entry):** {order['entry']}\n"
                f"🟢 **الهدف (TP):** {order['tp']}\n"
                f"🔴 **وقف الخسارة (SL):** {order['sl']}\n\n"
                f"📌 *ملاحظة: تم توسيع وقف الخسارة تلقائياً ليستوعب تذبذب الذهب ويمنع ضرب الستوب المبكر.*"
            )

            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                print(f"[{time.strftime('%H:%M:%S')}] ATR-adjusted signal sent successfully.")
            except Exception as e:
                print(f"Send Error: {e}")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] Monitoring market structure...")

        await asyncio.sleep(300)

if __name__ == "__main__":
    Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main_loop())
