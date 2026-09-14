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
def home():
    return "Pro Scalper M15 Multi-Asset Bot is Live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= ================= =================
# 1. المتغيرات العامة والإعدادات
# ================= ================= =================
active_orders = {}  
send_status_reports = True  
strategy_mode = "flexible"   
news_filter_active = True
account_balance_cents = 200000  # 2,000 دولار
risk_percentage = 0.5           

# إعداد مفاتيح الـ API الأساسية والثانوية
API_KEYS = []
key1 = os.environ.get("TWELVE_DATA_API_KEY")
key2 = os.environ.get("TWELVE_DATA_API_KEY_2") # مفتاح احتياطي ثانوي
if key1: API_KEYS.append(key1)
if key2: API_KEYS.append(key2)

current_key_idx = 0

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

# ================= ================= =================
# 2. محرك طلبات الـ API الذكي (التبديل التلقائي)
# ================= ================= =================
def fetch_url(url, timeout=5):
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
    global current_key_idx, API_KEYS
    if not API_KEYS:
        return None

    attempts = len(API_KEYS)
    for _ in range(attempts):
        active_key = API_KEYS[current_key_idx]
        url = f"https://api.twelvedata.com/{endpoint_path}&apikey={active_key}"
        res = await asyncio.to_thread(fetch_url, url, 6)
        
        if res and isinstance(res, dict) and res.get("status_code") == 429:
            # التبديل للمفتاح الثانوي فوراً عند نفاذ الكوتا
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            print(f"⚠️ API Limit Reached! Switched to API Key Index: {current_key_idx}")
            continue
        return res
    return {"status_code": 429}

# ================= ================= =================
# 3. إدارة التوقيت وحاسبة اللوت
# ================= ================= =================
async def wait_for_m15_close():
    """الانتظار حتى لحظة إغلاق شمعة M15 (الدقيقة 00, 15, 30, 45)"""
    while True:
        now = datetime.utcnow()
        if now.minute % 15 == 0 and now.second < 3:
            break
        await asyncio.sleep(1)

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
# 4. التحليل الفني
# ================= ================= =================
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
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(data, window):
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

async def analyze_symbol(symbol):
    global active_orders, strategy_mode
    
    if datetime.utcnow().weekday() in [5, 6]:
        return None, "MARKET_CLOSED", 0, 0, 0
        
    has_news, _ = await is_high_impact_news_near()
    if has_news:
        return None, "NEWS_PAUSE", 0, 0, 0

    res_spot = await fetch_twelve_data(f"time_series?symbol={symbol}&interval=15min&outputsize=40")
    if not res_spot or res_spot.get("status_code") == 429 or "values" not in res_spot:
        return None, "API_LIMIT", 0, 0, 0
        
    spot_values = res_spot["values"]
    spot_values.reverse()
    spot_closes = [float(item["close"]) for item in spot_values]
    spot_highs = [float(item["high"]) for item in spot_values]
    spot_lows = [float(item["low"]) for item in spot_values]

    precision = 4 if "/" in symbol and symbol != "XAU/USD" else 2
    spot_price = round(spot_closes[-1], precision)
    
    ema20 = calculate_ema(spot_closes, 20)[-1]
    ema50 = calculate_ema(spot_closes, 50)[-1]
    rsi = calculate_rsi(spot_closes, 14)
    tight_swing_high = max(spot_highs[-3:-1])
    tight_swing_low = min(spot_lows[-3:-1])

    if symbol in active_orders:
        return active_orders[symbol], "ACTIVE", spot_price, rsi, 0

    rsi_buy_max = 75 if strategy_mode == "flexible" else 68
    rsi_buy_min = 30  
    rsi_sell_min = 25 if strategy_mode == "flexible" else 32
    rsi_sell_max = 70 

    price_step = 0.50 if symbol == "XAU/USD" else (spot_price * 0.0015)
    max_risk = 10.00 if symbol == "XAU/USD" else (spot_price * 0.02)

    if ema20 > ema50 and (rsi_buy_min <= rsi < rsi_buy_max):
        proposed_entry = round(min(max(tight_swing_high, spot_price) + price_step, spot_price + (price_step * 5)), precision)
        sl_price = round(tight_swing_low - price_step, precision)
        if proposed_entry > spot_price > sl_price:
            risk_distance = proposed_entry - sl_price
            if risk_distance <= max_risk:
                tp_price = round(proposed_entry + (risk_distance * 1.5), precision)
                rec_lot = calculate_recommended_lot(symbol, proposed_entry, sl_price)
                new_order = {"symbol": symbol, "type": "Buy Stop", "entry": proposed_entry, "tp": tp_price, "sl": sl_price, "rsi": rsi, "lot": rec_lot, "status": "PENDING"}
                active_orders[symbol] = new_order
                return new_order, "NEW_ORDER", spot_price, rsi, rec_lot

    elif ema20 < ema50 and (rsi_sell_min < rsi <= rsi_sell_max):
        proposed_entry = round(max(min(tight_swing_low, spot_price) - price_step, spot_price - (price_step * 5)), precision)
        sl_price = round(tight_swing_high + price_step, precision)
        if proposed_entry < spot_price < sl_price:
            risk_distance = sl_price - proposed_entry
            if risk_distance <= max_risk:
                tp_price = round(proposed_entry - (risk_distance * 1.5), precision)
                rec_lot = calculate_recommended_lot(symbol, proposed_entry, sl_price)
                new_order = {"symbol": symbol, "type": "Sell Stop", "entry": proposed_entry, "tp": tp_price, "sl": sl_price, "rsi": rsi, "lot": rec_lot, "status": "PENDING"}
                active_orders[symbol] = new_order
                return new_order, "NEW_ORDER", spot_price, rsi, rec_lot

    return None, "NO_SIGNAL", spot_price, rsi, 0

# ================= ================= =================
# 5. حلقات الفحص الذكي ومراقبة الصفقات
# ================= ================= =================
async def order_monitor_loop(bot: Bot, chat_id: str):
    """تستيقظ كل 5 دقائق فقط في حال وجود صفقات معلقة أو مفعلة لمتابعة الأهداف والستوب"""
    global active_orders
    while True:
        try:
            if active_orders:
                symbols_to_remove = []
                for sym, order in list(active_orders.items()):
                    live_p = await get_live_price(sym)
                    if live_p is not None:
                        status = order["status"]
                        if status == "PENDING":
                            if (order['type'] == 'Buy Stop' and live_p <= order['sl']) or (order['type'] == 'Sell Stop' and live_p >= order['sl']):
                                symbols_to_remove.append(sym)
                                await bot.send_message(chat_id=chat_id, text=f"🚫 **إلغاء صفقة [{sym}]:** السعر ضرب الستوب قبل التفعيل (`{live_p}`).", parse_mode="Markdown")
                            elif order['type'] == 'Buy Stop' and live_p >= order['entry']:
                                active_orders[sym]["status"] = "TRIGGERED"
                                await bot.send_message(chat_id=chat_id, text=f"⚡️ **تفعيل صفقة شراء [{sym}]!** السعر: `{live_p}`", parse_mode="Markdown")
                            elif order['type'] == 'Sell Stop' and live_p <= order['entry']:
                                active_orders[sym]["status"] = "TRIGGERED"
                                await bot.send_message(chat_id=chat_id, text=f"⚡️ **تفعيل صفقة بيع [{sym}]!** السعر: `{live_p}`", parse_mode="Markdown")

                        elif status == "TRIGGERED":
                            if (order['type'] == 'Buy Stop' and live_p >= order['tp']) or (order['type'] == 'Sell Stop' and live_p <= order['tp']):
                                symbols_to_remove.append(sym)
                                await bot.send_message(chat_id=chat_id, text=f"🎯 **تم تحقيق الهدف [{sym}]!** السعر: `{live_p}`", parse_mode="Markdown")
                            elif (order['type'] == 'Buy Stop' and live_p <= order['sl']) or (order['type'] == 'Sell Stop' and live_p >= order['sl']):
                                symbols_to_remove.append(sym)
                                await bot.send_message(chat_id=chat_id, text=f"🔴 **ضرب وقف الخسارة [{sym}].** السعر: `{live_p}`", parse_mode="Markdown")
                
                for sym in symbols_to_remove:
                    if sym in active_orders: del active_orders[sym]

            # تكرار الفحص كل 5 دقائق للصفقات النشطة فقط
            await asyncio.sleep(300)
        except Exception as e:
            print(f"Monitor Loop Exception: {e}")
            await asyncio.sleep(60)

async def heartbeat_loop(bot: Bot, chat_id: str):
    """إرسال تقرير نبض الحياة كل 5 دقائق بدون أي استهلاك لبيانات الـ API"""
    while True:
        try:
            await asyncio.sleep(300)
            if send_status_reports:
                msg = f"🟢 **نبض البوت:** البوت يعمل ومستيقظ بنجاح ({time.strftime('%H:%M')} UTC)."
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            print(f"Heartbeat Exception: {e}")

async def market_scanner_loop(bot: Bot, chat_id: str):
    """فحص السوق الفني يتم رسمياً عند إغلاق كل شمعة 15 دقيقة"""
    global selected_mode
    while True:
        try:
            await wait_for_m15_close()
            current_symbols = ASSET_MODES[selected_mode]["symbols"]
            
            for sym in current_symbols:
                order, event, spot_price, rsi, rec_lot = await analyze_symbol(sym)
                
                if order and event == "NEW_ORDER":
                    emoji = "🟢" if order['type'] == "Buy Stop" else "🔴"
                    msg = (
                        f"⚡️ **إشارة جديدة (شمعة M15 مكتملة)**\n"
                        f"⏱ **التوقيت:** {time.strftime('%H:%M')} | **{sym}**\n\n"
                        f"📊 **السعر:** `{spot_price}` | **RSI:** `{rsi}`\n"
                        f"{emoji} **النوع:** {order['type']}\n"
                        f"🎯 **الدخول:** `{order['entry']}`\n"
                        f"🟢 **الهدف:** `{order['tp']}` | 🔴 **الستوب:** `{order['sl']}`\n\n"
                        f"💰 **اللوت المقترح:** `{order['lot']}` | المخاطرة: {risk_percentage}%"
                    )
                    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                await asyncio.sleep(1)
        except Exception as e:
            print(f"Market Scanner Exception: {e}")
        await asyncio.sleep(10)

# ================= ================= =================
# 6. لوحة التحكم والتشغيل الرئيسي
# ================= ================= =================
def build_settings_keyboard():
    keyboard = [
        [InlineKeyboardButton(f"🌐 النطاق: {ASSET_MODES[selected_mode]['label']}", callback_data="menu_asset_modes")],
        [InlineKeyboardButton("🔴 إيقاف نبض الحياة" if send_status_reports else "🟢 تشغيل نبض الحياة", callback_data="toggle_report")],
        [InlineKeyboardButton(f"🎯 النمط: {'مرن' if strategy_mode == 'flexible' else 'مشدد'}", callback_data="toggle_mode")],
        [InlineKeyboardButton(f"🎯 نسبة المخاطرة: {risk_percentage}%", callback_data="toggle_risk")]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_asset_modes_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟡 الذهب فقط", callback_data="set_mode_gold_only")],
        [InlineKeyboardButton("الذهب والعملات 💱", callback_data="set_mode_gold_forex")],
        [InlineKeyboardButton("الأسهم فقط 📈", callback_data="set_mode_stocks_only")],
        [InlineKeyboardButton("الكل 🚀", callback_data="set_mode_all")]
    ])

async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚙️ **لوحة إعدادات البوت**", reply_markup=build_settings_keyboard(), parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global send_status_reports, strategy_mode, risk_percentage, selected_mode
    query = update.callback_query
    await query.answer()

    if query.data == "toggle_report": send_status_reports = not send_status_reports
    elif query.data == "toggle_mode": strategy_mode = "strict" if strategy_mode == "flexible" else "flexible"
    elif query.data == "toggle_risk": risk_percentage = 1.0 if risk_percentage == 0.5 else 0.5
    elif query.data == "menu_asset_modes":
        await query.edit_message_text("📊 **اختر نطاق الأصول:**", reply_markup=build_asset_modes_keyboard(), parse_mode="Markdown")
        return
    elif query.data.startswith("set_mode_"):
        selected_mode = query.data.replace("set_mode_", "")

    await query.edit_message_text("⚙️ **تم تحديث الإعدادات**", reply_markup=build_settings_keyboard(), parse_mode="Markdown")

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id: return

    Thread(target=run_web_server, daemon=True).start()
    app_bot = Application.builder().token(token).build()

    app_bot.add_handler(CommandHandler("settings", handle_settings))
    app_bot.add_handler(CallbackQueryHandler(button_callback))

    loop = asyncio.get_event_loop()
    loop.create_task(market_scanner_loop(app_bot.bot, chat_id))
    loop.create_task(order_monitor_loop(app_bot.bot, chat_id))
    loop.create_task(heartbeat_loop(app_bot.bot, chat_id))

    print("Smart M15 Bot Active...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
