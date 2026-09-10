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
    return "Pro Dynamic M15 Gold Bot is Live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

active_order = None
order_status = None 

async def wait_for_next_5min_mark():
    while True:
        now = datetime.utcnow()
        if now.minute % 5 == 0 and now.second < 5:
            break
        await asyncio.sleep(1)

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

def analyze_gold_market():
    global active_order, order_status
    
    now_utc = datetime.utcnow()
    if now_utc.hour == 21:
        return None, "MARKET_CLOSED", 0, 0, 0

    api_key = os.environ.get("TWELVE_DATA_API_KEY")
    if not api_key:
        print("Error: TWELVE_DATA_API_KEY missing!")
        return None, "NO_KEY", 0, 0, 0

    try:
        url_spot = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=15min&outputsize=40&apikey={api_key}"
        res_spot = requests.get(url_spot).json()

        url_futures = f"https://api.twelvedata.com/time_series?symbol=MGC&interval=15min&outputsize=40&apikey={api_key}"
        res_futures = requests.get(url_futures).json()

        if "values" not in res_spot:
            print("Twelve Data Error or limit reached.")
            return None, "API_ERROR", 0, 0, 0

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

        # قمم وقيعان الشموع الـ 3 الأخيرة لإبقائها قريبة ولكن حقيقية
        tight_swing_high = max(spot_highs[-3:-1])
        tight_swing_low = min(spot_lows[-3:-1])

        # -------------------------------------------------------------
        # 1. إدارة ومتابعة الصفقة الحالية
        # -------------------------------------------------------------
        if active_order is not None:
            # فحص التفعيل
            if order_status == "PENDING":
                if active_order['type'] == 'Buy Stop' and spot_price >= active_order['entry']:
                    order_status = "TRIGGERED"
                    return active_order, "JUST_TRIGGERED", spot_price, basis_current, rsi
                elif active_order['type'] == 'Sell Stop' and spot_price <= active_order['entry']:
                    order_status = "TRIGGERED"
                    return active_order, "JUST_TRIGGERED", spot_price, basis_current, rsi

            # فحص إغلاق الصفقة المفعلة (TP / SL)
            if order_status == "TRIGGERED":
                if active_order['type'] == 'Buy Stop':
                    if spot_price >= active_order['tp'] or spot_price <= active_order['sl']:
                        active_order = None
                        order_status = None
                        return None, "CLOSED", spot_price, basis_current, rsi
                elif active_order['type'] == 'Sell Stop':
                    if spot_price <= active_order['tp'] or spot_price >= active_order['sl']:
                        active_order = None
                        order_status = None
                        return None, "CLOSED", spot_price, basis_current, rsi

            # إلغاء الصفقة المعلقة إذا هبط السعر وكسر الستوب قبل تفعيل الدخول (Invalidation Check)
            if order_status == "PENDING":
                if active_order['type'] == 'Buy Stop' and (spot_price <= active_order['sl'] or ema20 < ema50):
                    active_order = None
                    order_status = None
                    return None, "CANCELLED_INVALIDATED", spot_price, basis_current, rsi
                elif active_order['type'] == 'Sell Stop' and (spot_price >= active_order['sl'] or ema20 > ema50):
                    active_order = None
                    order_status = None
                    return None, "CANCELLED_INVALIDATED", spot_price, basis_current, rsi

            if active_order is not None:
                status_event = "STILL_TRIGGERED" if order_status == "TRIGGERED" else "STILL_PENDING"
                return active_order, status_event, spot_price, basis_current, rsi

        # -------------------------------------------------------------
        # 2. توليد صفقة جديدة بشرط الستوب أسفل ذيل الشمعة تماماً
        # -------------------------------------------------------------
        if ema20 > ema50 and rsi < 68 and (basis_expansion >= 0.10 or volume_surging):
            proposed_entry = round(max(tight_swing_high, spot_price) + 0.50, 2)
            # الستوب يتم وضعه دائماً تحت ذيل أ저قل شمعة من الشموع الأخيرة بنطاق أمان 0.50$
            sl_price = round(tight_swing_low - 0.50, 2)
            
            risk_distance = proposed_entry - sl_price
            tp_price = round(proposed_entry + (risk_distance * 1.5), 2)

            active_order = {
                "type": "Buy Stop",
                "entry": proposed_entry,
                "tp": tp_price,
                "sl": sl_price,
                "rsi": rsi
            }
            order_status = "PENDING"
            return active_order, "NEW_ORDER", spot_price, basis_current, rsi

        elif ema20 < ema50 and rsi > 32 and (basis_expansion <= -0.10 or volume_surging):
            proposed_entry = round(min(tight_swing_low, spot_price) - 0.50, 2)
            # الستوب يتم وضعه دائماً أعلى ذيل أعلى شمعة من الشموع الأخيرة بنطاق أمان 0.50$
            sl_price = round(tight_swing_high + 0.50, 2)

            risk_distance = sl_price - proposed_entry
            tp_price = round(proposed_entry - (risk_distance * 1.5), 2)

            active_order = {
                "type": "Sell Stop",
                "entry": proposed_entry,
                "tp": tp_price,
                "sl": sl_price,
                "rsi": rsi
            }
            order_status = "PENDING"
            return active_order, "NEW_ORDER", spot_price, basis_current, rsi

        return None, "NO_SIGNAL", spot_price, basis_current, rsi

    except Exception as e:
        print(f"Execution Error: {e}")
        return None, "ERROR", 0, 0, 0

async def main_loop():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID environment variables!")
        return

    bot = Bot(token=token)

    while True:
        await wait_for_next_5min_mark()
        
        order, event, spot_price, basis, rsi = analyze_gold_market()
        
        if event == "MARKET_CLOSED":
            print(f"[{time.strftime('%H:%M:%S')}] Market closed for settlement.")
            
        elif event == "CANCELLED_INVALIDATED":
            msg = (
                f"🚫 **تم إلغاء الإشارة المعلقة تلقائياً**\n\n"
                f"السبب: تجاوز السعر مستوى وقف الخسارة قبل تفعيل الصفقة، مما أبطل الفكرة الفنية.\n"
                f"⏳ *البوت بانتظار تشكل فرصة جديدة...*"
            )
            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                print(f"Send Error: {e}")
                
        elif order:
            emoji = "🟢" if order['type'] == "Buy Stop" else "🔴"
            
            if event in ["NEW_ORDER", "STILL_PENDING"]:
                msg_header = "🚨 **إشارة جديدة (محمية بالذيل)**" if event == "NEW_ORDER" else "⏳ **تذكير بالأمر المعلق**"
                
                msg = (
                    f"{msg_header}\n"
                    f"⏱ **توقيت الفحص:** {time.strftime('%H:%M')} | **الرمز:** XAUUSD\n\n"
                    f"📊 **سعر المنصة:** `{spot_price}`\n"
                    f"📐 **فارق الآجل/الفوري:** `{basis}`\n"
                    f"📈 **مؤشر RSI:** `{rsi}`\n\n"
                    f"{emoji} **نوع الأمر المعلق:** {order['type']}\n"
                    f"🎯 **سعر الدخول:** `{order['entry']}`\n"
                    f"🟢 **الهدف:** `{order['tp']}`\n"
                    f"🔴 **وقف الخسارة:** `{order['sl']}`\n\n"
                    f"📌 *الستوب محمي بالكامل أسفل قاع الشمعة.*"
                )
                try:
                    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                except Exception as e:
                    print(f"Send Error: {e}")

            elif event == "JUST_TRIGGERED":
                msg = (
                    f"⚡️ **تم تفعيل الصفقة الآن!**\n\n"
                    f" Symbol: **XAUUSD** | Type: **{order['type']}**\n"
                    f"📍 **سعر التفعيل:** `{spot_price}`\n"
                    f"🟢 **الهدف:** `{order['tp']}`\n"
                    f"🔴 **الستوب:** `{order['sl']}`"
                )
                try:
                    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                except Exception as e:
                    print(f"Send Error: {e}")

        await asyncio.sleep(10)

if __name__ == "__main__":
    Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main_loop())
