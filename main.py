import os
import time
import asyncio
import requests
from datetime import datetime
from threading import Thread
from flask import Flask
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ================= ================= =================
# 1. خادم Flask للحفاظ على استمرارية البوت (Keep-Alive)
# ================= ================= =================
app = Flask(__name__)

@app.route('/')
@app.route('/ping')
def home():
    return "XAUUSD AI Dynamic Trading Engine is Live & Healthy!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= ================= =================
# 2. المتغيرات العامة والإعدادات
# ================= ================= =================
SYMBOL = "XAU/USD"
active_orders = {}  
send_status_reports = True    
news_filter_active = True
account_balance_cents = 200000  
risk_percentage = 0.5           
current_key_idx = 0
last_scanned_candle = ""     
active_strategy = "both"      # "scalping", "intraday", "both"

ORDER_TRANSLATIONS = {
    "Buy Stop": "إيقاف أمر الشراء",
    "Sell Stop": "إيقاف أمر البيع",
    "Buy Limit": "حد أمر الشراء",
    "Sell Limit": "حد أمر البيع",
    "Market Buy": "تنفيذ شراء مباشر",
    "Market Sell": "تنفيذ بيع مباشر"
}

STRATEGY_TYPES = {
    "scalping": "⚡️ السكالبينج الخاطف",
    "intraday": "📈 الاتجاه اليومي",
    "both":     "🚀 كلاهما معاً"
}

def get_all_api_keys():
    keys = []
    for k, v in os.environ.items():
        if k.startswith("TWELVE_DATA_API_KEY") and v.strip():
            if v.strip() not in keys:
                keys.append(v.strip())
    return keys

# ================= ================= =================
# 3. جلب البيانات والأخبار الاقتصادية بشكل آمن
# ================= ================= =================
def fetch_url(url, params=None, timeout=6):
    try:
        res = requests.get(url, params=params, timeout=timeout)
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

async def fetch_twelve_data(endpoint_name, extra_params=None):
    global current_key_idx
    api_keys = get_all_api_keys()
    if not api_keys:
        return None

    attempts = len(api_keys)
    for _ in range(attempts):
        if current_key_idx >= len(api_keys):
            current_key_idx = 0
            
        active_key = api_keys[current_key_idx]
        url = f"https://api.twelvedata.com/{endpoint_name}"
        
        queryParams = {"apikey": active_key}
        if extra_params:
            queryParams.update(extra_params)
            
        res = await asyncio.to_thread(fetch_url, url, queryParams, 6)
        
        if res and isinstance(res, dict) and res.get("status_code") == 429:
            current_key_idx = (current_key_idx + 1) % len(api_keys)
            continue
        return res
    return {"status_code": 429}

async def is_high_impact_news_near():
    if not news_filter_active:
        return False, ""
    try:
        url = "https://nfp.ourforecast.com/api/v1/events"
        data = await asyncio.to_thread(fetch_url, url, None, 5)
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

def calculate_recommended_lot(entry_price, stop_loss_price):
    try:
        risk_amount_cents = account_balance_cents * (risk_percentage / 100.0)
        price_distance = abs(entry_price - stop_loss_price)
        if price_distance == 0:
            return 0.10

        cost_per_point = 100.0
        raw_lot = risk_amount_cents / (price_distance * cost_per_point)
        return max(0.01, round(raw_lot, 2))
    except Exception:
        return 0.10

# ================= ================= =================
# 4. الحسابات الفنية والمؤشرات
# ================= ================= =================
def calculate_atr(highs, lows, closes, window=14):
    if len(closes) < window + 1: return 2.5
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    return sum(tr_list[-window:]) / window if tr_list else 2.5

def calculate_rsi(closes, window=14):
    if len(closes) < window + 1: return 50.0
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

# ================= ================= =================
# 5. محرك تحليل الذكاء الاصطناعي واختيار أمر الدخول
# ================= ================= =================
async def analyze_gold_market():
    global active_orders
    
    if datetime.utcnow().weekday() in [5, 6]:
        return [], "الماركت مغلق (عطلة نهاية الأسبوع)", 0, 0
        
    has_news, news_title = await is_high_impact_news_near()
    if has_news:
        return [], f"توقف مؤقت بسبب خبر عالي التأثير: {news_title}", 0, 0

    res_m15 = await fetch_twelve_data("time_series", {"symbol": SYMBOL, "interval": "15min", "outputsize": "100"})
    res_h1  = await fetch_twelve_data("time_series", {"symbol": SYMBOL, "interval": "1h", "outputsize": "50"})

    if not res_m15 or "values" not in res_m15 or not res_h1 or "values" not in res_h1:
        return [], "خطأ في الاتصال بالبيانات", 0, 0

    m15_vals = res_m15["values"]
    m15_vals.reverse()
    closes_m15 = [float(x["close"]) for x in m15_vals]
    highs_m15  = [float(x["high"]) for x in m15_vals]
    lows_m15   = [float(x["low"]) for x in m15_vals]
    
    h1_vals = res_h1["values"]
    h1_vals.reverse()
    closes_h1 = [float(x["close"]) for x in h1_vals]

    spot_price = round(closes_m15[-1], 2)
    atr = calculate_atr(highs_m15, lows_m15, closes_m15, window=14)
    rsi_m15 = calculate_rsi(closes_m15, window=14)
    
    ema20_h1 = calculate_ema(closes_h1, 20)[-1]
    ema50_h1 = calculate_ema(closes_h1, 50)[-1]
    h1_bullish = ema20_h1 > ema50_h1
    h1_bearish = ema20_h1 < ema50_h1

    ema20_m15 = calculate_ema(closes_m15, 20)[-1]
    ema50_m15 = calculate_ema(closes_m15, 50)[-1]

    ema_distance = abs(spot_price - ema20_m15)
    is_overextended = ema_distance > (atr * 1.8)

    swing_high = max(highs_m15[-20:-1])
    swing_low  = min(lows_m15[-20:-1])
    price_step = round(max(0.30, atr * 0.25), 2)

    generated_orders = []
    order_key = f"{SYMBOL}_AI_EXEC"

    if order_key in active_orders:
        return [], "يوجد أمر نشط حالياً للذهب", spot_price, rsi_m15

    # الشراء
    if h1_bullish and (ema20_m15 > ema50_m15) and (35 <= rsi_m15 <= 62):
        if is_overextended or rsi_m15 > 56:
            raw_type = "Buy Limit"
            entry_p = round(max(ema20_m15, swing_low + (atr * 0.5)), 2)
            sl_p    = round(entry_p - (atr * 1.3), 2)
            tp_p    = round(entry_p + (abs(entry_p - sl_p) * 2.0), 2)
            reason  = "إعادة اختبار بعد ارتداد وتراجع الزخم المباشر"
        elif spot_price <= (ema20_m15 + 0.5) and closes_m15[-1] > closes_m15[-2]:
            raw_type = "Market Buy"
            entry_p = spot_price
            sl_p    = round(swing_low - (atr * 1.0), 2)
            tp_p    = round(entry_p + (abs(entry_p - sl_p) * 1.8), 2)
            reason  = "ارتداد مكتمل من المتوسط وتأكيد الشمعة الانعكاسية"
        else:
            raw_type = "Buy Stop"
            entry_p = round(swing_high + price_step, 2)
            sl_p    = round(swing_low - (atr * 1.0), 2)
            tp_p    = round(entry_p + (abs(entry_p - sl_p) * 1.8), 2)
            reason  = "اختراق اختراقي مع زخم متوازن للاتجاه الصاعد"

        rec_lot = calculate_recommended_lot(entry_p, sl_p)
        order_obj = {
            "order_key": order_key, "symbol": SYMBOL,
            "type_raw": raw_type, "type_ar": ORDER_TRANSLATIONS[raw_type],
            "entry": entry_p, "tp": tp_p, "sl": sl_p,
            "lot": rec_lot, "rsi": rsi_m15, "reason": reason
        }
        active_orders[order_key] = order_obj
        generated_orders.append(order_obj)

    # البيع
    elif h1_bearish and (ema20_m15 < ema50_m15) and (38 <= rsi_m15 <= 68):
        if is_overextended or rsi_m15 < 43:
            raw_type = "Sell Limit"
            entry_p = round(min(ema20_m15, swing_high - (atr * 0.5)), 2)
            sl_p    = round(entry_p + (atr * 1.3), 2)
            tp_p    = round(entry_p - (abs(sl_p - entry_p) * 2.0), 2)
            reason  = "انتظار إعادة اختبار المقاومة لتجنب البيع عند القاع"
        elif spot_price >= (ema20_m15 - 0.5) and closes_m15[-1] < closes_m15[-2]:
            raw_type = "Market Sell"
            entry_p = spot_price
            sl_p    = round(swing_high + (atr * 1.0), 2)
            tp_p    = round(entry_p - (abs(sl_p - entry_p) * 1.8), 2)
            reason  = "تأكيد الدوران من منطقة المقاومة بشمعة انعكاسية"
        else:
            raw_type = "Sell Stop"
            entry_p = round(swing_low - price_step, 2)
            sl_p    = round(swing_high + (atr * 1.0), 2)
            tp_p    = round(entry_p - (abs(sl_p - entry_p) * 1.8), 2)
            reason  = "اختراق هابط مع حجم وزخم سليم دون تشبع بيعي"

        rec_lot = calculate_recommended_lot(entry_p, sl_p)
        order_obj = {
            "order_key": order_key, "symbol": SYMBOL,
            "type_raw": raw_type, "type_ar": ORDER_TRANSLATIONS[raw_type],
            "entry": entry_p, "tp": tp_p, "sl": sl_p,
            "lot": rec_lot, "rsi": rsi_m15, "reason": reason
        }
        active_orders[order_key] = order_obj
        generated_orders.append(order_obj)

    return generated_orders, "تم الفحص بنجاح", spot_price, rsi_m15

# ================= ================= =================
# 6. بناء قائمة الإعدادات والأزرار التفاعلية
# ================= ================= =================
def build_settings_text():
    return (
        f"⚙️ **قائمة إعدادات البوت الذكي:**\n\n"
        f"🟡 **الأصل المتداول:** `الذهب XAU/USD`\n"
        f"🎯 **الاستراتيجية:** {STRATEGY_TYPES.get(active_strategy, '🚀 كلاهما معاً')}\n"
        f"💰 **رصيد الحساب:** `{account_balance_cents}` سنت\n"
        f"🛡 **نسبة المخاطرة:** `{risk_percentage}%` لكل صفقة\n"
        f"📰 **فلتر الأخبار:** `{'مفعل ✅' if news_filter_active else 'معطل ❌'}`"
    )

def build_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡️ السكالبينج الخاطف", callback_data="set_strat_scalping"),
         InlineKeyboardButton("📈 الاتجاه اليومي", callback_data="set_strat_intraday")],
        [InlineKeyboardButton("🚀 كلاهما معاً", callback_data="set_strat_both")],
        [InlineKeyboardButton("📰 تبديل فلتر الأخبار", callback_data="toggle_news")]
    ])

# ================= ================= =================
# 7. المهام الخلفية واستقبال الأوامر
# ================= ================= =================
async def market_scanner_loop(bot: Bot, chat_id: str):
    global last_scanned_candle
    while True:
        try:
            now = datetime.utcnow()
            if now.minute % 15 == 0:
                candle_id = now.strftime("%Y-%m-%d %H:%M")
                if candle_id != last_scanned_candle:
                    last_scanned_candle = candle_id
                    
                    orders, status, spot_price, rsi = await analyze_gold_market()
                    
                    # إرسال إشارة الصفقة فور توفرها (الأولوية العظمى)
                    for order in orders:
                        emoji = "🔴" if "بيع" in order['type_ar'] else "🟢"
                        msg = (
                            f"🤖 **إشارة صفقة جديدة [الذهب XAU/USD]**\n"
                            f"⏱ **التوقيت:** {now.strftime('%H:%M')} UTC\n\n"
                            f"📊 **السعر الحالي:** `{spot_price}` | **RSI:** `{rsi}`\n"
                            f"{emoji} **نوع الأمر:** `{order['type_ar']}`\n"
                            f"📍 **نقطة الدخول:** `{order['entry']}`\n"
                            f"🟢 **أخذ الربح (TP):** `{order['tp']}`\n"
                            f"🔴 **وقف الخسارة (SL):** `{order['sl']}`\n\n"
                            f"💰 **حجم العقد (Lot):** `{order['lot']}`\n"
                            f"💡 **السبب:** _{order['reason']}_"
                        )
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    await asyncio.sleep(1)
        except Exception as e:
            print(f"Scanner Loop Error: {e}")
        await asyncio.sleep(5)

async def heartbeat_loop(bot: Bot, chat_id: str):
    last_sent_minute = -1
    while True:
        try:
            now = datetime.utcnow()
            if send_status_reports and now.minute % 30 == 0 and now.minute != last_sent_minute:
                last_sent_minute = now.minute
                msg = f"🟢 **مُحرّك الذهب الذكي:** يعمل بنجاح وضمان وصول الإشارات مفعل ({now.strftime('%H:%M')} UTC)"
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            await asyncio.sleep(10)
        except Exception:
            await asyncio.sleep(30)

# معالجة أمر Settings والإعدادات
async def handle_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")

async def handle_manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **جاري تحليل سوق الذهب (XAU/USD)...**", parse_mode="Markdown")
    orders, status, spot_price, rsi = await analyze_gold_market()
    
    report = (
        f"🏆 **تقرير الذهب الفوري (XAU/USD):**\n\n"
        f"💵 **السعر الحالي:** `{spot_price}`\n"
        f"📈 **RSI (15m):** `{rsi}`\n"
        f"⚙️ **الحالة:** `{status}`\n"
    )
    if orders:
        ord_info = orders[0]
        report += (
            f"\n🎯 **صفقة مكتشفة:**\n"
            f"• **الأمر:** `{ord_info['type_ar']}`\n"
            f"• **الدخول:** `{ord_info['entry']}`\n"
            f"• **TP:** `{ord_info['tp']}` | **SL:** `{ord_info['sl']}`\n"
            f"• **اللوت:** `{ord_info['lot']}`"
        )
    else:
        report += "\n✋ **لا توجد صفقة مكتملة الشروط الآن. البوت يراقب السوق.**"
        
    await update.message.reply_text(report, parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_strategy, news_filter_active
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("set_strat_"):
        active_strategy = query.data.replace("set_strat_", "")
    elif query.data == "toggle_news":
        news_filter_active = not news_filter_active
        
    try:
        await query.edit_message_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")
    except Exception:
        pass

async def post_init(application: Application):
    chat_id = os.environ.get("CHAT_ID")
    if chat_id:
        asyncio.create_task(market_scanner_loop(application.bot, chat_id))
        asyncio.create_task(heartbeat_loop(application.bot, chat_id))

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token: return
    Thread(target=run_web_server, daemon=True).start()
    app_bot = Application.builder().token(token).post_init(post_init).build()
    
    # معالجات الأوامر
    app_bot.add_handler(CommandHandler(["settings", "الاعدادات", "إعدادات"], handle_settings_command))
    app_bot.add_handler(MessageHandler(filters.Regex(r'(?i)^/?(settings|الاعدادات|إعدادات)$'), handle_settings_command))
    
    app_bot.add_handler(MessageHandler(filters.Regex(r'(?i)^/?scan$'), handle_manual_scan))
    app_bot.add_handler(CallbackQueryHandler(button_callback))
    
    app_bot.run_polling()

if __name__ == "__main__":
    main()
