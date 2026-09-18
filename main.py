import os
import time
import asyncio
import requests
from threading import Thread
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ================= ================= =================
# 1. خادم Flask للحفاظ على الاستمرارية (Keep-Alive)
# ================= ================= =================
app = Flask(__name__)

@app.route('/')
@app.route('/ping')
def home():
    return "Semi-Auto Pivot Trading Bot is Running!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= ================= =================
# 2. إعدادات البوت وتخزين البيانات
# ================= ================= =================
class BotConfig:
    def __init__(self):
        self.symbol = "XAU/USD"
        self.enabled_timeframes = {"D1": True, "H4": True, "H1": True, "M30": False, "M15": False, "M5": False, "M1": False}
        self.custom_balance = 1000.0       # الرصيد المخصص (يمكن تغييره يدوياً)
        self.risk_percentage = 1.0         # نسبة المخاطرة %
        self.use_dynamic_lot = True        # True = حساب اللوت, False = لوت ثابت
        self.fixed_lot = 0.10              # اللوت الثابت
        self.awaiting_balance_input = False # حالة انتظار كتابة الرصيد

config = BotConfig()
current_key_idx = 0

TF_MAP = {
    "D1": "1day", "H4": "4h", "H1": "1h",
    "M30": "30min", "M15": "15min", "M5": "5min", "M1": "1min"
}

def get_all_api_keys():
    keys = []
    for k, v in os.environ.items():
        if k.startswith("TWELVE_DATA_API_KEY") and v.strip():
            if v.strip() not in keys:
                keys.append(v.strip())
    return keys

# ================= ================= =================
# 3. خوارزمية جلب البيانات وتحديد القمم والقيعان (Pivot High/Low)
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
    if not api_keys: return None

    attempts = len(api_keys)
    for _ in range(attempts):
        if current_key_idx >= len(api_keys): current_key_idx = 0
        active_key = api_keys[current_key_idx]
        url = f"https://api.twelvedata.com/{endpoint_name}"
        queryParams = {"apikey": active_key}
        if extra_params: queryParams.update(extra_params)
            
        res = await asyncio.to_thread(fetch_url, url, queryParams, 6)
        if res and isinstance(res, dict) and res.get("status_code") == 429:
            current_key_idx = (current_key_idx + 1) % len(api_keys)
            continue
        return res
    return {"status_code": 429}

def extract_chart_pivots(highs, lows):
    """
    استخراج قمم وقيعان الشارت المرتكزة الحقيقية (Real Chart Swing Highs & Lows)
    """
    pivot_highs = []
    pivot_lows = []
    n = len(highs)
    if n < 5: return pivot_highs, pivot_lows

    # البحث عن الارتكازات المباشرة (القمة أعلى من شمعتين قبلها وشمعتين بعدها)
    for i in range(2, n - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            pivot_highs.append(round(highs[i], 2))
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            pivot_lows.append(round(lows[i], 2))

    return pivot_highs, pivot_lows

def calculate_recommended_lot(entry_price, sl_price):
    if not config.use_dynamic_lot:
        return config.fixed_lot
    try:
        risk_amount = config.custom_balance * (config.risk_percentage / 100.0)
        price_distance = abs(entry_price - sl_price)
        if price_distance <= 0: return config.fixed_lot
        
        cost_per_point = 100.0 if "XAU" in config.symbol else 100000.0
        raw_lot = risk_amount / (price_distance * cost_per_point)
        return max(0.01, round(raw_lot, 2))
    except Exception:
        return config.fixed_lot

# ================= ================= =================
# 4. محرك تحليل المستويات الدقيق
# ================= ================= =================
async def run_pivot_scan():
    all_pivots_high = []
    all_pivots_low = []
    spot_price = None

    for tf_code, is_enabled in config.enabled_timeframes.items():
        if not is_enabled: continue
        
        td_interval = TF_MAP.get(tf_code, "15min")
        res = await fetch_twelve_data("time_series", {
            "symbol": config.symbol, 
            "interval": td_interval, 
            "outputsize": "150"
        })
        
        if res and "values" in res:
            vals = res["values"][::-1]
            highs = [float(x["high"]) for x in vals]
            lows  = [float(x["low"]) for x in vals]
            closes = [float(x["close"]) for x in vals]
            
            if spot_price is None:
                spot_price = round(closes[-1], 2)
                
            p_highs, p_lows = extract_chart_pivots(highs, lows)
            all_pivots_high.extend(p_highs)
            all_pivots_low.extend(p_lows)

    if spot_price is None:
        return None, "⚠️ تعذر اتصال البوت بمصدر البيانات، يرجى المحاولة لاحقاً."

    # تصفية وتصنيف القيعان (الدعوم) والقمم (المقاومات) المباشرة أسفل وأعلى السعر
    supports = sorted(list(set([p for p in all_pivots_low if p < spot_price])))
    resistances = sorted(list(set([p for p in all_pivots_high if p > spot_price])))

    # التحقق المباشر: يجب توفر قاعين حقيقيين وقمة حقيقية على الأقل
    if len(supports) < 2:
        return None, f"⚠️ لم يتم العثور على قيعان تداول سابقة كافية أسفل السعر الحالي ({spot_price}). يرجى تفعيل فريمات إضافية."
    
    if len(resistances) < 2:
        return None, f"⚠️ لم يتم العثور على قمم تداول سابقة كافية أعلى السعر الحالي ({spot_price}). يرجى تفعيل فريمات إضافية."

    # نقاط الشراء (الدخول = القاع الأول، الستوب = القاع الثاني الحقيقي)
    buy_entry = supports[-1]
    buy_sl    = supports[-2]

    # نقاط البيع (الدخول = القمة الأولى، الستوب = القمة الثانية الحقيقية)
    sell_entry = resistances[0]
    sell_sl    = resistances[1]

    buy_tp  = sell_entry
    sell_tp = buy_entry

    buy_lot  = calculate_recommended_lot(buy_entry, buy_sl)
    sell_lot = calculate_recommended_lot(sell_entry, sell_sl)

    return {
        "spot": spot_price,
        "symbol": config.symbol,
        "buy": {"entry": buy_entry, "tp": buy_tp, "sl": buy_sl, "lot": buy_lot},
        "sell": {"entry": sell_entry, "tp": sell_tp, "sl": sell_sl, "lot": sell_lot}
    }, "OK"

# ================= ================= =================
# 5. الواجهة البرمجية والأوامر
# ================= ================= =================
def build_settings_text():
    tf_status = ", ".join([tf for tf, active in config.enabled_timeframes.items() if active])
    lot_mode_str = f"محسوب تلقائياً ({config.risk_percentage}%)" if config.use_dynamic_lot else f"ثابت ({config.fixed_lot})"
    
    return (
        f"⚙️ **لوحة التحكم والتداول:**\n\n"
        f"🪙 **الزوج الحالي:** `{config.symbol}`\n"
        f"⏱️ **الفريمات المفعلة:** `{tf_status}`\n"
        f"💰 **الرصيد المعتمد:** `${config.custom_balance}`\n"
        f"🎯 **طريقة حساب اللوت:** `{lot_mode_str}`\n\n"
        f"💡 _لتغيير الرصيد لأي مبلغ مخصص، أرسل الأمر:_ `/balance 1500`"
    )

def build_settings_keyboard():
    tf_btns = []
    for tf, active in config.enabled_timeframes.items():
        label = f"✅ {tf}" if active else f"❌ {tf}"
        tf_btns.append(InlineKeyboardButton(label, callback_data=f"tf_{tf}"))

    symbol_btn = InlineKeyboardButton(f"🪙 الزوج: {config.symbol}", callback_data="toggle_symbol")
    lot_btn = InlineKeyboardButton("🎯 تغيير وضع اللوت", callback_data="toggle_lot_mode")
    risk_btn = InlineKeyboardButton(f"⚠️ المخاطرة: {config.risk_percentage}%", callback_data="change_risk")
    bal_btn = InlineKeyboardButton(f"✏️ تعديل الرصيد يدوياً", callback_data="prompt_balance")

    return InlineKeyboardMarkup([
        tf_btns[:4], tf_btns[4:],
        [symbol_btn, lot_btn],
        [bal_btn, risk_btn]
    ])

# ================= ================= =================
# 6. معالجة الرسائل وإدخال الرصيد اليدوي
# ================= ================= =================
async def handle_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")

async def handle_balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر تغيير الرصيد يدوياً بأي قيمة: /balance 1250"""
    try:
        if context.args:
            val = float(context.args[0])
            if val > 0:
                config.custom_balance = val
                await update.message.reply_text(f"✅ تم تحديث الرصيد المعتمد بنجاح إلى: **${val}**", parse_mode="Markdown")
                return
        await update.message.reply_text("⚠️ يرجى إدخال المبلغ بشكل صحيح، مثال:\n`/balance 1500`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("⚠️ يرجى كتابة رقم صحيح بعد الأمر، مثال:\n`/balance 750`", parse_mode="Markdown")

async def handle_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **جاري البحث عن القمم والقيعان الحقيقية وتحديد الصفقات...**", parse_mode="Markdown")
    
    res, status = await run_pivot_scan()
    if not res:
        await update.message.reply_text(status, parse_mode="Markdown")
        return

    msg = (
        f"🔍 **نتيجة تحليل القمم والقيعان (Pivot MTF)**\n"
        f"🪙 **الزوج:** `{res['symbol']}` | **السعر الحالي:** `{res['spot']}`\n"
        f"💵 **الرصيد المعتمد:** `${config.custom_balance}`\n\n"
        f"🟢 **1. أمر شراء معلق (Buy Limit):**\n"
        f"• **سعر الدخول:** `{res['buy']['entry']}` (قاع سابق)\n"
        f"• **أخذ الربح (TP):** `{res['buy']['tp']}`\n"
        f"• **وقف الخسارة (SL):** `{res['buy']['sl']}` (القاع التالي)\n"
        f"• **حجم العقد (Lot):** `{res['buy']['lot']}`\n\n"
        f"🔴 **2. أمر بيع معلق (Sell Limit):**\n"
        f"• **سعر الدخول:** `{res['sell']['entry']}` (قمة سابقة)\n"
        f"• **أخذ الربح (TP):** `{res['sell']['tp']}`\n"
        f"• **وقف الخسارة (SL):** `{res['sell']['sl']}` (القمة التالية)\n"
        f"• **حجم العقد (Lot):** `{res['sell']['lot']}`\n\n"
        f"⚠️ **تذكير:** عند تفعيل إحدى الصفقتين يدوياً، قم بإلغاء الأمر الثاني فوراً."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if config.awaiting_balance_input:
        try:
            val = float(text)
            if val > 0:
                config.custom_balance = val
                config.awaiting_balance_input = False
                await update.message.reply_text(f"✅ تم تعيين الرصيد إلى: **${val}**", parse_mode="Markdown")
                return
        except ValueError:
            pass
        config.awaiting_balance_input = False

    if text.lower() == "scan":
        await handle_scan_command(update, context)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("tf_"):
        tf = data.split("_")[1]
        config.enabled_timeframes[tf] = not config.enabled_timeframes[tf]
    elif data == "toggle_symbol":
        symbols = ["XAU/USD", "EUR/USD", "GBP/USD", "BTC/USD"]
        idx = (symbols.index(config.symbol) + 1) % len(symbols)
        config.symbol = symbols[idx]
    elif data == "toggle_lot_mode":
        config.use_dynamic_lot = not config.use_dynamic_lot
    elif data == "change_risk":
        risks = [0.5, 1.0, 1.5, 2.0]
        idx = (risks.index(config.risk_percentage) + 1) % len(risks) if config.risk_percentage in risks else 0
        config.risk_percentage = risks[idx]
    elif data == "prompt_balance":
        config.awaiting_balance_input = True
        await query.message.reply_text("✏️ **اكتب قيمة الرصيد الجديدة الآن في المحادثة:**\n(مثال: `1500` أو `250.5`)", parse_mode="Markdown")
        return

    try:
        await query.edit_message_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")
    except Exception:
        pass

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token: return
    Thread(target=run_web_server, daemon=True).start()
    
    app_bot = Application.builder().token(token).build()
    
    app_bot.add_handler(CommandHandler("settings", handle_settings_command))
    app_bot.add_handler(CommandHandler("balance", handle_balance_command))
    app_bot.add_handler(CommandHandler("scan", handle_scan_command))
    app_bot.add_handler(CallbackQueryHandler(button_callback))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    
    app_bot.run_polling()

if __name__ == "__main__":
    main()
