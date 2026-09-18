import os
import asyncio
import requests
from datetime import datetime, timedelta, timezone
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
    return "ZigZag Multi-Mode Bot is Active!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= Global Constants =================
# إعدادات وضع السوينغ (Default)
SWING_MIN_ENTRY_DIST_PCT = 0.30
SWING_MIN_SL_DIST_PCT    = 0.30
SWING_MIN_RR             = 2.0

# إعدادات وضع السكالبينج (سريع خاطف)
SCALP_MIN_ENTRY_DIST_PCT = 0.08
SCALP_MIN_SL_DIST_PCT    = 0.08
SCALP_MIN_RR             = 1.2

MAX_LOT           = 1.00
CONFLUENCE_STRONG = 3     # عدد الفريمات المتوافقة لاعتبار المستوى "قوياً"
NEWS_WINDOW_BEFORE_MIN = 30   # دقائق قبل الخبر
NEWS_WINDOW_AFTER_MIN  = 30   # دقائق بعد الخبر
NEWS_LOOKAHEAD_MIN     = 90   # نطاق البحث للأمام

TF_PRIORITY = {"D1": 5, "H4": 4, "H1": 3, "M30": 2, "M15": 2, "M5": 1, "M1": 1}
SCALP_TFS   = ["M1", "M5", "M15"]

HIGH_IMPACT_KEYWORDS = [
    "non-farm", "nonfarm", "nfp", "cpi", "interest rate", "fomc",
    "federal funds", "unemployment", "gdp", "retail sales",
    "pce", "ppi", "adp", "ism", "fed chair", "powell",
    "jobless", "payroll"
]

# ================= Bot Config =================
class BotConfig:
    def __init__(self):
        self.symbol = "XAU/USD"
        self.mode = "swing"   # "swing" أو "scalp"
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
RISK_OPTIONS      = [0.5, 1.0, 1.5, 2.0]
ZIGZAG_OPTIONS    = [0.15, 0.30, 0.50, 1.00]

# ================= Helpers =================
def get_symbol_precision(symbol):
    s = symbol.upper()
    if "JPY" in s: return 3
    if any(x in s for x in ("XAU", "BTC", "ETH")): return 2
    return 5

def get_cost_per_point(symbol):
    s = symbol.upper()
    if "XAU" in s: return 100.0
    if any(x in s for x in ("BTC", "ETH")): return 1.0
    return 100000.0

def get_all_api_keys():
    keys = []
    for k, v in os.environ.items():
        if k.startswith("TWELVE_DATA_API_KEY") and v.strip():
            if v.strip() not in keys: keys.append(v.strip())
    return keys

def get_active_params():
    """إرجاع إعدادات الوضع النشط"""
    if config.mode == "scalp":
        return {
            "min_entry": SCALP_MIN_ENTRY_DIST_PCT,
            "min_sl":    SCALP_MIN_SL_DIST_PCT,
            "rr":        SCALP_MIN_RR,
            "label":     "⚡ سكالبينج",
        }
    return {
        "min_entry": SWING_MIN_ENTRY_DIST_PCT,
        "min_sl":    SWING_MIN_SL_DIST_PCT,
        "rr":        SWING_MIN_RR,
        "label":     "🎯 سوينغ",
    }

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
    if not api_keys: return None
    for _ in range(len(api_keys)):
        if current_key_idx >= len(api_keys): current_key_idx = 0
        active_key = api_keys[current_key_idx]
        url = f"https://api.twelvedata.com/{endpoint_name}"
        params = {"apikey": active_key}
        if extra_params: params.update(extra_params)
        res = await asyncio.to_thread(fetch_url, url, params, 8)
        if res and isinstance(res, dict) and res.get("status_code") == 429:
            current_key_idx = (current_key_idx + 1) % len(api_keys)
            continue
        return res
    return {"status_code": 429}

# ================= News Filter (Finnhub) =================
async def get_upcoming_news():
    """
    جلب الأخبار الاقتصادية عالية التأثير للدولار خلال الساعات القادمة.
    يُرجع None إذا لم يتم تكوين FINNHUB_API_KEY.
    """
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return None

    now_utc = datetime.now(timezone.utc)
    start_d = now_utc.strftime("%Y-%m-%d")
    end_d   = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")

    url = "https://finnhub.io/api/v1/calendar/economic"
    params = {"from": start_d, "to": end_d, "token": api_key}

    res = await asyncio.to_thread(fetch_url, url, params, 6)
    if not res or not isinstance(res, dict):
        return []

    calendar = res.get("economicCalendar") or []
    upcoming = []

    for ev in calendar:
        country = (ev.get("country") or "").upper()
        if country not in ("US", "USD"):
            continue

        impact = (ev.get("impact") or "").lower()
        event_name = (ev.get("event") or "").lower()
        is_high = (impact == "high") or any(k in event_name for k in HIGH_IMPACT_KEYWORDS)
        if not is_high:
            continue

        raw_time = ev.get("time")
        if not raw_time:
            continue

        ev_dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                ev_dt = datetime.strptime(raw_time, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if ev_dt is None:
            continue

        delta_min = (ev_dt - now_utc).total_seconds() / 60.0
        if -NEWS_WINDOW_AFTER_MIN <= delta_min <= NEWS_LOOKAHEAD_MIN:
            upcoming.append({
                "event": ev.get("event", "Unknown"),
                "minutes": delta_min,
                "time_str": ev_dt.strftime("%H:%M UTC"),
            })

    upcoming.sort(key=lambda x: x["minutes"])
    return upcoming

def format_news_block(news_list):
    """تنسيق قسم الأخبار للعرض في الرسالة"""
    if news_list is None:
        return "📰 <b>فلتر الأخبار:</b> <i>غير مفعّل (FINNHUB_API_KEY غير مضبوط)</i>\n"

    if not news_list:
        return f"📰 <b>فلتر الأخبار:</b> ✅ <i>لا أخبار قوية خلال {NEWS_LOOKAHEAD_MIN} دقيقة القادمة</i>\n"

    lines = ["📰 <b>فلتر الأخبار:</b> ⚠️ <b>يوجد أخبار قوية قريبة!</b>"]
    for n in news_list[:3]:
        mins = n["minutes"]
        if mins >= 0:
            lines.append(f"   • <code>{n['event']}</code> بعد <b>{int(mins)}</b> دقيقة ({n['time_str']})")
        else:
            lines.append(f"   • <code>{n['event']}</code> مضى عليها <b>{int(-mins)}</b> دقيقة ({n['time_str']})")
    return "\n".join(lines) + "\n"

def is_news_blocking(news_list):
    """هل يوجد خبر يمنع الدخول الآن؟"""
    if not news_list:
        return False
    for n in news_list:
        if -NEWS_WINDOW_AFTER_MIN <= n["minutes"] <= NEWS_WINDOW_BEFORE_MIN:
            return True
    return False

# ================= ZigZag Pivot Detection =================
def zigzag_pivots(highs, lows, precision, threshold_pct=0.3, skip_last=2):
    n = len(highs) - skip_last
    if n < 3: return [], []
    pivot_highs, pivot_lows = [], []
    direction = None
    running_high, running_low = highs[0], lows[0]

    for i in range(1, n):
        h, l = highs[i], lows[i]

        if direction is None:
            if h > running_high: running_high = h
            if l < running_low: running_low = l
            up_move = (running_high - lows[0]) / lows[0] * 100 if lows[0] else 0
            down_move = (highs[0] - running_low) / highs[0] * 100 if highs[0] else 0
            if up_move >= threshold_pct and up_move >= down_move:
                direction = 'up'
            elif down_move >= threshold_pct:
                direction = 'down'
            continue

        if direction == 'up':
            if h > running_high: running_high = h
            if running_high > 0 and (running_high - l) / running_high * 100 >= threshold_pct:
                pivot_highs.append(round(running_high, precision))
                direction = 'down'
                running_low = l
        else:
            if l < running_low: running_low = l
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

# ================= Scan Engine =================
async def run_pivot_scan():
    """
    محرك التحليل الرئيسي:
    1) فحص الأخبار
    2) جلب البيانات من الفريمات المفعّلة
    3) استخراج Pivots + دمجها + كشف التوافق
    4) توليد إشارة (صفقة قوية واحدة، أو صفقتين)
    """
    # ---- 0) الفحص الأولي ----
    params = get_active_params()
    active_tfs = [tf for tf, on in config.enabled_timeframes.items() if on]
    if not active_tfs:
        return None, "⚠️ يجب تفعيل فريم واحد على الأقل."

    # ---- 1) فحص الأخبار ----
    news_list = await get_upcoming_news()
    news_block = format_news_block(news_list)

    if is_news_blocking(news_list):
        blocking = [n for n in news_list if -NEWS_WINDOW_AFTER_MIN <= n["minutes"] <= NEWS_WINDOW_BEFORE_MIN]
        ev = blocking[0]
        mins = ev["minutes"]
        when = f"بعد <b>{int(mins)}</b> دقيقة" if mins >= 0 else f"مضى عليها <b>{int(-mins)}</b> دقيقة"
        return None, (
            f"🚫 <b>تم إيقاف الفحص — نافذة خبر اقتصادي</b>\n\n"
            f"📰 <b>الحدث:</b> <code>{ev['event']}</code>\n"
            f"⏰ <b>التوقيت:</b> {ev['time_str']} ({when})\n\n"
            f"⚠️ الدخول في صفقات خلال نافذة الأخبار (قبل {NEWS_WINDOW_BEFORE_MIN} د / بعد {NEWS_WINDOW_AFTER_MIN} د) "
            f"خطير جداً بسبب التقلبات الحادة.\n\n"
            f"⏳ <b>انتظر انتهاء الخبر</b> ثم أعد <code>/scan</code>."
        )

    # ---- 2) جلب البيانات من كل الفريمات ----
    all_highs = []   # [(price, priority, tf_code)]
    all_lows  = []
    spot_price = None
    precision = get_symbol_precision(config.symbol)

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
            lows  = [float(x["low"])  for x in vals]
            closes = [float(x["close"]) for x in vals]

            if spot_price is None:
                spot_price = round(closes[-1], precision)

            p_highs, p_lows = zigzag_pivots(
                highs, lows, precision,
                threshold_pct=config.zigzag_threshold,
                skip_last=2
            )
            pri = TF_PRIORITY.get(tf_code, 1)
            for p in p_highs: all_highs.append((p, pri, tf_code))
            for p in p_lows:  all_lows.append((p, pri, tf_code))

    if spot_price is None:
        return None, "⚠️ تعذر الاتصال بمصدر البيانات، حاول لاحقاً."

    # ---- 3) دمج المستويات المتقاربة + تتبع الفريمات ----
    def merge_levels(levels):
        if not levels: return []
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

    lows_merged  = merge_levels(all_lows)
    highs_merged = merge_levels(all_highs)

    # ---- 4) الفلترة حسب المسافة الدنيا من السعر ----
    min_entry = params["min_entry"]
    supports = [
        (p, pri, tfs) for p, pri, tfs in lows_merged
        if p < spot_price and (spot_price - p) / spot_price * 100 >= min_entry
    ]
    resistances = [
        (p, pri, tfs) for p, pri, tfs in highs_merged
        if p > spot_price and (p - spot_price) / spot_price * 100 >= min_entry
    ]

    supports.sort(key=lambda x: spot_price - x[0])
    resistances.sort(key=lambda x: x[0] - spot_price)

    if len(supports) < 2 or len(resistances) < 2:
        return None, (
            f"⚠️ <b>مستويات غير كافية</b>\n\n"
            f"💵 السعر: <code>{spot_price:.{precision}f}</code>\n"
            f"🛡️ الوضع: <b>{params['label']}</b> | أدنى مسافة: <code>{min_entry}%</code>\n"
            f"🔎 دعوم: <code>{len(supports)}</code> | مقاومات: <code>{len(resistances)}</code>\n\n"
            f"💡 <b>الحلول:</b>\n"
            f"• فعّل فريمات إضافية\n"
            f"• قلل حساسية ZigZag\n"
            f"• بدّل الوضع إلى سكالبينج"
        )

    # ---- 5) اختيار الدخول + الستوب ----
    def pick_entry_sl(levels, is_support):
        entry = levels[0]
        sl = None
        for lv in levels[1:]:
            if is_support:
                dist_pct = (entry[0] - lv[0]) / entry[0] * 100
            else:
                dist_pct = (lv[0] - entry[0]) / entry[0] * 100
            if dist_pct >= params["min_sl"]:
                sl = lv
                break
        if sl is None:
            if is_support:
                sl_price = entry[0] * (1 - params["min_sl"] / 100)
            else:
                sl_price = entry[0] * (1 + params["min_sl"] / 100)
            sl = (sl_price, 0, {"auto"})
        return entry, sl

    buy_entry, buy_sl   = pick_entry_sl(supports, is_support=True)
    sell_entry, sell_sl = pick_entry_sl(resistances, is_support=False)

    buy_entry_p  = round(buy_entry[0], precision)
    buy_sl_p     = round(buy_sl[0], precision)
    sell_entry_p = round(sell_entry[0], precision)
    sell_sl_p    = round(sell_sl[0], precision)

    # ---- 6) TP مستقل لكل صفقة ----
    rr = params["rr"]
    buy_risk  = buy_entry_p - buy_sl_p
    sell_risk = sell_sl_p - sell_entry_p
    buy_tp_p  = round(buy_entry_p + buy_risk * rr, precision)
    sell_tp_p = round(sell_entry_p - sell_risk * rr, precision)

    # ---- 7) اللوت ----
    buy_lot  = calculate_recommended_lot(buy_entry_p, buy_sl_p, config.symbol)
    sell_lot = calculate_recommended_lot(sell_entry_p, sell_sl_p, config.symbol)

    # ---- 8) حساب قوة كل مستوى (Confluence) ----
    buy_strength  = len(buy_entry[2])
    sell_strength = len(sell_entry[2])
    buy_is_strong  = buy_strength  >= CONFLUENCE_STRONG
    sell_is_strong = sell_strength >= CONFLUENCE_STRONG

    return {
        "spot": spot_price,
        "symbol": config.symbol,
        "precision": precision,
        "tfs": active_tfs,
        "mode": config.mode,
        "params": params,
        "news_block": news_block,
        "news_list": news_list,
        "buy": {
            "entry": buy_entry_p,
            "tp": buy_tp_p,
            "sl": buy_sl_p,
            "lot": buy_lot,
            "risk": get_actual_risk(buy_lot, buy_entry_p, buy_sl_p, config.symbol),
            "source_tfs": sorted(buy_entry[2]),
            "strength": buy_strength,
            "is_strong": buy_is_strong,
        },
        "sell": {
            "entry": sell_entry_p,
            "tp": sell_tp_p,
            "sl": sell_sl_p,
            "lot": sell_lot,
            "risk": get_actual_risk(sell_lot, sell_entry_p, sell_sl_p, config.symbol),
            "source_tfs": sorted(sell_entry[2]),
            "strength": sell_strength,
            "is_strong": sell_is_strong,
        },
    }, "OK"

# ================= Signal Formatter =================
def format_signal_card(side, data, precision, params, title_prefix=""):
    entry = data["entry"]; tp = data["tp"]; sl = data["sl"]
    dist = abs(entry - data["entry"])  # placeholder
    if side == "buy":
        sl_pct = (entry - sl) / entry * 100
        tp_pct = (tp - entry) / entry * 100
        emoji = "🟢"
        title = f"{emoji} <b>{title_prefix}أمر شراء معلق (Buy Limit)</b>"
    else:
        sl_pct = (sl - entry) / entry * 100
        tp_pct = (entry - tp) / entry * 100
        emoji = "🔴"
        title = f"{emoji} <b>{title_prefix}أمر بيع معلق (Sell Limit)</b>"

    stars = "⭐" * min(data["strength"], 5)
    strength_tag = ""
    if data["is_strong"]:
        strength_tag = f"\n🏆 <b>مستوى مؤسسي قوي</b> (توافق {data['strength']} فريمات) {stars}"

    tfs_str = ", ".join(data["source_tfs"]) if data["source_tfs"] else "—"

    return (
        f"{title}{strength_tag}\n"
        f"• الدخول: <code>{entry:.{precision}f}</code>\n"
        f"• الهدف TP: <code>{tp:.{precision}f}</code> (<code>+{tp_pct:.2f}%</code>)\n"
        f"• الستوب SL: <code>{sl:.{precision}f}</code> (<code>-{sl_pct:.2f}%</code>)\n"
        f"• اللوت: <code>{data['lot']}</code>\n"
        f"• المخاطرة: <code>${data['risk']:.2f}</code>\n"
        f"• المصدر: <code>{tfs_str}</code>"
    )

# ================= UI Builders =================
def build_settings_text():
    tf_status = ", ".join([tf for tf, on in config.enabled_timeframes.items() if on]) or "لا يوجد"
    lot_mode = (
        f"تلقائي ({config.risk_percentage}%)" if config.use_dynamic_lot
        else f"ثابت ({config.fixed_lot})"
    )
    mode_label = "⚡ سكالبينج (خاطف)" if config.mode == "scalp" else "🎯 سوينغ (Default)"
    p = get_active_params()

    return (
        f"⚙️ <b>لوحة التحكم</b>\n\n"
        f"🎮 <b>الوضع:</b> {mode_label}\n"
        f"🪙 <b>الزوج:</b> <code>{config.symbol}</code>\n"
        f"⏱️ <b>الفريمات:</b> <code>{tf_status}</code>\n"
        f"💰 <b>الرصيد:</b> <code>${config.custom_balance}</code>\n"
        f"🎯 <b>اللوت:</b> <code>{lot_mode}</code>\n"
        f"📐 <b>ZigZag:</b> <code>{config.zigzag_threshold}%</code>\n\n"
        f"🛡️ <b>فلاتر الوضع النشط:</b>\n"
        f"• أدنى مسافة دخول: <code>{p['min_entry']}%</code>\n"
        f"• أدنى مسافة ستوب: <code>{p['min_sl']}%</code>\n"
        f"• R:R: <code>1:{p['rr']:.1f}</code>\n"
        f"• أقصى لوت: <code>{MAX_LOT}</code>\n\n"
        f"📰 <b>فلتر الأخبار:</b> "
        f"{'✅ مفعّل' if os.environ.get('FINNHUB_API_KEY') else '❌ غير مضبوط'}\n\n"
        f"💡 <code>/scan</code> للتحليل"
    )

def build_settings_keyboard():
    tf_btns = []
    for tf, active in config.enabled_timeframes.items():
        label = f"✅ {tf}" if active else f"❌ {tf}"
        tf_btns.append(InlineKeyboardButton(label, callback_data=f"tf_{tf}"))

    mode_label = "⚡ سكالبينج" if config.mode == "swing" else "🎯 سوينغ"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎮 تبديل إلى: {mode_label}", callback_data="toggle_mode")],
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
        "👋 <b>مرحباً بك في بوت ZigZag Multi-Mode</b>\n\n"
        "بوت نصف آلي يكشف القمم والقيعان الثابتة عبر ZigZag، "
        "ويولّد إشارات صفقات معلقة مع فلتر أخبار اقتصادية.\n\n"

        "📌 <b>الأوامر:</b>\n"
        "/settings — لوحة التحكم\n"
        "/scan — تحليل وتوليد الصفقات\n"
        "/balance &lt;value&gt; — تعديل الرصيد\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "🎮 <b>وضعا التشغيل:</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"

        "🎯 <b>وضع السوينغ (Swing):</b>\n"
        "• فريمات كبيرة: H1, H4, D1\n"
        "• أهداف بعيدة (R:R = 1:2)\n"
        "• صفقات تُفتح مرة أو مرتين يومياً\n"
        "• مناسبة للاتجاه الواضح\n\n"

        "⚡ <b>وضع السكالبينج (Scalp):</b>\n"
        "• فريمات صغيرة: M1, M5, M15\n"
        "• أهداف قريبة سريعة (R:R = 1:1.2)\n"
        "• صفقات خاطفة (دقائق)\n"
        "• مناسبة للسوق العرضي والحركات السريعة\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "📰 <b>فلتر الأخبار (Finnhub):</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "عند كل <code>/scan</code> يفحص البوت الأخبار الأمريكية عالية التأثير:\n"
        "• ⛔ <b>يمنع الإشارة</b> إذا كان الوقت ضمن نافذة خبر "
        f"(قبل {NEWS_WINDOW_BEFORE_MIN} دقيقة / بعد {NEWS_WINDOW_AFTER_MIN} دقيقة)\n"
        "• ⚠️ <b>يحذّر</b> إذا كان هناك خبر خلال 90 دقيقة\n"
        "• ✅ يعرض 'آمن' إذا لم توجد أخبار قريبة\n\n"
        "🔑 يتطلب <code>FINNHUB_API_KEY</code> (مجاني من finnhub.io)\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "📐 <b>شرح حساسية ZigZag:</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🟢 <code>0.15%</code> — حساس جداً (M1/M5 سكالبينج)\n"
        "🟡 <code>0.30%</code> — متوازن (H1/H4) ← <b>الافتراضي</b>\n"
        "🟠 <code>0.50%</code> — متحفظ (H4/D1)\n"
        "🔴 <code>1.00%</code> — صارم جداً (D1 فقط)\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "⭐ <b>منطق الصفقة القوية (Confluence):</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "عندما يتوافق مستوى على <b>3 فريمات أو أكثر</b> (مثل D1+H4+H1)، "
        "يعتبره البوت <b>مستوى مؤسسياً</b> ويعرضه كصفقة رئيسية أولى.\n\n"

        "⚠️ <b>آلية العمل:</b>\n"
        "1️⃣ <code>/settings</code> لضبط الوضع والفريمات\n"
        "2️⃣ <code>/scan</code> لاستلام الإشارة\n"
        "3️⃣ ضع الأمر(ين) المعلق(ين)\n"
        "4️⃣ عند التفعيل → ألغِ الآخر <b>فوراً</b>\n"
        "5️⃣ تابع حتى TP أو SL\n\n"

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
    msg = await update.message.reply_text("🔍 <b>جاري التحليل وفحص الأخبار...</b>", parse_mode="HTML")

    res, status = await run_pivot_scan()
    if not res:
        await msg.edit_text(status, parse_mode="HTML")
        return

    p = res["precision"]
    spot = res["spot"]
    buy = res["buy"]
    sell = res["sell"]
    params = res["params"]
    precision = p

    tfs = ", ".join(res["tfs"])
    news_block = res["news_block"]

    # ---- تحديد الإستراتيجية ----
    # إذا كان أحد المستويين قوياً (توافق 3+) → اعرضه كرئيسي فقط
    # وإذا كان كلاهما قوياً → اعرضهما
    # وإذا لم يوجد قوي → اعرض كلاهما (الوضع الافتراضي)

    strong_sides = []
    if buy["is_strong"]:  strong_sides.append("buy")
    if sell["is_strong"]: strong_sides.append("sell")

    header = (
        f"🔍 <b>نتيجة تحليل ZigZag</b>\n\n"
        f"🎮 <b>الوضع:</b> {params['label']} | <b>R:R:</b> <code>1:{params['rr']:.1f}</code>\n"
        f"🪙 <b>الزوج:</b> <code>{res['symbol']}</code>\n"
        f"💵 <b>السعر الحالي:</b> <code>{spot:.{precision}f}</code>\n"
        f"📊 <b>الفريمات:</b> <code>{tfs}</code>\n"
        f"📐 <b>الحساسية:</b> <code>{config.zigzag_threshold}%</code>\n"
        f"{news_block}"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    body = ""

    if len(strong_sides) == 1:
        # ---- صفقة واحدة قوية ----
        side = strong_sides[0]
        data = buy if side == "buy" else sell
        body += (
            f"🏆 <b>إشارة رئيسية واحدة — مستوى مؤسسي قوي</b>\n"
            f"({data['strength']} فريمات متوافقة: <code>{', '.join(data['source_tfs'])}</code>)\n\n"
            f"{format_signal_card(side, data, precision, params, title_prefix='[رئيسية] ')}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⚖️ <b>الإشارة الثانية مستبعدة</b> لعدم اكتمال شروط القوة.\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>ركّز على هذه الصفقة فقط.</b> راقبها حتى TP أو SL."
        )

    elif len(strong_sides) == 2:
        # ---- كلتاهما قوية ----
        body += (
            f"🏆 <b>مستويان مؤسسيان قويان (كلاهما 3+ فريمات)</b>\n\n"
            f"{format_signal_card('buy', buy, precision, params)}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{format_signal_card('sell', sell, precision, params)}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ عند تفعيل إحداهما، ألغِ الأخرى <b>فوراً</b>."
        )

    else:
        # ---- الوضع الافتراضي: صفقتان ----
        body += (
            f"{format_signal_card('buy', buy, precision, params)}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{format_signal_card('sell', sell, precision, params)}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"✅ TP مستقل لكل صفقة (لا تعارض)\n"
            f"⚠️ عند تفعيل إحداهما، ألغِ الأخرى <b>فوراً</b>."
        )

    text = header + body
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
            "🤔 لم أفهم الأمر.\nجرّب: /start أو /settings أو /scan",
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

    elif data == "toggle_mode":
        if config.mode == "swing":
            config.mode = "scalp"
            # التبديل التلقائي إلى فريمات السكالبينج
            config.enabled_timeframes = {
                "D1": False, "H4": False, "H1": False,
                "M30": False, "M15": True, "M5": True, "M1": True
            }
            config.zigzag_threshold = 0.15
        else:
            config.mode = "swing"
            config.enabled_timeframes = {
                "D1": True, "H4": True, "H1": True,
                "M30": False, "M15": False, "M5": False, "M1": False
            }
            config.zigzag_threshold = 0.30

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
        print("⚠️ تحذير: لم يتم العثور على مفاتيح TWELVE_DATA_API_KEY.")

    if not os.environ.get("FINNHUB_API_KEY"):
        print("⚠️ تحذير: FINNHUB_API_KEY غير مضبوط — فلتر الأخبار معطّل.")

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
