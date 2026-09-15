import os
import time
import asyncio
import requests
from datetime import datetime
from threading import Thread
from flask import Flask
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

app = Flask(__name__)

@app.route('/')
@app.route('/ping')
def home():
    return "Pro Scalper ATR-Adaptive S/R Bot is Live and Active!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= ================= =================
# 1. المتغيرات العامة والإعدادات
# ================= ================= =================
active_orders = {}  
send_status_reports = True    
strategy_strictness = "flexible" 
active_strategy = "both"      # "scalping", "intraday", "both"
news_filter_active = True
account_balance_cents = 200000  
risk_percentage = 0.5           
current_key_idx = 0
last_scanned_candle = ""     

STRATEGY_TYPES = {
    "scalping": {"label": "⚡️ السكالبينج الخاطف (1:1.5)"},
    "intraday": {"label": "📈 الاتجاه اليومي (1:2)"},
    "both":     {"label": "🚀 الاستراتيجيتين معاً"}
}

ASSET_MODES = {
    "gold_only": {
        "label": "🟡 الذهب فقط",
        "symbols": ["XAU/USD"]
    },
    "gold_forex": {
        "label": "الذهب والعملات 💱",
        "symbols": ["XAU/USD", "EUR/USD", "GBP/USD", "USD/JPY"]
    },
    "stocks_only": {
        "label": "الأسهم فقط 📈",
        "symbols": ["TSLA", "NVDA", "AMD", "AAPL"]
    },
    "all": {
        "label": "الكل (ذهب + عملات + أسهم) 🚀",
        "symbols": ["XAU/USD", "EUR/USD", "GBP/USD", "USD/JPY", "TSLA", "NVDA", "AMD", "AAPL"]
    }
}

selected_mode = "gold_only"

def get_all_api_keys():
    keys = []
    for k, v in os.environ.items():
        if k.startswith("TWELVE_DATA_API_KEY") and v.strip():
            if v.strip() not in keys:
                keys.append(v.strip())
    return keys

# ================= ================= =================
# 2. محرك طلبات الـ API مع التبديل التلقائي
# ================= ================= =================
def fetch_url(url, timeout=6):
    try:
        res = requests.get(url, timeout=timeout)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("code") == 429:
                return {"status_code": 429}
            return data
        elif res.status_code == 429:
            return {"status_code": 429}
    except Exception as e:
        print(f"Fetch Error: {e}")
    return None

async def fetch_twelve_data(endpoint_path):
    global current_key_idx
    api_keys = get_all_api_keys()
    if not api_keys:
        print("❌ Error: No Twelve Data API keys found in Environment Variables!")
        return None

    attempts = len(api_keys)
    for _ in range(attempts):
        if current_key_idx >= len(api_keys):
            current_key_idx = 0
            
        active_key = api_keys[current_key_idx]
        url = f"https://api.twelvedata.com/{endpoint_path}&apikey={active_key}"
        res = await asyncio.to_thread(fetch_url, url, 6)
        
        if res and isinstance(res, dict) and res.get("status_code") == 429:
            current_key_idx = (current_key_idx + 1) % len(api_keys)
            print(f"⚠️ API Limit Reached! Switched to API Key Index: {current_key_idx}")
            continue
        return res
    return {"status_code": 429}

# ================= ================= =================
# 3. إدارة الأخبار وحاسبة اللوت
# ================= ================= =================
async def is_high_impact_news_near():
    if not news_filter_active:
        return False, ""
    try:
        url = "https://nfp.ourforecast.com/api/v1/events"
        data = await asyncio.to_thread(fetch_url, url, 5)
        if data and isinstance(data, list):
            now = datetime.utcnow()
            for event in data:
                if event.get("currency") == "USD" and event.get("impact") == "High":
                    event_time_str = event.get("date", "").replace("Z", "+00:00")
                    if event_time_str:
                        event_time = datetime.fromisoformat(event_time_str).replace(tzinfo=None)
                        if abs((event_time - now).total_seconds()) <= 1800:
                            return True, f"{event.get('title')} ({event_time.strftime('%H:%M')} UTC)"
    except Exception as e:
        print(f"News API Error: {e}")
    return False, ""

def calculate_recommended_lot(symbol, entry_price, stop_loss_price):
    try:
        risk_amount_cents = account_balance_cents * (risk_percentage / 100.0)
        price_distance = abs(entry_price - stop_loss_price)
        if price_distance == 0:
            return 0.10

        cost_per_point = 100000.0 if "/" in symbol and symbol != "XAU/USD" else 100.0
        raw_lot = risk_amount_cents / (price_distance * cost_per_point)
        return max(0.01, round(raw_lot, 2))
    except Exception as e:
        print(f"Error calculating lot: {e}")
        return 0.10

# ================= ================= =================
# 4. المؤشرات الحسابية والتحليل التكيفي (ATR + S/R)
# ================= ================= =================
def calculate_atr(highs, lows, closes, window=14):
    if len(closes) < window + 1:
        return 2.0
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)
    if not tr_list:
        return 2.0
    return sum(tr_list[-window:]) / window

def calculate_rsi(closes, window=14):
    if len(closes) < window + 1:
        return 50.0
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
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(data, window):
    if not data: return [0]
    weights = [2 / (window + 1)]
    ema = [data[0]]
    for price in data[1:]:
        ema.append((price * weights[0]) + (ema[-1] * (1 - weights[0])))
    return ema

async def get_live_price(symbol):
    res = await fetch_twelve_data(f"price?symbol={symbol}")
    if res and "price" in res:
        return float(res["price"])
    return None

def adjust_tp_for_structure(symbol, order_type, entry, math_tp, sl, lookback_highs, lookback_lows, precision, tp_buffer):
    risk_dist = abs(entry - sl)
    if risk_dist == 0:
        return math_tp, True

    if order_type == "Buy Stop":
        resistances = [h for h in lookback_highs if h > entry]
        if resistances:
            nearest_res = min(resistances)
            safe_tp = nearest_res - tp_buffer
            if safe_tp < math_tp:
                if (safe_tp - entry) >= (risk_dist * 1.2):
                    return round(safe_tp, precision), True
                else:
                    return math_tp, False
        return math_tp, True

    elif order_type == "Sell Stop":
        supports = [l for l in lookback_lows if l < entry]
        if supports:
            nearest_sup = max(supports)
            safe_tp = nearest_sup + tp_buffer
            if safe_tp > math_tp:
                if (entry - safe_tp) >= (risk_dist * 1.2):
                    return round(safe_tp, precision), True
                else:
                    return math_tp, False
        return math_tp, True

    return math_tp, True

async def analyze_symbol(symbol):
    global active_orders, strategy_strictness, active_strategy
    
    if datetime.utcnow().weekday() in [5, 6]:
        return [], "الماركت مغلق", 0, 0
        
    has_news, _ = await is_high_impact_news_near()
    if has_news:
        return [], "توقف للأخبار", 0, 0

    res_spot = await fetch_twelve_data(f"time_series?symbol={symbol}&interval=15min&outputsize=120")
    if not res_spot or res_spot.get("status_code") == 429 or "values" not in res_spot:
        return [], "خطأ في الـ API", 0, 0
        
    spot_values = res_spot["values"]
    spot_values.reverse()
    spot_closes = [float(item["close"]) for item in spot_values]
    spot_highs = [float(item["high"]) for item in spot_values]
    spot_lows = [float(item["low"]) for item in spot_values]

    precision = 4 if "/" in symbol and symbol != "XAU/USD" else 2
    spot_price = round(spot_closes[-1], precision)
    
    current_atr = calculate_atr(spot_highs, spot_lows, spot_closes, window=14)
    ema20 = calculate_ema(spot_closes, 20)[-1]
    ema50 = calculate_ema(spot_closes, 50)[-1]
    rsi = calculate_rsi(spot_closes, 14)

    rsi_buy_max = 75 if strategy_strictness == "flexible" else 68
    rsi_buy_min = 30  
    rsi_sell_min = 25 if strategy_strictness == "flexible" else 32
    rsi_sell_max = 70 

    price_step = round(max(0.30, current_atr * 0.25), precision)
    lookback_highs = spot_highs[-100:-1]
    lookback_lows = spot_lows[-100:-1]

    strats_to_check = []
    if active_strategy in ["intraday", "both"]:
        strats_to_check.append("intraday")
    if active_strategy in ["scalping", "both"]:
        strats_to_check.append("scalping")

    generated_orders = []

    for strat in strats_to_check:
        order_key = f"{symbol}_{strat}"
        if order_key in active_orders:
            continue

        # --- أ) فحص الاتجاه اليومي ---
        if strat == "intraday":
            wide_swing_high = max(spot_highs[-100:-1])
            wide_swing_low = min(spot_lows[-100:-1])
            sl_buffer = round(current_atr * 2.0, precision)
            tp_buffer = round(current_atr * 0.5, precision)

            if ema20 > ema50 and (rsi_buy_min <= rsi < rsi_buy_max):
                proposed_entry = round(max(wide_swing_high, spot_price) + price_step, precision)
                sl_price = round(wide_swing_low - sl_buffer, precision)
                risk_distance = proposed_entry - sl_price
                
                if proposed_entry > spot_price > sl_price and risk_distance > 0:
                    raw_tp = round(proposed_entry + (risk_distance * 2.0), precision)
                    final_tp, is_valid = adjust_tp_for_structure(symbol, "Buy Stop", proposed_entry, raw_tp, sl_price, lookback_highs, lookback_lows, precision, tp_buffer)
                    
                    if is_valid:
                        rec_lot = calculate_recommended_lot(symbol, proposed_entry, sl_price)
                        new_order = {
                            "order_key": order_key, "symbol": symbol, "strategy_type": strat,
                            "strategy_name": "📈 اتجاه يومي (ATR ذكي)", "type": "Buy Stop", 
                            "entry": proposed_entry, "tp": final_tp, "sl": sl_price, 
                            "rsi": rsi, "lot": rec_lot, "status": "PENDING"
                        }
                        active_orders[order_key] = new_order
                        generated_orders.append(new_order)

            elif ema20 < ema50 and (rsi_sell_min < rsi <= rsi_sell_max):
                proposed_entry = round(min(wide_swing_low, spot_price) - price_step, precision)
                sl_price = round(max(wide_swing_high, spot_price) + sl_buffer, precision)
                risk_distance = sl_price - proposed_entry
                
                if proposed_entry < spot_price < sl_price and risk_distance > 0:
                    raw_tp = round(proposed_entry - (risk_distance * 2.0), precision)
                    final_tp, is_valid = adjust_tp_for_structure(symbol, "Sell Stop", proposed_entry, raw_tp, sl_price, lookback_highs, lookback_lows, precision, tp_buffer)
                    
                    if is_valid:
                        rec_lot = calculate_recommended_lot(symbol, proposed_entry, sl_price)
                        new_order = {
                            "order_key": order_key, "symbol": symbol, "strategy_type": strat,
                            "strategy_name": "📈 اتجاه يومي (ATR ذكي)", "type": "Sell Stop", 
                            "entry": proposed_entry, "tp": final_tp, "sl": sl_price, 
                            "rsi": rsi, "lot": rec_lot, "status": "PENDING"
                        }
                        active_orders[order_key] = new_order
                        generated_orders.append(new_order)

        # --- ب) فحص السكالبينج الخاطف ---
        elif strat == "scalping":
            tight_swing_high = max(spot_highs[-16:-1])
            tight_swing_low = min(spot_lows[-16:-1])
            sl_buffer_scalp = round(current_atr * 1.0, precision)
            tp_buffer_scalp = round(current_atr * 0.3, precision)
            max_risk = round(current_atr * 4.0, precision)

            if ema20 > ema50 and (rsi_buy_min <= rsi < rsi_buy_max):
                proposed_entry = round(min(max(tight_swing_high, spot_price) + price_step, spot_price + (price_step * 5)), precision)
                sl_price = round(tight_swing_low - sl_buffer_scalp, precision)
                risk_distance = proposed_entry - sl_price

                if proposed_entry > spot_price > sl_price and risk_distance <= max_risk:
                    raw_tp = round(proposed_entry + (risk_distance * 1.5), precision)
                    final_tp, is_valid = adjust_tp_for_structure(symbol, "Buy Stop", proposed_entry, raw_tp, sl_price, lookback_highs, lookback_lows, precision, tp_buffer_scalp)
                    
                    if is_valid:
                        rec_lot = calculate_recommended_lot(symbol, proposed_entry, sl_price)
                        new_order = {
                            "order_key": order_key, "symbol": symbol, "strategy_type": strat,
                            "strategy_name": "⚡️ سكالبينج خاطف (ATR ذكي)", "type": "Buy Stop", 
                            "entry": proposed_entry, "tp": final_tp, "sl": sl_price, 
                            "rsi": rsi, "lot": rec_lot, "status": "PENDING"
                        }
                        active_orders[order_key] = new_order
                        generated_orders.append(new_order)

            elif ema20 < ema50 and (rsi_sell_min < rsi <= rsi_sell_max):
                proposed_entry = round(max(min(tight_swing_low, spot_price) - price_step, spot_price - (price_step * 5)), precision)
                sl_price = round(max(tight_swing_high, spot_price) + sl_buffer_scalp, precision)
                risk_distance = sl_price - proposed_entry

                if proposed_entry < spot_price < sl_price and risk_distance <= max_risk:
                    raw_tp = round(proposed_entry - (risk_distance * 1.5), precision)
                    final_tp, is_valid = adjust_tp_for_structure(symbol, "Sell Stop", proposed_entry, raw_tp, sl_price, lookback_highs, lookback_lows, precision, tp_buffer_scalp)
                    
                    if is_valid:
                        rec_lot = calculate_recommended_lot(symbol, proposed_entry, sl_price)
                        new_order = {
                            "order_key": order_key, "symbol": symbol, "strategy_type": strat,
                            "strategy_name": "⚡️ سكالبينج خاطف (ATR ذكي)", "type": "Sell Stop", 
                            "entry": proposed_entry, "tp": final_tp, "sl": sl_price, 
                            "rsi": rsi, "lot": rec_lot, "status": "PENDING"
                        }
                        active_orders[order_key] = new_order
                        generated_orders.append(new_order)

    status_str = f"تم استخراج {len(generated_orders)} إشارة" if generated_orders else "مسح طبيعي"
    return generated_orders, status_str, spot_price, rsi

# ================= ================= =================
# 5. الحلقات المستقلة (المسح والمراقبة ونبض الحياة)
# ================= ================= =================
async def market_scanner_loop(bot: Bot, chat_id: str):
    global last_scanned_candle, selected_mode
    print("🚀 Market Scanner Loop Started...")
    
    while True:
        try:
            now = datetime.utcnow()
            if now.minute % 15 == 0:
                candle_id = now.strftime("%Y-%m-%d %H:%M")
                
                if candle_id != last_scanned_candle:
                    last_scanned_candle = candle_id
                    print(f"⏰ [M15 Triggered] Candle Close: {candle_id} UTC")
                    
                    current_symbols = ASSET_MODES[selected_mode]["symbols"]
                    scan_results = []
                    
                    for sym in current_symbols:
                        try:
                            orders, event, spot_price, rsi = await analyze_symbol(sym)
                            scan_results.append((sym, spot_price, rsi, event))
                            
                            for order in orders:
                                emoji = "🟢" if order['type'] == "Buy Stop" else "🔴"
                                msg = (
                                    f"⚡️ **إشارة جديدة [{order.get('strategy_name')}]**\n"
                                    f"⏱ **التوقيت:** {now.strftime('%H:%M')} UTC | **{sym}**\n\n"
                                    f"📊 **السعر الحالي:** `{spot_price}` | **RSI:** `{rsi}`\n"
                                    f"{emoji} **النوع:** {order['type']}\n"
                                    f"🎯 **أمر الدخول:** `{order['entry']}`\n"
                                    f"🟢 **الهدف الأمني (TP):** `{order['tp']}`\n"
                                    f"🔴 **الستوب التكيفي (SL):** `{order['sl']}`\n\n"
                                    f"💰 **اللوت المقترح:** `{order['lot']}` | المخاطرة المحسوبة: {risk_percentage}%"
                                )
                                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        except Exception as sym_err:
                            print(f"Error scanning {sym}: {sym_err}")
                            scan_results.append((sym, 0, 0, "خطأ بالاتصال"))
                        
                        await asyncio.sleep(1)

                    summary_lines = []
                    for sym, price, rsi, status in scan_results:
                        summary_lines.append(f"▫️ **{sym}:** السعر `{price}` | RSI `{rsi}` ({status})")
                    
                    summary_msg = (
                        f"📊 **تقرير مسح شمعة M15 مكتملة**\n"
                        f"⏱ **التوقيت:** `{now.strftime('%H:%M')} UTC`\n"
                        f"🌐 **النطاق:** {ASSET_MODES[selected_mode]['label']}\n"
                        f"🎯 **الاستراتيجية:** {STRATEGY_TYPES[active_strategy]['label']}\n\n"
                        + "\n".join(summary_lines) + "\n\n"
                        f"✅ **الحالة:** تم تفعيل التتبع المزدوج المستقل لكلتا الاستراتيجيتين."
                    )
                    await bot.send_message(chat_id=chat_id, text=summary_msg, parse_mode="Markdown")

        except Exception as e:
            print(f"Scanner Loop Exception: {e}")
            
        await asyncio.sleep(5) 

async def order_monitor_loop(bot: Bot, chat_id: str):
    global active_orders
    while True:
        try:
            if active_orders:
                keys_to_remove = []
                for order_key, order in list(active_orders.items()):
                    sym = order["symbol"]
                    live_p = await get_live_price(sym)
                    if live_p is not None:
                        status = order["status"]
                        strat_title = order.get("strategy_name", "")
                        
                        if status == "PENDING":
                            if (order['type'] == 'Buy Stop' and live_p <= order['sl']) or (order['type'] == 'Sell Stop' and live_p >= order['sl']):
                                keys_to_remove.append(order_key)
                                await bot.send_message(chat_id=chat_id, text=f"🚫 **إلغاء صفقة [{sym} - {strat_title}]:** السعر ضرب الستوب قبل التفعيل (`{live_p}`).", parse_mode="Markdown")
                            elif order['type'] == 'Buy Stop' and live_p >= order['entry']:
                                active_orders[order_key]["status"] = "TRIGGERED"
                                await bot.send_message(chat_id=chat_id, text=f"⚡️ **تفعيل صفقة شراء [{sym} - {strat_title}]!** السعر: `{live_p}`", parse_mode="Markdown")
                            elif order['type'] == 'Sell Stop' and live_p <= order['entry']:
                                active_orders[order_key]["status"] = "TRIGGERED"
                                await bot.send_message(chat_id=chat_id, text=f"⚡️ **تفعيل صفقة بيع [{sym} - {strat_title}]!** السعر: `{live_p}`", parse_mode="Markdown")

                        elif status == "TRIGGERED":
                            if (order['type'] == 'Buy Stop' and live_p >= order['tp']) or (order['type'] == 'Sell Stop' and live_p <= order['tp']):
                                keys_to_remove.append(order_key)
                                await bot.send_message(chat_id=chat_id, text=f"🎯 **تم تحقيق الهدف [{sym} - {strat_title}]!** السعر: `{live_p}`", parse_mode="Markdown")
                            elif (order['type'] == 'Buy Stop' and live_p <= order['sl']) or (order['type'] == 'Sell Stop' and live_p >= order['sl']):
                                keys_to_remove.append(order_key)
                                await bot.send_message(chat_id=chat_id, text=f"🔴 **ضرب وقف الخسارة [{sym} - {strat_title}].** السعر: `{live_p}`", parse_mode="Markdown")
                
                for k in keys_to_remove:
                    if k in active_orders: del active_orders[k]

            await asyncio.sleep(60)
        except Exception as e:
            print(f"Monitor Loop Exception: {e}")
            await asyncio.sleep(30)

async def heartbeat_loop(bot: Bot, chat_id: str):
    last_sent_minute = -1
    while True:
        try:
            now = datetime.utcnow()
            if send_status_reports and now.minute % 5 == 0 and now.minute != last_sent_minute:
                last_sent_minute = now.minute
                keys_count = len(get_all_api_keys())
                msg = (
                    f"🟢 **نبض البوت:** مستيقظ ويعمل بنجاح ({now.strftime('%H:%M')} UTC)\n"
                    f"🔑 **مفاتيح API النشطة:** `{keys_count}`"
                )
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            await asyncio.sleep(10)
        except Exception as e:
            print(f"Heartbeat Exception: {e}")
            await asyncio.sleep(30)

# ================= ================= =================
# 6. لوحة التحكم والتقرير الشامل
# ================= ================= =================
def build_settings_text():
    return (
        f"⚙️ **تقرير وإعدادات البوت الحالية:**\n\n"
        f"🌐 **نطاق الأصول:** {ASSET_MODES[selected_mode]['label']}\n"
        f"🎯 **نمط الاستراتيجية:** {STRATEGY_TYPES[active_strategy]['label']}\n"
        f"🎛 **نمط المؤشرات:** {'مرن (Flexible)' if strategy_strictness == 'flexible' else 'مشدد (Strict)'}\n"
        f"📐 **تعديل الأهداف والستوب:** تكيفي مفعل 🤖 (مسار مستقل لكل استراتيجية)\n"
        f"🟢 **فلتر الأخبار:** {'مفعل ✅' if news_filter_active else 'معطل ❌'}\n"
        f"🎯 **نسبة المخاطرة:** `{risk_percentage}%` لكل صفقة\n"
        f"💰 **رصيد الحساب:** `{account_balance_cents}` سنت\n"
        f"📡 **رسائل نبض الحياة:** {'مفعلة 🟢' if send_status_reports else 'معطلة 🔴'}\n"
        f"🔑 **مفاتيح API المستكشفة:** `{len(get_all_api_keys())}`"
    )

def build_settings_keyboard():
    keyboard = [
        [InlineKeyboardButton(f"🌐 النطاق: {ASSET_MODES[selected_mode]['label']}", callback_data="menu_asset_modes")],
        [InlineKeyboardButton(f"🎯 الاستراتيجية: {STRATEGY_TYPES[active_strategy]['label']}", callback_data="menu_strategies")],
        [InlineKeyboardButton(f"🎛 النمط: {'مرن' if strategy_strictness == 'flexible' else 'مشدد'}", callback_data="toggle_mode")],
        [InlineKeyboardButton("🔴 إيقاف نبض الحياة" if send_status_reports else "🟢 تشغيل نبض الحياة", callback_data="toggle_report")],
        [InlineKeyboardButton(f"🟢 فلتر الأخبار: مفعل" if news_filter_active else "🔴 فلتر الأخبار: معطل", callback_data="toggle_news")],
        [InlineKeyboardButton(f"🎯 نسبة المخاطرة: {risk_percentage}%", callback_data="toggle_risk")],
        [InlineKeyboardButton(f"💰 الرصيد: {account_balance_cents} سنت", callback_data="prompt_balance")]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_strategies_keyboard():
    keyboard = [
        [InlineKeyboardButton("⚡️ السكالبينج الخاطف (1:1.5)", callback_data="set_strat_scalping")],
        [InlineKeyboardButton("📈 الاتجاه اليومي (1:2)", callback_data="set_strat_intraday")],
        [InlineKeyboardButton("🚀 الاستراتيجيتين معاً", callback_data="set_strat_both")],
        [InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_asset_modes_keyboard():
    keyboard = [
        [InlineKeyboardButton("🟡 الذهب فقط", callback_data="set_mode_gold_only")],
        [InlineKeyboardButton("الذهب والعملات 💱", callback_data="set_mode_gold_forex")],
        [InlineKeyboardButton("الأسهم فقط 📈", callback_data="set_mode_stocks_only")],
        [InlineKeyboardButton("الكل 🚀", callback_data="set_mode_all")],
        [InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def handle_manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **جاري الفحص المباشر وإرسال طلبات الـ API...**", parse_mode="Markdown")
    current_symbols = ASSET_MODES[selected_mode]["symbols"]
    report = f"📊 **تقرير الفحص الفوري ({datetime.utcnow().strftime('%H:%M')} UTC):**\n\n"
    
    for sym in current_symbols:
        orders, event, spot_price, rsi = await analyze_symbol(sym)
        report += f"▫️ **{sym}:** السعر `{spot_price}` | RSI: `{rsi}` | عدد الإشارات: `{len(orders)}`\n"
    
    report += f"\n🔑 **مفاتيح API المستكشفة:** `{len(get_all_api_keys())}`"
    await update.message.reply_text(report, parse_mode="Markdown")

async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global account_balance_cents
    text = update.message.text.strip()
    
    if text.lower().startswith("balance ") or text.lower().startswith("رصيد "):
        try:
            val = int(text.split()[1])
            account_balance_cents = val
            await update.message.reply_text(f"✅ **تم تحديث الرصيد إلى:** `{account_balance_cents}` سنت", parse_mode="Markdown")
        except Exception:
            await update.message.reply_text("❌ صيغة خاطئة! أرسل: `balance 200000`", parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global send_status_reports, strategy_strictness, active_strategy, news_filter_active, risk_percentage, selected_mode
    query = update.callback_query
    await query.answer()

    if query.data == "toggle_report": 
        send_status_reports = not send_status_reports
    elif query.data == "toggle_mode": 
        strategy_strictness = "strict" if strategy_strictness == "flexible" else "flexible"
    elif query.data == "toggle_news": 
        news_filter_active = not news_filter_active
    elif query.data == "toggle_risk": 
        risk_percentage = 1.0 if risk_percentage == 0.5 else 0.5
    elif query.data == "menu_asset_modes":
        await query.edit_message_text("📊 **اختر نطاق الأصول المقتنصة:**", reply_markup=build_asset_modes_keyboard(), parse_mode="Markdown")
        return
    elif query.data == "menu_strategies":
        await query.edit_message_text("🎯 **اختر استراتيجية التداول المفضلة:**", reply_markup=build_strategies_keyboard(), parse_mode="Markdown")
        return
    elif query.data.startswith("set_mode_"):
        selected_mode = query.data.replace("set_mode_", "")
    elif query.data.startswith("set_strat_"):
        active_strategy = query.data.replace("set_strat_", "")
    elif query.data == "back_to_settings":
        pass

    try:
        await query.edit_message_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")
    except Exception:
        pass

# ================= ================= =================
# 7. التشغيل الرئيسي
# ================= ================= =================
async def post_init(application: Application):
    chat_id = os.environ.get("CHAT_ID")
    if chat_id:
        asyncio.create_task(market_scanner_loop(application.bot, chat_id))
        asyncio.create_task(order_monitor_loop(application.bot, chat_id))
        asyncio.create_task(heartbeat_loop(application.bot, chat_id))

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token: 
        print("❌ TELEGRAM_TOKEN Missing!")
        return

    Thread(target=run_web_server, daemon=True).start()
    app_bot = Application.builder().token(token).post_init(post_init).build()

    app_bot.add_handler(MessageHandler(filters.Regex(r'(?i)^/?settings$'), handle_settings))
    app_bot.add_handler(MessageHandler(filters.Regex(r'(?i)^/?scan$'), handle_manual_scan))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    app_bot.add_handler(CallbackQueryHandler(button_callback))

    print("Pro Scalper ATR-Adaptive S/R Bot Running...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
