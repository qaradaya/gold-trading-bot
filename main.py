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
    return "XAUUSD Dual VIP Smart Engine is Live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= ================= =================
# 2. المتغيرات العامة والعدادات
# ================= ================= =================
SYMBOL = "XAU/USD"
news_filter_active = True      
account_balance_cents = 200000 
risk_percentage = 0.5          
current_key_idx = 0
last_scanned_candle = ""     
active_strategy = "both"       # "scalping", "intraday", "both"

# عدادات تسلسلية مميزة
scalp_id_counter = 200
trend_id_counter = 500

# سجل الصفقات لمنع التكرار القريب
recent_sent_signals = {} 

ORDER_TRANSLATIONS = {
    "Market Buy": "تنفيذ شراء مباشر (Market Buy)",
    "Market Sell": "تنفيذ بيع مباشر (Market Sell)",
    "Buy Limit": "حد شراء مع التصحيح (Buy Limit)",
    "Sell Limit": "حد بيع مع التصحيح (Sell Limit)"
}

STRATEGY_NAMES = {
    "scalping": "⚡️ السكالبينج الخاطف الذكي (Scalp VIP)",
    "intraday": "📈 الاتجاه اليومي الفائق (Trend VIP)",
    "both":     "🚀 الاستراتيجيتين معاً (VIP Dual)"
}

def get_all_api_keys():
    keys = []
    for k, v in os.environ.items():
        if k.startswith("TWELVE_DATA_API_KEY") and v.strip():
            if v.strip() not in keys:
                keys.append(v.strip())
    return keys

# ================= ================= =================
# 3. جلب البيانات والتحقق من الجلسات والسيولة
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

def is_liquidity_session():
    """التحقق من أوقات سيولة لندن ونيويورك (07:00 إلى 17:00 UTC)"""
    now_utc = datetime.utcnow()
    if now_utc.weekday() in [5, 6]:
        return False, "الماركت مغلق (عطلة نهاية الأسبوع)"
    
    hour = now_utc.hour
    if 7 <= hour < 17:
        return True, "جلسة تداول نشطة (لندن / نيويورك)"
    else:
        return False, "خارج أوقات السيولة (يتم تجاهل التذبذب العشوائي)"

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

def is_near_duplicate(strategy_tag, raw_type, entry_price):
    """منع إرسال صفقات مكررة بنفس السعر والاتجاه لنفس الاستراتيجية خلال ساعتين"""
    if strategy_tag in recent_sent_signals:
        last_sig = recent_sent_signals[strategy_tag]
        if last_sig["type"] == raw_type and abs(entry_price - last_sig["entry"]) < 1.50:
            if (time.time() - last_sig["time"]) < 7200:
                return True
    return False

# ================= ================= =================
# 4. الحسابات الفنية والمؤشرات
# ================= ================= =================
def calculate_ema(data, window):
    if not data or len(data) < window: return [0] * len(data)
    alpha = 2 / (window + 1)
    ema = [sum(data[:window]) / window]
    for price in data[window:]:
        ema.append((price * alpha) + (ema[-1] * (1 - alpha)))
    return ([ema[0]] * (window - 1)) + ema

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

def calculate_recommended_lot(entry_price, stop_loss_price):
    try:
        risk_amount_cents = account_balance_cents * (risk_percentage / 100.0)
        price_distance = abs(entry_price - stop_loss_price)
        if price_distance == 0: return 0.10
        cost_per_point = 100.0
        raw_lot = risk_amount_cents / (price_distance * cost_per_point)
        return max(0.01, round(raw_lot, 2))
    except Exception:
        return 0.10

# ================= ================= =================
# 5. محرك التحليل المزدوج الذكي (Dual VIP Engine)
# ================= ================= =================
async def analyze_gold_market_dual_engine():
    session_ok, session_msg = is_liquidity_session()
    if not session_ok:
        return [], session_msg, 0, 0

    has_news, news_title = await is_high_impact_news_near()
    if has_news:
        return [], f"توقف بسبب خبر عالي التأثير: {news_title}", 0, 0

    res_h4  = await fetch_twelve_data("time_series", {"symbol": SYMBOL, "interval": "4h", "outputsize": "100"})
    res_h1  = await fetch_twelve_data("time_series", {"symbol": SYMBOL, "interval": "1h", "outputsize": "100"})
    res_m15 = await fetch_twelve_data("time_series", {"symbol": SYMBOL, "interval": "15min", "outputsize": "100"})

    if not res_h4 or "values" not in res_h4 or not res_h1 or "values" not in res_h1 or not res_m15 or "values" not in res_m15:
        return [], "خطأ في الاتصال بالبيانات", 0, 0

    # fريم H4
    vals_h4 = res_h4["values"][::-1]
    closes_h4 = [float(x["close"]) for x in vals_h4]
    ema200_h4 = calculate_ema(closes_h4, 200)[-1] if len(closes_h4) >= 200 else calculate_ema(closes_h4, 50)[-1]
    h4_bullish = closes_h4[-1] > ema200_h4
    h4_bearish = closes_h4[-1] < ema200_h4

    # فريم H1
    vals_h1 = res_h1["values"][::-1]
    closes_h1 = [float(x["close"]) for x in vals_h1]
    ema50_h1  = calculate_ema(closes_h1, 50)[-1]
    ema200_h1 = calculate_ema(closes_h1, 200)[-1] if len(closes_h1) >= 200 else calculate_ema(closes_h1, 100)[-1]
    h1_bullish = closes_h1[-1] > ema50_h1 and ema50_h1 > ema200_h1
    h1_bearish = closes_h1[-1] < ema50_h1 and ema50_h1 < ema200_h1

    # فريم M15
    vals_m15 = res_m15["values"][::-1]
    closes_m15 = [float(x["close"]) for x in vals_m15]
    highs_m15  = [float(x["high"]) for x in vals_m15]
    lows_m15   = [float(x["low"]) for x in vals_m15]

    spot_price = round(closes_m15[-1], 2)
    atr_m15 = calculate_atr(highs_m15, lows_m15, closes_m15, window=14)
    rsi_m15 = calculate_rsi(closes_m15, window=14)
    ema20_m15 = calculate_ema(closes_m15, 20)[-1]
    ema50_m15 = calculate_ema(closes_m15, 50)[-1]

    generated_orders = []

    # ---------------------------------------------------------
    # ⚡️ المحرك الأول: السكالبينج الخاطف الذكي (Scalp VIP)
    # ---------------------------------------------------------
    if active_strategy in ["scalping", "both"]:
        # السكالبينج الشرائي: يتطلب اتفاق اتجاه H1 الصاعد + ارتداد خاطف M15
        if h1_bullish and (closes_m15[-1] > closes_m15[-2]):
            if (ema20_m15 > ema50_m15) and (38 <= rsi_m15 <= 55) and abs(spot_price - ema20_m15) <= (atr_m15 * 0.6):
                raw_type = "Market Buy"
                entry_p = spot_price
                if not is_near_duplicate("scalping", raw_type, entry_p):
                    sl_p    = round(min(lows_m15[-3:]) - (atr_m15 * 0.4), 2)
                    risk_d  = abs(entry_p - sl_p)
                    if 1.5 <= risk_d <= 4.0: # ستوب محكم للسكالبينج
                        tp_p    = round(entry_p + (risk_d * 1.5), 2) # RRR 1:1.5
                        rec_lot = calculate_recommended_lot(entry_p, sl_p)
                        reason  = "⚡️ ارتداد خاطف مع اتجاه H1 الصاعد + اعادة تجميع RSI"
                        
                        generated_orders.append({
                            "type_raw": raw_type, "type_ar": ORDER_TRANSLATIONS[raw_type],
                            "entry": entry_p, "tp": tp_p, "sl": sl_p, "lot": rec_lot,
                            "strategy_tag": "scalping", "strategy_name_ar": "⚡️ السكالبينج الخاطف الذكي (Scalp VIP)",
                            "reason": reason
                        })

        # السكالبينج البيعي: يتطلب اتفاق اتجاه H1 الهابط + رفض خاطف M15
        elif h1_bearish and (closes_m15[-1] < closes_m15[-2]):
            if (ema20_m15 < ema50_m15) and (45 <= rsi_m15 <= 62) and abs(spot_price - ema20_m15) <= (atr_m15 * 0.6):
                raw_type = "Market Sell"
                entry_p = spot_price
                if not is_near_duplicate("scalping", raw_type, entry_p):
                    sl_p    = round(max(highs_m15[-3:]) + (atr_m15 * 0.4), 2)
                    risk_d  = abs(sl_p - entry_p)
                    if 1.5 <= risk_d <= 4.0:
                        tp_p    = round(entry_p - (risk_d * 1.5), 2)
                        rec_lot = calculate_recommended_lot(entry_p, sl_p)
                        reason  = "⚡️ رفض من المقاومة مع اتجاه H1 الهابط + ضغط بائعي خاطف"
                        
                        generated_orders.append({
                            "type_raw": raw_type, "type_ar": ORDER_TRANSLATIONS[raw_type],
                            "entry": entry_p, "tp": tp_p, "sl": sl_p, "lot": rec_lot,
                            "strategy_tag": "scalping", "strategy_name_ar": "⚡️ السكالبينج الخاطف الذكي (Scalp VIP)",
                            "reason": reason
                        })

    # ---------------------------------------------------------
    # 📈 المحرك الثاني: الاتجاه اليومي الفائق (Trend VIP)
    # ---------------------------------------------------------
    if active_strategy in ["intraday", "both"] and not generated_orders:
        if h4_bullish and h1_bullish:
            if spot_price <= (ema50_m15 + (atr_m15 * 0.4)) and (35 <= rsi_m15 <= 50):
                raw_type = "Market Buy"
                entry_p = spot_price
                if not is_near_duplicate("intraday", raw_type, entry_p):
                    sl_p    = round(min(lows_m15[-5:]) - (atr_m15 * 0.5), 2)
                    risk_d  = abs(entry_p - sl_p)
                    if 2.5 <= risk_d <= 7.0:
                        tp_p    = round(entry_p + (risk_d * 2.0), 2) # RRR 1:2.0
                        rec_lot = calculate_recommended_lot(entry_p, sl_p)
                        reason  = "📈 توافق فريم H4 و H1 الصاعد + إعادة اختبار منطقة الطلب الرئيسية"
                        
                        generated_orders.append({
                            "type_raw": raw_type, "type_ar": ORDER_TRANSLATIONS[raw_type],
                            "entry": entry_p, "tp": tp_p, "sl": sl_p, "lot": rec_lot,
                            "strategy_tag": "intraday", "strategy_name_ar": "📈 الاتجاه اليومي الفائق (Trend VIP)",
                            "reason": reason
                        })

        elif h4_bearish and h1_bearish:
            if spot_price >= (ema50_m15 - (atr_m15 * 0.4)) and (50 <= rsi_m15 <= 65):
                raw_type = "Market Sell"
                entry_p = spot_price
                if not is_near_duplicate("intraday", raw_type, entry_p):
                    sl_p    = round(max(highs_m15[-5:]) + (atr_m15 * 0.5), 2)
                    risk_d  = abs(sl_p - entry_p)
                    if 2.5 <= risk_d <= 7.0:
                        tp_p    = round(entry_p - (risk_d * 2.0), 2)
                        rec_lot = calculate_recommended_lot(entry_p, sl_p)
                        reason  = "📈 توافق فريم H4 و H1 الهابط + كسرة هيكل مع إعادة اختبار العرض"
                        
                        generated_orders.append({
                            "type_raw": raw_type, "type_ar": ORDER_TRANSLATIONS[raw_type],
                            "entry": entry_p, "tp": tp_p, "sl": sl_p, "lot": rec_lot,
                            "strategy_tag": "intraday", "strategy_name_ar": "📈 الاتجاه اليومي الفائق (Trend VIP)",
                            "reason": reason
                        })

    return generated_orders, f"مسح متكامل - {session_msg}", spot_price, rsi_m15

# ================= ================= =================
# 6. واجهة الإعدادات
# ================= ================= =================
def build_settings_text():
    keys_count = len(get_all_api_keys())
    news_status = "مفعل ✅" if news_filter_active else "معطل ❌"
    
    return (
        f"⚙️ **تقرير وإعدادات البوت الذكية (Dual VIP):**\n\n"
        f"🌐 **نطاق الأصول:** 🟡 `الذهب (XAU/USD) فقط`\n"
        f"🎯 **نمط التداول:** `{STRATEGY_NAMES.get(active_strategy, '🚀 الاستراتيجيتين معاً')}`\n"
        f"⏰ **فلتر الجلسات:** `لندن ونيويورك فقط (07:00 - 17:00 UTC)`\n"
        f"🟢 **فلتر الأخبار:** `{news_status}`\n"
        f"🎯 **نسبة المخاطرة:** `{risk_percentage}% لكل صفقة`\n"
        f"💰 **رصيد الحساب:** `{account_balance_cents} سنت`\n"
        f"🔑 **مفاتيح API الشغالة:** `{keys_count}`"
    )

def build_settings_keyboard():
    strat_text = f"🎯 نمط التداول: {STRATEGY_NAMES.get(active_strategy, '🚀 الاستراتيجيتين معاً')}"
    news_text = "🟢 فلتر الأخبار: مفعل" if news_filter_active else "🔴 فلتر الأخبار: معطل"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 النطاق: 🟡 الذهب فقط", callback_data="none")],
        [InlineKeyboardButton(strat_text, callback_data="toggle_strategy")],
        [InlineKeyboardButton(news_text, callback_data="toggle_news")],
        [InlineKeyboardButton(f"🎯 نسبة المخاطرة: {risk_percentage}%", callback_data="none")],
        [InlineKeyboardButton(f"💰 الرصيد: {account_balance_cents} سنت", callback_data="none")]
    ])

# ================= ================= =================
# 7. الحلقة الرئيسية والمهام
# ================= ================= =================
async def market_scanner_loop(bot: Bot, chat_id: str):
    global last_scanned_candle, scalp_id_counter, trend_id_counter, recent_sent_signals
    while True:
        try:
            now = datetime.utcnow()
            if now.minute % 15 == 0:
                candle_id = now.strftime("%Y-%m-%d %H:%M")
                if candle_id != last_scanned_candle:
                    last_scanned_candle = candle_id
                    
                    orders, status, spot_price, rsi = await analyze_gold_market_dual_engine()
                    
                    for order in orders:
                        if order["strategy_tag"] == "scalping":
                            scalp_id_counter += 1
                            order_id = f"#SCALP-VIP-{scalp_id_counter}"
                        else:
                            trend_id_counter += 1
                            order_id = f"#TREND-VIP-{trend_id_counter}"

                        emoji = "🔴" if "Sell" in order['type_raw'] else "🟢"
                        msg = (
                            f"🤖 **إشارة تداول ذكية [الذهب XAU/USD]**\n"
                            f"🆔 **المرجع:** `{order_id}`\n"
                            f"🎯 **الاستراتيجية:** `{order['strategy_name_ar']}`\n"
                            f"⏱ **التوقيت:** {now.strftime('%H:%M')} UTC\n\n"
                            f"📊 **السعر الحالي:** `{spot_price}` | **RSI:** `{rsi}`\n"
                            f"{emoji} **نوع الأمر:** `{order['type_ar']}`\n"
                            f"📍 **نقطة الدخول:** `{order['entry']}`\n"
                            f"🟢 **أخذ الربح (TP):** `{order['tp']}`\n"
                            f"🔴 **وقف الخسارة (SL):** `{order['sl']}`\n\n"
                            f"💰 **حجم العقد (Lot):** `{order['lot']}`\n"
                            f"💡 **السبب والتحليل:** _{order['reason']}_"
                        )
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

                        # حفظ الصفقة في السجل لمنع التكرار
                        recent_sent_signals[order["strategy_tag"]] = {
                            "entry": order["entry"],
                            "type": order["type_raw"],
                            "time": time.time()
                        }

                    await asyncio.sleep(1)
        except Exception as e:
            print(f"Scanner Loop Error: {e}")
        await asyncio.sleep(5)

async def handle_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")

async def handle_manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **جاري فحص سوق الذهب بالمحرك المزدوج الذكي...**", parse_mode="Markdown")
    orders, status, spot_price, rsi = await analyze_gold_market_dual_engine()
    
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
                f"• **السبب:** _{ord_info['reason']}_\n"
            )
    else:
        report += "\n✋ **لا توجد صفقة مستوفية للشروط الذكية القوية في الوقت الحالي.**"
        
    await update.message.reply_text(report, parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_strategy, news_filter_active
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
        
    try:
        await query.edit_message_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")
    except Exception:
        pass

async def post_init(application: Application):
    chat_id = os.environ.get("CHAT_ID")
    if chat_id:
        asyncio.create_task(market_scanner_loop(application.bot, chat_id))

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
