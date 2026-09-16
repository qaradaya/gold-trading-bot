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
# 2. المتغيرات العامة والإعدادات والعدادات
# ================= ================= =================
SYMBOL = "XAU/USD"
send_status_reports = False    # معطلة افتراضياً لإيقاف نبض الحياة الدوري
news_filter_active = True      
account_balance_cents = 200000 
risk_percentage = 0.5          
current_key_idx = 0
last_scanned_candle = ""     
active_strategy = "both"       # "scalping", "intraday", "both"

# العدادات التسلسلية لكل استراتيجية
scalp_id_counter = 100
intraday_id_counter = 100

# تتبع الصفقات لمنع التكرار وقائمة الأوامر المعلقة
pending_orders_tracker = []
recent_sent_signals = {} # { "scalping": {"entry": float, "type": str, "time": float}, ... }

ORDER_TRANSLATIONS = {
    "Buy Stop": "إيقاف أمر الشراء (Buy Stop)",
    "Sell Stop": "إيقاف أمر البيع (Sell Stop)",
    "Buy Limit": "حد أمر الشراء (Buy Limit)",
    "Sell Limit": "حد أمر البيع (Sell Limit)",
    "Market Buy": "تنفيذ شراء مباشر (Market Buy)",
    "Market Sell": "تنفيذ بيع مباشر (Market Sell)"
}

STRATEGY_TYPES = {
    "scalping": "⚡️ السكالبينج الخاطف",
    "intraday": "📈 الاتجاه اليومي",
    "both":     "🚀 الاستراتيجيتين معاً"
}

def get_all_api_keys():
    keys = []
    for k, v in os.environ.items():
        if k.startswith("TWELVE_DATA_API_KEY") and v.strip():
            if v.strip() not in keys:
                keys.append(v.strip())
    return keys

# ================= ================= =================
# 3. جلب البيانات والأخبار الاقتصادية
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
# 5. محرك تحليل الذكاء الاصطناعي مع منع التكرار القريب
# ================= ================= =================
def is_near_duplicate(strategy_tag, raw_type, entry_price):
    if strategy_tag in recent_sent_signals:
        last_sig = recent_sent_signals[strategy_tag]
        # إذا كانت بنفس الاتجاه وسعر الدخول بفارق أقل من 1.5$ ومرت أقل من ساعتين
        if last_sig["type"] == raw_type and abs(entry_price - last_sig["entry"]) < 1.50:
            if (time.time() - last_sig["time"]) < 7200:
                return True
    return False

async def analyze_gold_market():
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

    generated_orders = []

    # --- ⚡️ استراتيجية السكالبينج الخاطف ---
    if active_strategy in ["scalping", "both"]:
        if (ema20_m15 > ema50_m15 or rsi_m15 <= 42) and closes_m15[-1] > closes_m15[-2]:
            if abs(spot_price - ema20_m15) <= (atr * 0.8) and (30 <= rsi_m15 <= 65):
                raw_type = "Market Buy"
                entry_p = spot_price
                if not is_near_duplicate("scalping", raw_type, entry_p):
                    sl_p    = round(min(lows_m15[-3:]) - (atr * 0.5), 2)
                    tp_p    = round(entry_p + (abs(entry_p - sl_p) * 1.5), 2)
                    reason  = "⚡️ ارتداد خاطف وتأكيد زخم الشمعة الانعكاسية (Scalp)"
                    rec_lot = calculate_recommended_lot(entry_p, sl_p)
                    
                    generated_orders.append({
                        "symbol": SYMBOL, "type_raw": raw_type, "type_ar": ORDER_TRANSLATIONS[raw_type],
                        "entry": entry_p, "tp": tp_p, "sl": sl_p, "lot": rec_lot, "rsi": rsi_m15, "reason": reason,
                        "strategy_tag": "scalping", "strategy_name_ar": "⚡️ السكالبينج الخاطف"
                    })

        elif (ema20_m15 < ema50_m15 or rsi_m15 >= 58) and closes_m15[-1] < closes_m15[-2]:
            if abs(spot_price - ema20_m15) <= (atr * 0.8) and (35 <= rsi_m15 <= 70):
                raw_type = "Market Sell"
                entry_p = spot_price
                if not is_near_duplicate("scalping", raw_type, entry_p):
                    sl_p    = round(max(highs_m15[-3:]) + (atr * 0.5), 2)
                    tp_p    = round(entry_p - (abs(sl_p - entry_p) * 1.5), 2)
                    reason  = "⚡️ رفض من المقاوِمَة وانعطاف هابط خاطف (Scalp)"
                    rec_lot = calculate_recommended_lot(entry_p, sl_p)
                    
                    generated_orders.append({
                        "symbol": SYMBOL, "type_raw": raw_type, "type_ar": ORDER_TRANSLATIONS[raw_type],
                        "entry": entry_p, "tp": tp_p, "sl": sl_p, "lot": rec_lot, "rsi": rsi_m15, "reason": reason,
                        "strategy_tag": "scalping", "strategy_name_ar": "⚡️ السكالبينج الخاطف"
                    })

    # --- 📈 استراتيجية الاتجاه اليومي ---
    if active_strategy in ["intraday", "both"] and not generated_orders:
        if h1_bullish and (35 <= rsi_m15 <= 62):
            raw_type = "Buy Limit"
            entry_p = round(spot_price - (atr * 0.35) + 0.25, 2)
            if not is_near_duplicate("intraday", raw_type, entry_p):
                sl_p    = round(entry_p - (atr * 1.2), 2)
                tp_p    = round(entry_p + (abs(entry_p - sl_p) * 1.8), 2)
                reason  = "📈 حد شراء بالقرب من الدعم لتأكيد الاتجاه الصاعد"
                rec_lot = calculate_recommended_lot(entry_p, sl_p)
                
                generated_orders.append({
                    "symbol": SYMBOL, "type_raw": raw_type, "type_ar": ORDER_TRANSLATIONS[raw_type],
                    "entry": entry_p, "tp": tp_p, "sl": sl_p, "lot": rec_lot, "rsi": rsi_m15, "reason": reason,
                    "strategy_tag": "intraday", "strategy_name_ar": "📈 الاتجاه اليومي"
                })

        elif h1_bearish and (38 <= rsi_m15 <= 68):
            raw_type = "Sell Limit"
            entry_p = round(spot_price + (atr * 0.35), 2)
            if not is_near_duplicate("intraday", raw_type, entry_p):
                sl_p    = round(entry_p + (atr * 1.2), 2)
                tp_p    = round(entry_p - (abs(sl_p - entry_p) * 1.8), 2)
                reason  = "📈 حد بيع بالقرب من المقاومة لتأكيد الاتجاه الهابط"
                rec_lot = calculate_recommended_lot(entry_p, sl_p)
                
                generated_orders.append({
                    "symbol": SYMBOL, "type_raw": raw_type, "type_ar": ORDER_TRANSLATIONS[raw_type],
                    "entry": entry_p, "tp": tp_p, "sl": sl_p, "lot": rec_lot, "rsi": rsi_m15, "reason": reason,
                    "strategy_tag": "intraday", "strategy_name_ar": "📈 الاتجاه اليومي"
                })

    return generated_orders, "تم الفحص بنجاح", spot_price, rsi_m15

# ================= ================= =================
# 6. بناء لوحة الإعدادات (مطابقة تماماً للصورة)
# ================= ================= =================
def build_settings_text():
    keys_count = len(get_all_api_keys())
    news_status = "مفعل ✅" if news_filter_active else "معطل ❌"
    heartbeat_status = "مفعلة 🟢" if send_status_reports else "معطلة 🔴"
    
    return (
        f"⚙️ **تقرير وإعدادات البوت الحالية:**\n\n"
        f"🌐 **نطاق الأصول:** 🟡 `الذهب فقط`\n"
        f"🎯 **نمط الاستراتيجية:** {STRATEGY_TYPES.get(active_strategy, '🚀 الاستراتيجيتين معاً')}\n"
        f"🎛 **نمط المؤشرات:** `مرن (Flexible)`\n"
        f"📐 **تعديل الأهداف والستوب:** `تكيفي مفعل 🤖 (مسار مستقل لكل استراتيجية)`\n"
        f"🟢 **فلتر الأخبار:** `{news_status}`\n"
        f"🎯 **نسبة المخاطرة:** `{risk_percentage}% لكل صفقة`\n"
        f"💰 **رصيد الحساب:** `{account_balance_cents} سنت`\n"
        f"📡 **رسائل نبض الحياة:** `{heartbeat_status}`\n"
        f"🔑 **مفاتيح API المستكشفة:** `{keys_count}`"
    )

def build_settings_keyboard():
    strat_text = f"🎯 الاستراتيجية: {STRATEGY_TYPES.get(active_strategy, '🚀 الاستراتيجيتين معاً')}"
    news_text = "🟢 فلتر الأخبار: مفعل" if news_filter_active else "🔴 فلتر الأخبار: معطل"
    heartbeat_text = "🟢 تشغيل نبض الحياة" if send_status_reports else "🔴 تشغيل نبض الحياة"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 النطاق: 🟡 الذهب فقط", callback_data="none")],
        [InlineKeyboardButton(strat_text, callback_data="toggle_strategy")],
        [InlineKeyboardButton("🎛 النمط: مرن", callback_data="none")],
        [InlineKeyboardButton(heartbeat_text, callback_data="toggle_reports")],
        [InlineKeyboardButton(news_text, callback_data="toggle_news")],
        [InlineKeyboardButton(f"🎯 نسبة المخاطرة: {risk_percentage}%", callback_data="none")],
        [InlineKeyboardButton(f"💰 الرصيد: {account_balance_cents} سنت", callback_data="none")]
    ])

# ================= ================= =================
# 7. فحص صلاحية الأوامر المعلقة مع المرجع التسلسلي
# ================= ================= =================
async def check_and_cancel_expired_orders(bot: Bot, chat_id: str):
    global pending_orders_tracker
    current_time = time.time()
    remaining_orders = []

    for order in pending_orders_tracker:
        # إلغاء الأمر إذا مرت 45 دقيقة (2700 ثانية) ولم يتفعل
        if current_time - order["created_at"] >= 2700:
            cancel_msg = (
                f"⚠️ **تنبيه إلغاء أمر معلق [XAU/USD]**\n\n"
                f"🆔 **رقم الصفقة الملغاة:** `{order['order_id']}`\n"
                f"🎯 **الاستراتيجية:** `{order['strategy_name_ar']}`\n"
                f"❌ **نوع الأمر:** `{order['type_ar']}`\n"
                f"📍 **سعر الدخول المقترح:** `{order['entry']}`\n"
                f"⏱ **السبب:** انتهت الصلاحية (مرور 45 دقيقة دون ملامسة السعر لحماية الحساب)."
            )
            try:
                await bot.send_message(chat_id=chat_id, text=cancel_msg, parse_mode="Markdown")
            except Exception as e:
                print(f"Cancel Msg Error: {e}")
        else:
            remaining_orders.append(order)

    pending_orders_tracker = remaining_orders

# ================= ================= =================
# 8. المهام الخلفية واستقبال الأوامر
# ================= ================= =================
async def market_scanner_loop(bot: Bot, chat_id: str):
    global last_scanned_candle, pending_orders_tracker, scalp_id_counter, intraday_id_counter, recent_sent_signals
    while True:
        try:
            now = datetime.utcnow()
            await check_and_cancel_expired_orders(bot, chat_id)

            if now.minute % 15 == 0:
                candle_id = now.strftime("%Y-%m-%d %H:%M")
                if candle_id != last_scanned_candle:
                    last_scanned_candle = candle_id
                    
                    orders, status, spot_price, rsi = await analyze_gold_market()
                    
                    for order in orders:
                        # توليد رقم تسلسلي للمرجعية
                        if order["strategy_tag"] == "scalping":
                            scalp_id_counter += 1
                            order_id = f"#SCALP-{scalp_id_counter}"
                        else:
                            intraday_id_counter += 1
                            order_id = f"#INTRA-{intraday_id_counter}"

                        emoji = "🔴" if "بيع" in order['type_ar'] else "🟢"
                        msg = (
                            f"🤖 **إشارة صفقة جديدة [الذهب XAU/USD]**\n"
                            f"🆔 **رقم الصفقة:** `{order_id}`\n"
                            f"🎯 **الاستراتيجية:** `{order['strategy_name_ar']}`\n"
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

                        # حفظ الصفقة الأخيرة لمنع التكرار
                        recent_sent_signals[order["strategy_tag"]] = {
                            "entry": order["entry"],
                            "type": order["type_raw"],
                            "time": time.time()
                        }

                        # حفظ الأوامر المعلقة لتتبع إلغائها
                        if "Limit" in order["type_raw"] or "Stop" in order["type_raw"]:
                            pending_orders_tracker.append({
                                "order_id": order_id,
                                "strategy_name_ar": order["strategy_name_ar"],
                                "type_ar": order["type_ar"],
                                "entry": order["entry"],
                                "created_at": time.time()
                            })
                    await asyncio.sleep(1)
        except Exception as e:
            print(f"Scanner Loop Error: {e}")
        await asyncio.sleep(5)

async def heartbeat_loop(bot: Bot, chat_id: str):
    last_sent_minute = -1
    while True:
        try:
            now = datetime.utcnow()
            if send_status_reports and now.minute % 15 == 0 and now.minute != last_sent_minute:
                last_sent_minute = now.minute
                msg = f"🟢 **مُحرّك الذهب الذكي:** يعمل بنجاح ويقوم بالمسح الدوري كل 15 دقيقة ({now.strftime('%H:%M')} UTC)"
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            await asyncio.sleep(10)
        except Exception:
            await asyncio.sleep(30)

async def handle_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")

async def handle_manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **جاري فحص سوق الذهب (XAU/USD) الآن...**", parse_mode="Markdown")
    orders, status, spot_price, rsi = await analyze_gold_market()
    
    report = (
        f"🏆 **تقرير الذهب الفوري (XAU/USD):**\n\n"
        f"💵 **السعر الحالي:** `{spot_price}`\n"
        f"📈 **RSI (15m):** `{rsi}`\n"
        f"⚙️ **الحالة:** `{status}`\n"
    )
    if orders:
        for ord_info in orders:
            report += (
                f"\n🎯 **صفقة مكتشفة ({ord_info['strategy_name_ar']}):**\n"
                f"• **الأمر:** `{ord_info['type_ar']}`\n"
                f"• **الدخول:** `{ord_info['entry']}`\n"
                f"• **TP:** `{ord_info['tp']}` | **SL:** `{ord_info['sl']}`\n"
                f"• **اللوت:** `{ord_info['lot']}`\n"
            )
    else:
        report += "\n✋ **لا توجد صفقة جديدة مكتملة الشروط في هذه اللحظة.**"
        
    await update.message.reply_text(report, parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_strategy, news_filter_active, send_status_reports
    query = update.callback_query
    await query.answer()
    
    if query.data == "toggle_strategy":
        if active_strategy == "both":
            active_strategy = "scalping"
        elif active_strategy == "scalping":
            active_strategy = "intraday"
        else:
            active_strategy = "both"
    elif query.data == "toggle_news":
        news_filter_active = not news_filter_active
    elif query.data == "toggle_reports":
        send_status_reports = not send_status_reports
        
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
    
    app_bot.add_handler(CommandHandler("settings", handle_settings_command))
    app_bot.add_handler(MessageHandler(filters.Regex(r'(?i)^/?(settings|الاعدادات|إعدادات)$'), handle_settings_command))
    
    app_bot.add_handler(MessageHandler(filters.Regex(r'(?i)^/?scan$'), handle_manual_scan))
    app_bot.add_handler(CallbackQueryHandler(button_callback))
    
    app_bot.run_polling()

if __name__ == "__main__":
    main()
