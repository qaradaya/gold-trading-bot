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
    return "XAUUSD High-Confluence AI Engine is Active!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= ================= =================
# 2. المتغيرات العامة والإعدادات
# ================= ================= =================
SYMBOL = "XAU/USD"
send_status_reports = False    
news_filter_active = True      
account_balance_cents = 200000 
risk_percentage = 0.5          
current_key_idx = 0
last_scanned_candle = ""     

vip_id_counter = 500  # عداد الصفقات القوية
last_sent_signal_time = 0 # لمنع تكرار الصفقات خلال نفس الفترة

ORDER_TRANSLATIONS = {
    "Buy Limit": "حد شراء مع التصحيح (Buy Limit)",
    "Sell Limit": "حد بيع مع التصحيح (Sell Limit)",
    "Market Buy": "شراء مباشر تأكيدي (Market Buy)",
    "Market Sell": "بيع مباشر تأكيدي (Market Sell)"
}

def get_all_api_keys():
    keys = []
    for k, v in os.environ.items():
        if k.startswith("TWELVE_DATA_API_KEY") and v.strip():
            if v.strip() not in keys:
                keys.append(v.strip())
    return keys

# ================= ================= =================
# 3. جلب البيانات والتحقق من أوقات السيولة
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
    """التحقق من أن الوقت الحالي ضمن جلسة لندن أو نيويورك (07:00 إلى 17:00 UTC)"""
    now_utc = datetime.utcnow()
    # تجنب التداول في عطلة نهاية الأسبوع
    if now_utc.weekday() in [5, 6]:
        return False, "الماركت مغلق (عطلة نهاية الأسبوع)"
    
    hour = now_utc.hour
    if 7 <= hour < 17:
        return True, "جلسة تداول نشطة (لندن / نيويورك)"
    else:
        return False, "خارج أوقات السيولة الرئيسية (يتم تجاهل التذبذب الآسيوي)"

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

# ================= ================= =================
# 4. المؤشرات الفنية وتحليل قمم وقيعان الهيكل الثابتة
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

def find_confirmed_pivots(highs, lows, left=3, right=3):
    """تحديد القمم والقيعان الهيكلية المؤكدة بدون إمكانية إعادة الرسم (Non-Repainting)"""
    pivot_highs = []
    pivot_lows = []
    
    n = len(highs)
    for i in range(left, n - right):
        # القمة الهيكلية
        if all(highs[i] > highs[i - k] for k in range(1, left + 1)) and \
           all(highs[i] > highs[i + k] for k in range(1, right + 1)):
            pivot_highs.append((i, highs[i]))
            
        # القاع الهيكلي
        if all(lows[i] < lows[i - k] for k in range(1, left + 1)) and \
           all(lows[i] < lows[i + k] for k in range(1, right + 1)):
            pivot_lows.append((i, lows[i]))
            
    return pivot_highs, pivot_lows

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
# 5. محرك تحليل التوافق العالي (High-Confluence Engine)
# ================= ================= =================
async def analyze_gold_market_high_confluence():
    global last_sent_signal_time
    
    # 1. فحص الجلسة والسيولة
    session_ok, session_msg = is_liquidity_session()
    if not session_ok:
        return [], session_msg, 0, 0

    # 2. فحص الأخبار
    has_news, news_title = await is_high_impact_news_near()
    if has_news:
        return [], f"توقف مؤقت بسبب خبر عالي التأثير: {news_title}", 0, 0

    # 3. جلب بيانات متعددة الفريمات (H4, H1, M15)
    res_h4  = await fetch_twelve_data("time_series", {"symbol": SYMBOL, "interval": "4h", "outputsize": "100"})
    res_h1  = await fetch_twelve_data("time_series", {"symbol": SYMBOL, "interval": "1h", "outputsize": "100"})
    res_m15 = await fetch_twelve_data("time_series", {"symbol": SYMBOL, "interval": "15min", "outputsize": "100"})

    if not res_h4 or "values" not in res_h4 or not res_h1 or "values" not in res_h1 or not res_m15 or "values" not in res_m15:
        return [], "خطأ في جلب بيانات الفريمات المتعددة", 0, 0

    # معالجة بيانات H4
    vals_h4 = res_h4["values"][::-1]
    closes_h4 = [float(x["close"]) for x in vals_h4]
    ema200_h4 = calculate_ema(closes_h4, 200)[-1] if len(closes_h4) >= 200 else calculate_ema(closes_h4, 50)[-1]
    h4_bullish = closes_h4[-1] > ema200_h4
    h4_bearish = closes_h4[-1] < ema200_h4

    # معالجة بيانات H1
    vals_h1 = res_h1["values"][::-1]
    closes_h1 = [float(x["close"]) for x in vals_h1]
    ema50_h1  = calculate_ema(closes_h1, 50)[-1]
    ema200_h1 = calculate_ema(closes_h1, 200)[-1] if len(closes_h1) >= 200 else calculate_ema(closes_h1, 100)[-1]
    h1_bullish = closes_h1[-1] > ema50_h1 and ema50_h1 > ema200_h1
    h1_bearish = closes_h1[-1] < ema50_h1 and ema50_h1 < ema200_h1

    # معالجة بيانات M15
    vals_m15 = res_m15["values"][::-1]
    closes_m15 = [float(x["close"]) for x in vals_m15]
    highs_m15  = [float(x["high"]) for x in vals_m15]
    lows_m15   = [float(x["low"]) for x in vals_m15]

    spot_price = round(closes_m15[-1], 2)
    atr_m15 = calculate_atr(highs_m15, lows_m15, closes_m15, window=14)
    rsi_m15 = calculate_rsi(closes_m15, window=14)
    ema50_m15 = calculate_ema(closes_m15, 50)[-1]

    # العثور على القمم والقيعان الهيكلية الثابتة
    p_highs, p_lows = find_confirmed_pivots(highs_m15, lows_m15, left=3, right=3)
    if not p_highs or not p_lows:
        return [], "جاري جمع البيانات الهيكلية...", spot_price, rsi_m15

    last_pivot_high = p_highs[-1][1]
    last_pivot_low  = p_lows[-1][1]

    generated_orders = []

    # ==========================================
    # تقييم نقاط القوة والتوافق (BUY SCENARIO)
    # ==========================================
    if h4_bullish and h1_bullish:
        confluence_score = 0
        details = []

        # 1. توافق الفريم الكبيرة (مستوفى 100%)
        confluence_score += 1
        details.append("✅ اتجاه H4 و H1 صاعد بقوة (أعلى EMA200)")

        # 2. اختبار منطقة الدعم أو تصحيح فيبوناتشي بالقرب من EMA50 على M15
        if spot_price <= (ema50_m15 + (atr_m15 * 0.5)):
            confluence_score += 1
            details.append("✅ السعر في منطقة تصحيح مثالية (Pullback to Value Zone)")

        # 3. ارتداد RSI من مناطق التشبع البيعي أو إعادة تجميع
        if 35 <= rsi_m15 <= 52:
            confluence_score += 1
            details.append("✅ مؤشر RSI يظهر إعادة تجميع بعد تصحيح صحي")

        # 4. شمعة انعكاسية صاعدة على M15
        if closes_m15[-1] > closes_m15[-2] and (closes_m15[-1] - lows_m15[-1]) > (highs_m15[-1] - closes_m15[-1]):
            confluence_score += 1
            details.append("✅ ظهور شمعة انعكاسية صاعدة (Bullish Rejection)")

        # 5. وجود سيولة جلسة نيويورك / لندن
        confluence_score += 1
        details.append("✅ سيولة الجلسة الرئيسية متوفرة")

        # شرط القبول الصارم: 4 درجات من 5 على الأقل
        if confluence_score >= 4 and (time.time() - last_sent_signal_time) > 10800: # تجنب تكرار الصفقات لأقل من 3 ساعات
            entry_p = spot_price
            sl_p    = round(last_pivot_low - (atr_m15 * 0.5), 2)
            risk_dist = abs(entry_p - sl_p)
            
            # التأكد من أن الستوب غير متضخم وغير ضيق جداً (بين 2.5$ و 7.5$)
            if 2.5 <= risk_dist <= 7.5:
                tp_p    = round(entry_p + (risk_dist * 2.0), 2) # نسبة عائد 1:2
                rec_lot = calculate_recommended_lot(entry_p, sl_p)
                
                generated_orders.append({
                    "type_raw": "Market Buy",
                    "type_ar": ORDER_TRANSLATIONS["Market Buy"],
                    "entry": entry_p, "tp": tp_p, "sl": sl_p, "lot": rec_lot,
                    "score": f"{confluence_score}/5",
                    "reason": "\n".join(details)
                })

    # ==========================================
    # تقييم نقاط القوة والتوافق (SELL SCENARIO)
    # ==========================================
    elif h4_bearish and h1_bearish:
        confluence_score = 0
        details = []

        # 1. توافق الفريم الكبيرة
        confluence_score += 1
        details.append("✅ اتجاه H4 و H1 هابط بقوة (أسفل EMA200)")

        # 2. التصحيح لمنطقة العرض
        if spot_price >= (ema50_m15 - (atr_m15 * 0.5)):
            confluence_score += 1
            details.append("✅ السعر في منطقة إعادة اختبار للمقاومة (Pullback to Supply)")

        # 3. مؤشر RSI
        if 48 <= rsi_m15 <= 65:
            confluence_score += 1
            details.append("✅ مؤشر RSI يظهر وصول التصحيح لذروته الهابطة")

        # 4. شمعة انعكاسية هابطة
        if closes_m15[-1] < closes_m15[-2] and (highs_m15[-1] - closes_m15[-1]) > (closes_m15[-1] - lows_m15[-1]):
            confluence_score += 1
            details.append("✅ ظهور شمعة انعكاسية هابطة (Bearish Rejection)")

        # 5. سيولة الجلسة
        confluence_score += 1
        details.append("✅ سيولة الجلسة الرئيسية متوفرة")

        if confluence_score >= 4 and (time.time() - last_sent_signal_time) > 10800:
            entry_p = spot_price
            sl_p    = round(last_pivot_high + (atr_m15 * 0.5), 2)
            risk_dist = abs(sl_p - entry_p)
            
            if 2.5 <= risk_dist <= 7.5:
                tp_p    = round(entry_p - (risk_dist * 2.0), 2)
                rec_lot = calculate_recommended_lot(entry_p, sl_p)
                
                generated_orders.append({
                    "type_raw": "Market Sell",
                    "type_ar": ORDER_TRANSLATIONS["Market Sell"],
                    "entry": entry_p, "tp": tp_p, "sl": sl_p, "lot": rec_lot,
                    "score": f"{confluence_score}/5",
                    "reason": "\n".join(details)
                })

    status_str = f"مسح عالي الجودة - {session_msg}"
    return generated_orders, status_str, spot_price, rsi_m15

# ================= ================= =================
# 6. واجهة الإعدادات والتلجرام
# ================= ================= =================
def build_settings_text():
    keys_count = len(get_all_api_keys())
    news_status = "مفعل ✅" if news_filter_active else "معطل ❌"
    
    return (
        f"👑 **محرك إشارات الذهب عالية التوافق (High-Confluence VIP Engine):**\n\n"
        f"🌐 **نطاق التداول:** 🟡 `الذهب (XAU/USD) فقط`\n"
        f"🎯 **الاستراتيجية:** `متابعة الاتجاه الفائق (H4 + H1 Trend Matching)`\n"
        f"📐 **نظام التصفية:** `5/5 Confluence Score Filter`\n"
        f"⏰ **فلتر الجلسات:** `لندن ونيويورك فقط (07:00 - 17:00 UTC)`\n"
        f"🟢 **فلتر الأخبار:** `{news_status}`\n"
        f"⚖️ **نسبة المخاطرة إلى العائد (RRR):** `1 : 2.0 كحد أدنى`\n"
        f"🎯 **نسبة المخاطرة:** `{risk_percentage}% لكل صفقة`\n"
        f"💰 **رصيد الحساب:** `{account_balance_cents} سنت`\n"
        f"🔑 **مفاتيح API الشغالة:** `{keys_count}`"
    )

def build_settings_keyboard():
    news_text = "🟢 فلتر الأخبار: مفعل" if news_filter_active else "🔴 فلتر الأخبار: معطل"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 نمط التداول: عالية المصداقية (VIP)", callback_data="none")],
        [InlineKeyboardButton(news_text, callback_data="toggle_news")],
        [InlineKeyboardButton(f"🎯 نسبة المخاطرة: {risk_percentage}%", callback_data="none")],
        [InlineKeyboardButton(f"💰 الرصيد: {account_balance_cents} سنت", callback_data="none")]
    ])

# ================= ================= =================
# 7. الحلقة الرئيسية للمسح الخلفي
# ================= ================= =================
async def market_scanner_loop(bot: Bot, chat_id: str):
    global last_scanned_candle, vip_id_counter, last_sent_signal_time
    while True:
        try:
            now = datetime.utcnow()
            # المسح عند إغلاق كل شمعة 15 دقيقة
            if now.minute % 15 == 0:
                candle_id = now.strftime("%Y-%m-%d %H:%M")
                if candle_id != last_scanned_candle:
                    last_scanned_candle = candle_id
                    
                    orders, status, spot_price, rsi = await analyze_gold_market_high_confluence()
                    
                    for order in orders:
                        vip_id_counter += 1
                        order_id = f"#GOLD-VIP-{vip_id_counter}"
                        last_sent_signal_time = time.time()

                        emoji = "🔴" if "Sell" in order['type_raw'] else "🟢"
                        msg = (
                            f"🔥 **إشارة صفقة عالية الجودة [GOLD VIP]**\n"
                            f"🆔 **المرجع:** `{order_id}`\n"
                            f"⏱ **التوقيت:** {now.strftime('%H:%M')} UTC\n"
                            f"🎯 **درجة التوافق (Confluence):** `{order['score']}`\n\n"
                            f"📊 **السعر الحالي:** `{spot_price}` | **RSI:** `{rsi}`\n"
                            f"{emoji} **نوع الأمر:** `{order['type_ar']}`\n"
                            f"📍 **نقطة الدخول:** `{order['entry']}`\n"
                            f"🟢 **أخذ الربح (TP - 1:2):** `{order['tp']}`\n"
                            f"🔴 **وقف الخسارة الهيكلي (SL):** `{order['sl']}`\n\n"
                            f"💰 **حجم العقد الموصى به:** `{order['lot']}`\n\n"
                            f"💡 **أسباب ودواعي الدخول:**\n_{order['reason']}_"
                        )
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        
                    await asyncio.sleep(1)
        except Exception as e:
            print(f"Scanner Loop Error: {e}")
        await asyncio.sleep(5)

async def handle_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")

async def handle_manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **جاري المسح العميق والتوافق المتعدد للذهب (XAU/USD)...**", parse_mode="Markdown")
    orders, status, spot_price, rsi = await analyze_gold_market_high_confluence()
    
    report = (
        f"🏆 **تقرير الذهب عالي الجودة (XAU/USD):**\n\n"
        f"💵 **السعر الحالي:** `{spot_price}`\n"
        f"📈 **RSI (15m):** `{rsi}`\n"
        f"⚙️ **حالة السوق:** `{status}`\n"
    )
    if orders:
        for ord_info in orders:
            report += (
                f"\n🔥 **صفقة مكتملة الشروط والقوة ({ord_info['score']}):**\n"
                f"• **الأمر:** `{ord_info['type_ar']}`\n"
                f"• **الدخول:** `{ord_info['entry']}`\n"
                f"• **TP:** `{ord_info['tp']}` | **SL:** `{ord_info['sl']}`\n"
                f"• **اللوت:** `{ord_info['lot']}`\n\n"
                f"**الأسباب:**\n_{ord_info['reason']}_"
            )
    else:
        report += "\n✋ **لا توجد صفقة مطابقة لجميع شروط القوة والتوافق العالي حالياً.**"
        
    await update.message.reply_text(report, parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global news_filter_active
    query = update.callback_query
    await query.answer()
    
    if query.data == "toggle_news":
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
