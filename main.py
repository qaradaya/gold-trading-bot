import os
import asyncio
import requests
from threading import Thread
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# ================= Flask Keep-Alive =================
app = Flask(__name__)

@app.route('/')
@app.route('/ping')
def home():
    return "ZigZag Semi-Auto Bot is Active!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= Constants (Safety Filters) =================
MIN_ENTRY_DIST_PCT = 0.30   # أدنى مسافة % من السعر الحالي للدخول
MIN_SL_DIST_PCT    = 0.30   # أدنى مسافة % بين الدخول والستوب
MIN_RR             = 2.0    # نسبة الربح/المخاطرة الدنيا (Risk:Reward)
MAX_LOT            = 1.00   # سقف اللوت الأقصى

TF_PRIORITY = {"D1": 5, "H4": 4, "H1": 3, "M30": 2, "M15": 1, "M5": 1, "M1": 1}

# ================= Bot Config =================
class BotConfig:
    def __init__(self):
        self.symbol = "XAU/USD"
        self.enabled_timeframes = {
            "D1": True, "H4": True, "H1": True,
            "M30": False, "M15": False, "M5": False, "M1": False
        }
        self.custom_balance = 1000.0
        self.risk_percentage = 1.0
        self.use_dynamic_lot = True
        self.fixed_lot = 0.10
        self.zigzag_threshold = 0.30
        self.awaiting_balance_input = False

config = BotConfig()
current_key_idx = 0

TF_MAP = {
    "D1": "1day", "H4": "4h", "H1": "1h",
    "M30": "30min", "M15": "15min", "M5": "5min", "M1": "1min"
}

AVAILABLE_SYMBOLS = ["XAU/USD", "EUR/USD", "GBP/USD", "BTC/USD", "USD/JPY"]
RISK_OPTIONS = [0.5, 1.0, 1.5, 2.0]
ZIGZAG_OPTIONS = [0.15, 0.30, 0.50, 1.00]

# ================= Helpers =================
def get_symbol_precision(symbol):
    s = symbol.upper()
    if "JPY" in s:
        return 3
    if any(x in s for x in ("XAU", "BTC", "ETH")):
        return 2
    return 5

def get_cost_per_point(symbol):
    s = symbol.upper()
    if "XAU" in s:
        return 100.0
    if any(x in s for x in ("BTC", "ETH")):
        return 1.0
    return 100000.0

def get_all_api_keys():
    keys = []
    for k, v in os.environ.items():
        if k.startswith("TWELVE_DATA_API_KEY") and v.strip():
            if v.strip() not in keys:
                keys.append(v.strip())
    return keys

# ================= Data Fetching =================
def fetch_url(url, params=None, timeout=8):
    try:
        res = requests.get(url, params=params, timeout=timeout)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("code") == 429:
                return {"status_code": 429}
            return data
        if res.status_code == 429:
            return {"status_code": 429}
    except Exception as e:
        print(f"Fetch Error: {e}")
    return None

async def fetch_twelve_data(endpoint_name, extra_params=None):
    global current_key_idx
    api_keys = get_all_api_keys()
    if not api_keys:
        return None

    for _ in range(len(api_keys)):
        if current_key_idx >= len(api_keys):
            current_key_idx = 0
        active_key = api_keys[current_key_idx]
        url = f"https://api.twelvedata.com/{endpoint_name}"
        params = {"apikey": active_key}
        if extra_params:
            params.update(extra_params)

        res = await asyncio.to_thread(fetch_url, url, params, 8)
        if res and isinstance(res, dict) and res.get("status_code") == 429:
            current_key_idx = (current_key_idx + 1) % len(api_keys)
            continue
        return res
    return {"status_code": 429}

# ================= ZigZag Pivot Detection =================
def zigzag_pivots(highs, lows, precision, threshold_pct=0.3, skip_last=2):
    """نقاط انعكاس ثابتة (stable pivots) لا تعيد الرسم."""
    n = len(highs) - skip_last
    if n < 3:
        return [], []

    pivot_highs = []
    pivot_lows = []
    direction = None
    running_high = highs[0]
    running_low = lows[0]

    for i in range(1, n):
        h, l = highs[i], lows[i]

        if direction is None:
            if h > running_high:
                running_high = h
            if l < running_low:
                running_low = l
            up_move = (running_high - lows[0]) / lows[0] * 100 if lows[0] else 0
            down_move = (highs[0] - running_low) / highs[0] * 100 if highs[0] else 0
            if up_move >= threshold_pct and up_move >= down_move:
                direction = 'up'
            elif down_move >= threshold_pct:
                direction = 'down'
            continue

        if direction == 'up':
            if h > running_high:
                running_high = h
            if running_high > 0 and (running_high - l) / running_high * 100 >= threshold_pct:
                pivot_highs.append(round(running_high, precision))
                direction = 'down'
                running_low = l
        else:
            if l < running_low:
                running_low = l
            if running_low > 0 and (h - running_low) / running_low * 100 >= threshold_pct:
                pivot_lows.append(round(running_low, precision))
                direction = 'up'
                running_high = h

    return pivot_highs, pivot_lows

# ================= Lot Calculation =================
def calculate_recommended_lot(entry_price, sl_price, symbol):
    if not config.use_dynamic_lot:
        return min(config.fixed_lot, MAX_LOT)
    try:
        risk_amount = config.custom_balance * (config.risk_percentage / 100.0)
        price_distance = abs(entry_price - sl_price)
        if price_distance <= 0:
            return min(config.fixed_lot, MAX_LOT)
        cost = get_cost_per_point(symbol)
        raw_lot = risk_amount / (price_distance * cost)
        return min(MAX_LOT, max(0.01, round(raw_lot, 2)))
    except Exception:
        return min(config.fixed_lot, MAX_LOT)

def get_actual_risk(lot, entry, sl, symbol):
    return lot * abs(entry - sl) * get_cost_per_point(symbol)

# ================= Scan Engine (مع الفلاتر) =================
async def run_pivot_scan():
    all_highs = []   # قائمة (price, priority, tf_code)
    all_lows = []
    spot_price = None
    precision = get_symbol_precision(config.symbol)

    active_tfs = [tf for tf, on in config.enabled_timeframes.items() if on]
    if not active_tfs:
        return None, "⚠️ يجب تفعيل فريم واحد على الأقل."

    # 1) جلب البيانات من كل الفريمات واستخراج القمم/القيعان
    for tf_code in active_tfs:
        td_interval = TF_MAP.get(tf_code, "15min")
        res = await fetch_twelve_data("time_series", {
            "symbol": config.symbol,
            "interval": td_interval,
            "outputsize": "200"
        })

        if res and "values" in res:
            vals = res["values"][::-1]
            highs = [float(x["high"]) for x in vals]
            lows = [float(x["low"]) for x in vals]
            closes = [float(x["close"]) for x in vals]

            if spot_price is None:
                spot_price = round(closes[-1], precision)

            p_highs, p_lows = zigzag_pivots(
                highs, lows, precision,
                threshold_pct=config.zigzag_threshold,
                skip_last=2
            )
            pri = TF_PRIORITY.get(tf_code, 1)
            for p in p_highs:
                all_highs.append((p, pri, tf_code))
            for p in p_lows:
                all_lows.append((p, pri, tf_code))

    if spot_price is None:
        return None, "⚠️ تعذر الاتصال بمصدر البيانات، حاول لاحقاً."

    # 2) دمج المستويات المتقاربة (خلال 0.05%)
    def merge_levels(levels):
        if not levels:
            return []
        levels = sorted(levels, key=lambda x: x[0])
        merged = []
        for p, pri, tf in levels:
            found = False
            for i, (mp, mpri, mtfs) in enumerate(merged):
                if abs(p - mp) / mp * 100 <= 0.05:
                    merged[i] = ((p + mp) / 2, max(pri, mpri), mtfs | {tf})
                    found = True
                    break
            if not found:
                merged.append((p, pri, {tf}))
        return merged

    lows_merged = merge_levels(all_lows)
    highs_merged = merge_levels(all_highs)

    # 3) فلتر المسافة الدنيا من السعر الحالي
    supports = [
        (p, pri, tfs) for p, pri, tfs in lows_merged
        if p < spot_price and (spot_price - p) / spot_price * 100 >= MIN_ENTRY_DIST_PCT
    ]
    resistances = [
        (p, pri, tfs) for p, pri, tfs in highs_merged
        if p > spot_price and (p - spot_price) / spot_price * 100 >= MIN_ENTRY_DIST_PCT
    ]

    # 4) ترتيب حسب القرب من السعر (الأقرب أولاً)
    supports.sort(key=lambda x: spot_price - x[0])
    resistances.sort(key=lambda x: x[0] - spot_price)

    if len(supports) < 2:
        return None, (
            f"⚠️ <b>لم يتم العثور على دعوم كافية</b>\n\n"
            f"💵 السعر الحالي: <code>{spot_price:.{precision}f}</code>\n"
            f"📏 الحد الأدنى للمسافة: <code>{MIN_ENTRY_DIST_PCT}%</code>\n"
            f"🔎 عدد الدعوم المكتشفة: <code>{len(supports)}</code>\n\n"
            f"💡 <b>الحلول:</b>\n"
            f"• فعّل فريمات إضافية (H4, D1)\n"
            f"• قلل حساسية ZigZag إلى <code>0.15%</code>\n"
            f"• تحقق من الزوج المحدد"
        )
    if len(resistances) < 2:
        return None, (
            f"⚠️ <b>لم يتم العثور على مقاومات كافية</b>\n\n"
            f"💵 السعر الحالي: <code>{spot_price:.{precision}f}</code>\n"
            f"📏 الحد الأدنى للمسافة: <code>{MIN_ENTRY_DIST_PCT}%</code>\n"
            f"🔎 عدد المقاومات المكتشفة: <code>{len(resistances)}</code>\n\n"
            f"💡 <b>الحلول:</b>\n"
            f"• فعّل فريمات إضافية (H4, D1)\n"
            f"• قلل حساسية ZigZag إلى <code>0.15%</code>\n"
            f"• تحقق من الزوج المحدد"
        )

    # 5) اختيار الدخول + الستوب مع فلتر المسافة الدنيا للستوب
    def pick_entry_sl(levels, is_support):
        entry = levels[0]
        sl = None
        for lv in levels[1:]:
            if is_support:
                dist_pct = (entry[0] - lv[0]) / entry[0] * 100
            else:
                dist_pct = (lv[0] - entry[0]) / entry[0] * 100
            if dist_pct >= MIN_SL_DIST_PCT:
                sl = lv
                break
        if sl is None:
            # تمديد الستوب صناعياً لتلبية الحد الأدنى
            if is_support:
                sl_price = entry[0] * (1 - MIN_SL_DIST_PCT / 100)
            else:
                sl_price = entry[0] * (1 + MIN_SL_DIST_PCT / 100)
            sl = (sl_price, 0, {"auto"})
        return entry, sl

    buy_entry, buy_sl = pick_entry_sl(supports, is_support=True)
    sell_entry, sell_sl = pick_entry_sl(resistances, is_support=False)

    buy_entry_p = round(buy_entry[0], precision)
    buy_sl_p    = round(buy_sl[0], precision)
    sell_entry_p = round(sell_entry[0], precision)
    sell_sl_p    = round(sell_sl[0], precision)

    # 6) TP مستقل لكل صفقة بناءً على R:R
    buy_risk = buy_entry_p - buy_sl_p
    sell_risk = sell_sl_p - sell_entry_p
    buy_tp_p  = round(buy_entry_p + buy_risk * MIN_RR, precision)
    sell_tp_p = round(sell_entry_p - sell_risk * MIN_RR, precision)

    # 7) اللوت مع سقف أقصى
    buy_lot  = calculate_recommended_lot(buy_entry_p, buy_sl_p, config.symbol)
    sell_lot = calculate_recommended_lot(sell_entry_p, sell_sl_p, config.symbol)

    return {
        "spot": spot_price,
        "symbol": config.symbol,
        "precision": precision,
        "tfs": active_tfs,
        "buy": {
            "entry": buy_entry_p,
            "tp": buy_tp_p,
            "sl": buy_sl_p,
            "lot": buy_lot,
            "risk": get_actual_risk(buy_lot, buy_entry_p, buy_sl_p, config.symbol),
            "source_tfs": ", ".join(sorted(buy_entry[2])),
        },
        "sell": {
            "entry": sell_entry_p,
            "tp": sell_tp_p,
            "sl": sell_sl_p,
            "lot": sell_lot,
            "risk": get_actual_risk(sell_lot, sell_entry_p, sell_sl_p, config.symbol),
            "source_tfs": ", ".join(sorted(sell_entry[2])),
        },
    }, "OK"

# ================= UI Builders =================
def build_settings_text():
    tf_status = ", ".join([tf for tf, on in config.enabled_timeframes.items() if on]) or "لا يوجد"
    lot_mode = (
        f"تلقائي ({config.risk_percentage}%)" if config.use_dynamic_lot
        else f"ثابت ({config.fixed_lot})"
    )
    return (
        f"⚙️ <b>لوحة التحكم</b>\n\n"
        f"🪙 <b>الزوج:</b> <code>{config.symbol}</code>\n"
        f"⏱️ <b>الفريمات:</b> <code>{tf_status}</code>\n"
        f"💰 <b>الرصيد:</b> <code>${config.custom_balance}</code>\n"
        f"🎯 <b>اللوت:</b> <code>{lot_mode}</code>\n"
        f"📐 <b>حساسية ZigZag:</b> <code>{config.zigzag_threshold}%</code>\n\n"
        f"🛡️ <b>الفلاتر الأمنية:</b>\n"
        f"• أدنى مسافة دخول: <code>{MIN_ENTRY_DIST_PCT}%</code>\n"
        f"• أدنى مسافة ستوب: <code>{MIN_SL_DIST_PCT}%</code>\n"
        f"• أدنى R:R: <code>1:{MIN_RR:.0f}</code>\n"
        f"• أقصى لوت: <code>{MAX_LOT}</code>\n\n"
        f"💡 <code>/scan</code> للتحليل • <code>/balance 1500</code> للرصيد"
    )

def build_settings_keyboard():
    tf_btns = []
    for tf, active in config.enabled_timeframes.items():
        label = f"✅ {tf}" if active else f"❌ {tf}"
        tf_btns.append(InlineKeyboardButton(label, callback_data=f"tf_{tf}"))

    return InlineKeyboardMarkup([
        tf_btns[:4], tf_btns[4:],
        [
            InlineKeyboardButton(f"🪙 {config.symbol}", callback_data="toggle_symbol"),
            InlineKeyboardButton("🎯 وضع اللوت", callback_data="toggle_lot_mode"),
        ],
        [
            InlineKeyboardButton("✏️ الرصيد", callback_data="prompt_balance"),
            InlineKeyboardButton(f"⚠️ {config.risk_percentage}%", callback_data="change_risk"),
        ],
        [
            InlineKeyboardButton(f"📐 ZigZag: {config.zigzag_threshold}%", callback_data="change_zigzag"),
        ],
    ])

# ================= Command Handlers =================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>مرحباً بك في بوت ZigZag Semi-Auto</b>\n\n"
        "يحدد البوت القمم والقيعان الثابتة عبر ZigZag ثم يعطيك صفقتين معلقتين "
        "(Buy Limit + Sell Limit) مع هدف وستوب مستقل لكل منهما.\n\n"

        "📌 <b>الأوامر:</b>\n"
        "/settings — لوحة التحكم\n"
        "/scan — تحليل وتوليد الصفقات\n"
        "/balance &lt;value&gt; — تعديل الرصيد\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "📐 <b>شرح حساسية ZigZag:</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "الحساسية تحدد <b>الحد الأدنى لنسبة الانعكاس</b> ليُعتبر السعر "
        "قمة أو قاعاً حقيقياً. كلما زادت النسبة، قلّت المستويات وازدادت قوتها.\n\n"

        "🟢 <b>0.15% — حساس جداً</b>\n"
        "• يلتقط تحركات صغيرة\n"
        "• مناسب للفريمات الصغيرة (M15, M30)\n"
        "• ⚠️ مستويات كثيرة، بعضها ضعيف\n\n"

        "🟡 <b>0.30% — متوازن (الافتراضي)</b>\n"
        "• توازن بين الكمية والجودة\n"
        "• مناسب لـ H1 و H4\n\n"

        "🟠 <b>0.50% — متحفظ</b>\n"
        "• مستويات قوية فقط\n"
        "• مناسب لـ H4 و D1\n\n"

        "🔴 <b>1.00% — صارم جداً</b>\n"
        "• القمم والقيعان الكبرى فقط (D1)\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "🛡️ <b>الفلاتر الأمنية المُطبَّقة:</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>أدنى مسافة دخول:</b> <code>{MIN_ENTRY_DIST_PCT}%</code> من السعر\n"
        f"  ↳ يمنع الأوامر الملاصقة للسعر\n"
        f"• <b>أدنى مسافة ستوب:</b> <code>{MIN_SL_DIST_PCT}%</code> من الدخول\n"
        f"  ↳ يمنع الستوبات الضيقة (ضجيج)\n"
        f"• <b>أدنى R:R:</b> <code>1:{MIN_RR:.0f}</code>\n"
        f"  ↳ TP مستقل لكل صفقة (لا تعارض)\n"
        f"• <b>أقصى لوت:</b> <code>{MAX_LOT}</code>\n"
        f"  ↳ يمنع اللوتات الضخمة\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <b>آلية العمل:</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ اضغط <code>/scan</code> لاستلام الإشارة\n"
        "2️⃣ ضع كلا الأمرين المعلقين\n"
        "3️⃣ عند تفعيل أحدهما، ألغِ الآخر <b>فوراً</b>\n"
        "4️⃣ تابع الصفقة حتى TP أو SL\n\n"

        "🎯 ابدأ الآن بـ <code>/settings</code>.",
        parse_mode="HTML"
    )

async def handle_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        build_settings_text(),
        reply_markup=build_settings_keyboard(),
        parse_mode="HTML"
    )

async def handle_balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if context.args:
            val = float(context.args[0])
            if val > 0:
                config.custom_balance = val
                await update.message.reply_text(
                    f"✅ تم تحديث الرصيد إلى <code>${val}</code>",
                    parse_mode="HTML"
                )
                return
        await update.message.reply_text(
            "⚠️ صيغة غير صحيحة. مثال:\n<code>/balance 1500</code>",
            parse_mode="HTML"
        )
    except ValueError:
        await update.message.reply_text(
            "⚠️ يرجى كتابة رقم صحيح. مثال:\n<code>/balance 750</code>",
            parse_mode="HTML"
        )

async def handle_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 <b>جاري التحليل...</b>", parse_mode="HTML")

    res, status = await run_pivot_scan()
    if not res:
        await msg.edit_text(status, parse_mode="HTML")
        return

    p = res["precision"]
    spot = res["spot"]
    buy = res["buy"]
    sell = res["sell"]

    buy_dist = abs(buy["entry"] - spot) / spot * 100
    sell_dist = abs(sell["entry"] - spot) / spot * 100

    buy_sl_pct = (buy["entry"] - buy["sl"]) / buy["entry"] * 100
    sell_sl_pct = (sell["sl"] - sell["entry"]) / sell["entry"] * 100

    buy_tp_pct = (buy["tp"] - buy["entry"]) / buy["entry"] * 100
    sell_tp_pct = (sell["entry"] - sell["tp"]) / sell["entry"] * 100

    tfs = ", ".join(res["tfs"])

    text = (
        f"🔍 <b>نتيجة تحليل ZigZag (MTF)</b>\n\n"
        f"🪙 <b>الزوج:</b> <code>{res['symbol']}</code>\n"
        f"💵 <b>السعر الحالي:</b> <code>{spot:.{p}f}</code>\n"
        f"📊 <b>الفريمات:</b> <code>{tfs}</code>\n"
        f"📐 <b>الحساسية:</b> <code>{config.zigzag_threshold}%</code> | "
        f"<b>R:R:</b> <code>1:{MIN_RR:.0f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>أمر شراء معلق (Buy Limit)</b>\n"
        f"• الدخول: <code>{buy['entry']:.{p}f}</code>\n"
        f"• الهدف TP: <code>{buy['tp']:.{p}f}</code> (<code>+{buy_tp_pct:.2f}%</code>)\n"
        f"• الستوب SL: <code>{buy['sl']:.{p}f}</code> (<code>-{buy_sl_pct:.2f}%</code>)\n"
        f"• اللوت: <code>{buy['lot']}</code>\n"
        f"• المخاطرة: <code>${buy['risk']:.2f}</code>\n"
        f"• بعد السعر: <code>{buy_dist:.2f}%</code>\n"
        f"• المصدر: <code>{buy['source_tfs']}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔴 <b>أمر بيع معلق (Sell Limit)</b>\n"
        f"• الدخول: <code>{sell['entry']:.{p}f}</code>\n"
        f"• الهدف TP: <code>{sell['tp']:.{p}f}</code> (<code>-{sell_tp_pct:.2f}%</code>)\n"
        f"• الستوب SL: <code>{sell['sl']:.{p}f}</code> (<code>+{sell_sl_pct:.2f}%</code>)\n"
        f"• اللوت: <code>{sell['lot']}</code>\n"
        f"• المخاطرة: <code>${sell['risk']:.2f}</code>\n"
        f"• بعد السعر: <code>{sell_dist:.2f}%</code>\n"
        f"• المصدر: <code>{sell['source_tfs']}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>TP مستقل لكل صفقة (لا تعارض)</b>\n"
        f"⚠️ عند تفعيل إحداهما، ألغِ الأخرى <b>فوراً</b>."
    )

    await msg.edit_text(text, parse_mode="HTML")

# ================= Text Handler =================
async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    text_lower = text.lower()

    if config.awaiting_balance_input:
        try:
            val = float(text)
            if val > 0:
                config.custom_balance = val
                config.awaiting_balance_input = False
                await update.message.reply_text(
                    f"✅ تم تعيين الرصيد إلى <code>${val}</code>",
                    parse_mode="HTML"
                )
                return
            else:
                config.awaiting_balance_input = False
                await update.message.reply_text("⚠️ القيمة يجب أن تكون أكبر من صفر.")
                return
        except ValueError:
            config.awaiting_balance_input = False
            await update.message.reply_text("⚠️ قيمة غير صالحة. تم إلغاء العملية.")
            return

    if text_lower in ("settings", "الاعدادات", "إعدادات", "اعدادات"):
        await handle_settings_command(update, context)
    elif text_lower in ("scan", "مسح", "فحص", "تحليل"):
        await handle_scan_command(update, context)
    elif text_lower in ("start", "بدء", "بداية"):
        await start_command(update, context)
    else:
        await update.message.reply_text(
            "🤔 لم أفهم الأمر.\n"
            "جرّب: /start أو /settings أو /scan",
            parse_mode="HTML"
        )

# ================= Callback Handler =================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("tf_"):
        tf = data.split("_", 1)[1]
        enabled_count = sum(config.enabled_timeframes.values())
        if config.enabled_timeframes[tf] and enabled_count <= 1:
            await query.answer("⚠️ يجب تفعيل فريم واحد على الأقل!", show_alert=True)
            return
        config.enabled_timeframes[tf] = not config.enabled_timeframes[tf]

    elif data == "toggle_symbol":
        idx = (AVAILABLE_SYMBOLS.index(config.symbol) + 1) % len(AVAILABLE_SYMBOLS)
        config.symbol = AVAILABLE_SYMBOLS[idx]

    elif data == "toggle_lot_mode":
        config.use_dynamic_lot = not config.use_dynamic_lot

    elif data == "change_risk":
        if config.risk_percentage in RISK_OPTIONS:
            idx = (RISK_OPTIONS.index(config.risk_percentage) + 1) % len(RISK_OPTIONS)
        else:
            idx = 0
        config.risk_percentage = RISK_OPTIONS[idx]

    elif data == "change_zigzag":
        idx = 1
        for i, t in enumerate(ZIGZAG_OPTIONS):
            if abs(t - config.zigzag_threshold) < 0.01:
                idx = (i + 1) % len(ZIGZAG_OPTIONS)
                break
        config.zigzag_threshold = ZIGZAG_OPTIONS[idx]

    elif data == "prompt_balance":
        config.awaiting_balance_input = True
        await query.answer()
        await query.message.reply_text(
            "✏️ اكتب قيمة الرصيد الجديدة:\n(مثال: <code>1500</code>)",
            parse_mode="HTML"
        )
        return

    await query.answer()
    try:
        await query.edit_message_text(
            build_settings_text(),
            reply_markup=build_settings_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Edit error: {e}")

# ================= Main =================
def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("❌ TELEGRAM_TOKEN غير موجود في المتغيرات البيئية.")
        return

    if not get_all_api_keys():
        print("⚠️ تحذير: لم يتم العثور على أي مفاتيح TWELVE_DATA_API_KEY.")

    Thread(target=run_web_server, daemon=True).start()

    bot = Application.builder().token(token).build()

    bot.add_handler(CommandHandler("start", start_command))
    bot.add_handler(CommandHandler("settings", handle_settings_command))
    bot.add_handler(CommandHandler("balance", handle_balance_command))
    bot.add_handler(CommandHandler("scan", handle_scan_command))
    bot.add_handler(CallbackQueryHandler(button_callback))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    print("🤖 Bot is running...")
    bot.run_polling()

if __name__ == "__main__":
    main()
