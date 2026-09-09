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
    return "Optimized Institutional Gold Bot is Live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

active_order = None

# حساب مؤشر القوة النسبية RSI
def calculate_rsi(closes, window=14):
    gains = []
    losses = []
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

# حساب المتوسط المتحرك الاسي EMA
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
        print("Error: TWELVE_DATA_API_KEY environment variable is missing!")
        return None, 0, 0, 0

    try:
        # 1. جلب بيانات الذهب الفوري اللحظية (Spot XAU/USD)
        url_spot = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=15min&outputsize=30&apikey={api_key}"
        res_spot = requests.get(url_spot).json()

        # 2. جلب بيانات العقود الآجلة اللحظية (Futures MGC/GC)
        url_futures = f"https://api.twelvedata.com/time_series?symbol=MGC&interval=15min&outputsize=30&apikey={api_key}"
        res_futures = requests.get(url_futures).json()

        if "values" not in res_spot:
            print(f"Twelve Data Error: {res_spot.get('message', 'No Spot Data')}")
            return None, 0, 0, 0

        spot_values = res_spot["values"]
        spot_values.reverse()

        spot_closes = [float(item["close"]) for item in spot_values]

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

        # 1. تحليل الفارق السعري (Basis Expansion)
        basis_current = round(futures_price - spot_price, 2)
        basis_prev = round(futures_closes[-2] - spot_closes[-2], 2)
        basis_expansion = round(basis_current - basis_prev, 2)

        # 2. تحليل الأحجام والسيولة (Volume Check)
        volume_current = futures_volumes[-1]
        volume_avg = sum(futures_volumes[-5:-1]) / 4 if len(futures_volumes) >= 5 else volume_current
        volume_surging = volume_current > volume_avg

        # 3. المؤشرات الفنية (EMA & RSI)
        ema20 = calculate_ema(spot_closes, 20)[-1]
        ema50 = calculate_ema(spot_closes, 50)[-1]
        rsi = calculate_rsi(spot_closes, 14)

        # إدارة الصفقة القائمة
        if active_order is not None:
            if active_order['type'] == 'Buy Stop' and spot_price >= active_order['tp']:
                active_order = None
            elif active_order['type'] == 'Sell Stop' and spot_price <= active_order['tp']:
                active_order = None
            elif active_order['type'] == 'Buy Stop' and spot_price <= active_order['sl']:
                active_order = None
            elif active_order['type'] == 'Sell Stop' and spot_price >= active_order['sl']:
                active_order = None
            
            return active_order, spot_price, basis_current, rsi

        # -------------------------------------------------------------
        # شروط الدخول المحسّنة (مسافة دخول قريبة ودقيقة من السعر المباشر)
        # -------------------------------------------------------------

        # شرط Buy Stop المؤسسي المباشر
        if ema20 > ema50 and rsi < 68 and (basis_expansion >= 0.15 or volume_surging):
            # تحديد الدخول بفارق 1.50$ فقط أعلى السعر المباشر الحالي
            proposed_entry = round(spot_price + 1.50, 2)
            
            active_order = {
                "type": "Buy Stop",
                "entry": proposed_entry,
                "tp": round(proposed_entry + 6.0, 2),
                "sl": round(proposed_entry - 3.0, 2),
                "rsi": rsi,
                "reason": "تدفق سيولة شرائية على العقود الآجلة - دخول قريب من السعر المباشر"
            }

        # شرط Sell Stop المؤسسي المباشر
        elif ema20 < ema50 and rsi > 32 and (basis_expansion <= -0.15 or volume_surging):
            # تحديد الدخول بفارق 1.50$ فقط أسفل السعر المباشر الحالي
            proposed_entry = round(spot_price - 1.50, 2)
            
            active_order = {
                "type": "Sell Stop",
                "entry": proposed_entry,
                "tp": round(proposed_entry - 6.0, 2),
                "sl": round(proposed_entry + 3.0, 2),
                "rsi": rsi,
                "reason": "تسارع ضغوط بيعية مؤسسية على العقود الآجلة - دخول قريب من السعر المباشر"
            }

        return active_order, spot_price, basis_current, rsi

    except Exception as e:
        print(f"Institutional Logic Error: {e}")
        return None, 0, 0, 0

async def main_loop():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID environment variables!")
        return

    bot = Bot(token=token)

    while True:
        order, spot_price, basis, rsi = get_institutional_gold_signal()
        
        if order:
            emoji = "🟢" if order['type'] == "Buy Stop" else "🔴"
            msg = (
                f"🚨 **إشارة دخول مؤسسية سريعة (Tight Order Flow)**\n"
                f"⏱ **الفريم:** 15 دقيقة (M15) | **الرمز:** XAUUSD\n\n"
                f"📊 **سعر المنصة المباشر:** {spot_price}\n"
                f"📐 **فارق الآجل/الفوري (Basis):** {basis}\n"
                f"📈 **مؤشر RSI:** {rsi}\n"
                f"💡 **السبب:** {order['reason']}\n\n"
                f"{emoji} **نوع الأمر المعلق:** {order['type']}\n"
                f"🎯 **سعر الدخول (Entry):** {order['entry']}\n"
                f"🟢 **الهدف (TP):** {order['tp']}\n"
                f"🔴 **وقف الخسارة (SL):** {order['sl']}\n\n"
                f"📌 *المسافة قريبة جداً من السعر اللحظي (1.5$ فقط)، نفذ الأمر معلقاً على منصتك فوراً.*"
            )

            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                print(f"[{time.strftime('%H:%M:%S')}] Tight institutional signal sent successfully.")
            except Exception as e:
                print(f"Send Error: {e}")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] Monitoring institutional market structure...")

        await asyncio.sleep(300)

if __name__ == "__main__":
    Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main_loop())
