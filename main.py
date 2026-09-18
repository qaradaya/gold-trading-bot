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
    return "Semi-Auto MTF ZigZag Signal Bot is Live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= ================= =================
# 2. الإعدادات العامة والمتغيرات
# ================= ================= =================
class BotConfig:
    def __init__(self):
        self.symbol = "XAU/USD"
        self.enabled_timeframes = {"D1": True, "H4": True, "H1": True, "M30": True, "M15": True, "M5": True, "M1": True}
        self.custom_balance = 500.0        # الرصيد بالدولار أو السنت
        self.risk_percentage = 1.0         # نسبة المخاطرة %
        self.use_dynamic_lot = True        # True = حساب اللوت حسب المخاطرة, False = لوت ثابت
        self.fixed_lot = 0.15              # اللوت الثابت
        self.min_safety_distance = 0.50    # الحد الأدنى للمسافة عن السعر الحالي (للذهب)

config = BotConfig()
current_key_idx = 0

TF_MAP = {
    "D1": "1day",
    "H4": "4h",
    "H1": "1h",
    "M30": "30min",
    "M15": "15min",
    "M5": "5min",
    "M1": "1min"
}

def get_all_api_keys():
    keys = []
    for k, v in os.environ.items():
        if k.startswith("TWELVE_DATA_API_KEY") and v.strip():
            if v.strip() not in keys:
                keys.append(v.strip())
    return keys

# ================= ================= =================
# 3. جلب البيانات واستخراج مستويات الزيجزاق
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
        if extra_params: queryParams.update(extra_params)
            
        res = await asyncio.to_thread(fetch_url, url, queryParams, 6)
        if res and isinstance(res, dict) and res.get("status_code") == 429:
            current_key_idx = (current_key_idx + 1) % len(api_keys)
            continue
        return res
    return {"status_code": 429}

def extract_swing_levels(highs, lows):
    """استخراج قمم وقيعان مرتكزة (تعتمد كخطوط دعم ومقاومة زيجزاق ثابتة)"""
    levels = []
    length = len(highs)
    if length < 5:
        return levels
        
    for i in range(2, length - 2):
        # قمة زيجزاق مؤكدة
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            levels.append(round(highs[i], 2))
        # قاع زيجزاق مؤكد
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            levels.append(round(lows[i], 2))
            
    return levels

def calculate_recommended_lot(entry_price, sl_price):
    if not config.use_dynamic_lot:
        return config.fixed_lot
        
    try:
        risk_amount = config.custom_balance * (config.risk_percentage / 100.0)
        price_distance = abs(entry_price - sl_price)
        if price_distance <= 0: return config.fixed_lot
        
        # قيمة النقطة للذهب (تتعدل حسب الأصول الأخرى)
        cost_per_point = 100.0 if "XAU" in config.symbol else 100000.0
        raw_lot = risk_amount / (price_distance * cost_per_point)
        return max(0.01, round(raw_lot, 2))
    except Exception:
        return config.fixed_lot

# ================= ================= =================
# 4. محرك المسح المزدوج (Scan Engine)
# ================= ================= =================
async def run_zigzag_scan():
    all_levels = []
    spot_price = None

    # جمع المستويات من الفريمات المفعلة
    for tf_code, is_enabled in config.enabled_timeframes.items():
        if not is_enabled:
            continue
        
        td_interval = TF_MAP.get(tf_code, "15min")
        res = await fetch_twelve_data("time_series", {"symbol": config.symbol, "interval": td_interval, "outputsize": "60"})
        
        if res and "values" in res:
            vals = res["values"][::-1]
            highs = [float(x["high"]) for x in vals]
            lows  = [float(x["low"]) for x in vals]
            closes = [float(x["close"]) for x in vals]
            
            if spot_price is None:
                spot_price = round(closes[-1], 2)
                
            tf_levels = extract_swing_levels(highs, lows)
            all_levels.extend(tf_levels)

    if not all_levels or spot_price is None:
        return None, "⚠️ تعذر جلب المستويات، يرجى المحاولة بعد لحظات."

    # دمج المستويات المتقاربة
    sorted_levels = sorted(list(set(all_levels)))
    merged_levels = []
    for lvl in sorted_levels:
        if not merged_levels or abs(lvl - merged_levels[-1]) > 0.30:
            merged_levels.append(lvl)

    # تقسيم المستويات بالنسبة للسعر الحالي
    supports = [lvl for lvl in merged_levels if lvl < (spot_price - config.min_safety_distance)]
    resistances = [lvl for lvl in merged_levels if lvl > (spot_price + config.min_safety_distance)]

    if len(supports) < 2 or len(resistances) < 2:
        return None, "⚠️ المستويات المكتشفة غير كافية حول السعر الحالي لبناء صفقتين معلقتين."

    # تحديد أطراف الصفقتين
    buy_entry = supports[-1]
    buy_sl    = supports[-2]
    
    sell_entry = resistances[0]
    sell_sl    = resistances[1]

    buy_tp  = sell_entry # هدف الشراء هو المقاومة القادمة
    sell_tp = buy_entry  # هدف البيع هو الدعم القادم

    buy_lot  = calculate_recommended_lot(buy_entry, buy_sl)
    sell_lot = calculate_recommended_lot(sell_entry, sell_sl)

    scan_result = {
        "spot": spot_price,
        "symbol": config.symbol,
        "buy": {"entry": buy_entry, "tp": buy_tp, "sl": buy_sl, "lot": buy_lot},
        "sell": {"entry": sell_entry, "tp": sell_tp, "sl": sell_sl, "lot": sell_lot}
    }

    return scan_result, "OK"

# ================= ================= =================
# 5. لوحة التحكم والأوامر التفاعلية
# ================= ================= =================
def build_settings_text():
    tf_status = ", ".join([tf for tf, active in config.enabled_timeframes.items() if active])
    lot_mode_str = f"محسوب تلقائياً ({config.risk_percentage}%)" if config.use_dynamic_lot else f"ثابت ({config.fixed_lot})"
    
    return (
        f"⚙️ **لوحة تحكم إشارات الزيجزاق (النصف أوتوماتيك):**\n\n"
        f"🪙 **الزوج الحالي:** `{config.symbol}`\n"
        f"⏱️ **الفريمات المفعلة:** `{tf_status}`\n"
        f"💰 **الرصيد المعتمد:** `${config.custom_balance}`\n"
        f"🎯 **وضع اللوت:** `{lot_mode_str}`\n"
        f"🛡️ **مسافة الأمان عن السعر:** `${config.min_safety_distance}`\n\n"
        f"👇 _يمكنك التعديل مباشرة من الأزرار أدناه:_ "
    )

def build_settings_keyboard():
    # صف الفريمات
    tf_btns = []
    for tf, active in config.enabled_timeframes.items():
        label = f"✅ {tf}" if active else f"❌ {tf}"
        tf_btns.append(InlineKeyboardButton(label, callback_data=f"tf_{tf}"))

    # تقسيم زر الفريمات إلى صفين
    row1 = tf_btns[:4]
    row2 = tf_btns[4:]

    # أزرار الأصول
    symbol_btn = InlineKeyboardButton(f"🪙 الزوج: {config.symbol}", callback_data="toggle_symbol")
    
    # زر وضع اللوت
    lot_mode_label = "🎯 اللوت: محسوب %" if config.use_dynamic_lot else "🎯 اللوت: ثابت"
    lot_btn = InlineKeyboardButton(lot_mode_label, callback_data="toggle_lot_mode")
    
    # تعديل الرصيد والمخاطرة
    bal_btn = InlineKeyboardButton(f"💰 الرصيد: ${config.custom_balance}", callback_data="change_balance")
    risk_btn = InlineKeyboardButton(f"⚠️ المخاطرة: {config.risk_percentage}%", callback_data="change_risk")

    return InlineKeyboardMarkup([
        row1, row2,
        [symbol_btn, lot_btn],
        [bal_btn, risk_btn]
    ])

# ================= ================= =================
# 6. معالجة الرسائل والأزرار
# ================= ================= =================
async def handle_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_settings_text(), reply_markup=build_settings_keyboard(), parse_mode="Markdown")

async def handle_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **جاري قياس مستويات الزيجزاق وتحديد الصفقتين المعلقتين...**", parse_mode="Markdown")
    
    res, status = await run_zigzag_scan()
    if not res:
        await update.message.reply_text(status, parse_mode="Markdown")
        return

    msg = (
        f"🔍 **نتيجة مسح المستويات (الزيجزاق MTF)**\n"
        f"🪙 **الزوج:** `{res['symbol']}` | **السعر الحالي:** `{res['spot']}`\n\n"
        f"🟢 **1. أمر شراء معلق (Buy Limit):**\n"
        f"• **سعر الدخول:** `{res['buy']['entry']}`\n"
        f"• **أخذ الربح (TP):** `{res['buy']['tp']}`\n"
        f"• **وقف الخسارة (SL):** `{res['buy']['sl']}`\n"
        f"• **حجم العقد (Lot):** `{res['buy']['lot']}`\n\n"
        f"🔴 **2. أمر بيع معلق (Sell Limit):**\n"
        f"• **سعر الدخول:** `{res['sell']['entry']}`\n"
        f"• **أخذ الربح (TP):** `{res['sell']['tp']}`\n"
        f"• **وقف الخسارة (SL):** `{res['sell']['sl']}`\n"
        f"• **حجم العقد (Lot):** `{res['sell']['lot']}`\n\n"
        f"⚠️ **تذكير التنفيذ اليدوي:**\n"
        f"_عند تفعيل إحدى الصفقتين يدوياً على منصتك، قم بإلغاء الأمر المعلق الثاني فوراً._"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

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
    elif data == "change_balance":
        balances = [100.0, 200.0, 500.0, 1000.0, 2000.0]
        idx = (balances.index(config.custom_balance) + 1) % len(balances) if config.custom_balance in balances else 0
        config.custom_balance = balances[idx]
    elif data == "change_risk":
        risks = [0.5, 1.0, 1.5, 2.0]
        idx = (risks.index(config.risk_percentage) + 1) % len(risks) if config.risk_percentage in risks else 0
        config.risk_percentage = risks[idx]

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
    app_bot.add_handler(MessageHandler(filters.Regex(r'(?i)^/?(settings|الاعدادات|إعدادات)$'), handle_settings_command))
    app_bot.add_handler(MessageHandler(filters.Regex(r'(?i)^/?scan$'), handle_scan_command))
    app_bot.add_handler(CallbackQueryHandler(button_callback))
    
    app_bot.run_polling()

if __name__ == "__main__":
    main()
