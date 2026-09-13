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
active_order = None
order_status = None 
send_status_reports = True  
strategy_mode = "flexible"   
news_filter_active = True
scan_target = "gold_only"  # الخيارات: gold_only, gold_forex, stocks_only, all
account_balance_cents = 200000  # رصيد الحساب بالسنت[span_2](start_span)[span_2](end_span)
risk_percentage = 0.5           # نسبة المخاطرة الافتراضية 0.5%[span_3](start_span)[span_3](end_span)

# قوائم أصول التداول
FOREX_PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY"]
STOCKS_LIST = ["TSLA", "NVDA", "AMD", "AAPL"]

async def wait_for_next_check():
    while True:
        now = datetime.utcnow()
        if now.minute % 5 == 0 and now.second < 3:
            break
        await asyncio.sleep(1)

# ================= ================= =================
# 2. فلتر الأخبار وحاسبة اللوت
# ================= ================= =================
def is_high_impact_news_near():
    """فحص الأخبار الاقتصادية عالية التأثير على الدولار (USD)""[span_4](start_span)"[span_4](end_span)
    if not news_filter_active:
        return False, ""
    try:
        url = "https://nfp.ourforecast.com/api/v1/events"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            events = response.json()
            now = datetime.utcnow()
            for event in events:
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
    """حساب حجم اللوت التلقائي بناءً على طبيعة الأصل والرصيد والستوب""[span_5](start_span)"[span_5](end_span)
    try:
        risk_amount_cents = account_balance_cents * (risk_percentage / 100.0)[span_6](start_span)[span_6](end_span)
        pips_at_risk = abs(entry_price - stop_loss_price)[span_7](start_span)[span_7](end_span)
        if pips_at_risk == 0:
            return 0.10
        
        # معيرات اللوت حسب نوع الأصل
        if "/" in symbol and "XAU" not in symbol:  # أزواج العملات (Forex)
            pip_value_per_cent_lot = 1.0
        elif symbol in STOCKS_LIST:               # الأسهم
            pip_value_per_cent_lot = 100.0
        else:                                     # الذهب (XAU/USD)[span_8](start_span)[span_8](end_span)
            pip_value_per_cent_lot = 10.0[span_9](start_span)[span_9](end_span)

        raw_lot = risk_amount_cents / (pips_at_risk * pip_value_per_cent_lot)[span_10](start_span)[span_10](end_span)
        return max(0.01, round(raw_lot, 2))[span_11](start_span)[span_11](end_span)
    except Exception as e:
        print(f"Error calculating lot: {e}")
        return 0.10[span_12](start_span)[span_12](end_span)

# ================= ================= =================
# 3. التحليلات الفنية ومؤشرات السوق
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
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(data, window):
    weights = [2 / (window + 1)]
    ema = [data[0]]
    for price in data[1:]:
        ema.append((price * weights[0]) + (ema[-1] * (1 - weights[0])))
    return ema

def get_live_price(symbol="XAU/USD"):
    api_key = os.environ.get("TWELVE_DATA_API_KEY")
    if not api_key:
        return None
    try:
        url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey={api_key}"
        res = requests.get(url, timeout=5).json()
        if "price" in res:
            return float(res["price"])
    except Exception as e:
        print(f"Live Price Error for {symbol}: {e}")
    return None

def analyze_market_symbol(symbol):
    """دالة تحليل معممة تدعم الذهب والعملات والأسهم"""
    global active_order, order_status, strategy_mode
    
    now_utc = datetime.utcnow()
    # عطلة نهاية الأسبوع
    if now_utc.weekday() in [5, 6]:
        return None, "MARKET_CLOSED", 0, 0, 0, 0

    has_news, news_title = is_high_impact_news_near()[span_13](start_span)[span_13](end_span)
    if has_news:
        return None, "NEWS_PAUSE", 0, 0, 0, 0[span_14](start_span)[span_14](end_span)

    api_key = os.environ.get("TWELVE_DATA_API_KEY")[span_15](start_span)[span_15](end_span)
    if not api_key:
        return None, "NO_KEY", 0, 0, 0, 0[span_16](start_span)[span_16](end_span)

    try:
        # جلب البيانات الفنية للأصل المطلوب
        url_spot = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval=15min&outputsize=40&apikey={api_key}"
        res_spot = requests.get(url_spot, timeout=8).json()
        
        if "values" not in res_spot:
            return None, "API_ERROR", 0, 0, 0, 0

        spot_values = res_spot["values"]
        spot_values.reverse()
        spot_closes = [float(item["close"]) for item in spot_values]
        spot_highs = [float(item["high"]) for item in spot_values]
        spot_lows = [float(item["low"]) for item in spot_values]
        spot_price = round(spot_closes[-1], 4 if "/" in symbol and "XAU" not in symbol else 2)

        # حساب الفارق الآجل (Basis) للذهب فقط
        basis_expansion = 0
        basis_current = 0
        volume_surging = True
        
        if symbol == "XAU/USD":
            url_futures = f"https://api.twelvedata.com/time_series?symbol=MGC&interval=15min&outputsize=40&apikey={api_key}[span_17](start_span)"[span_17](end_span)
            res_futures = requests.get(url_futures, timeout=8).json()[span_18](start_span)[span_18](end_span)
            if "values" in res_futures:
                futures_values = res_futures["values"][span_19](start_span)[span_19](end_span)
                futures_values.reverse()[span_20](start_span)[span_20](end_span)
                futures_closes = [float(item["close"]) for item in futures_values][span_21](start_span)[span_21](end_span)
                futures_volumes = [float(item.get("volume", 0)) for item in futures_values][span_22](start_span)[span_22](end_span)
                futures_price = round(futures_closes[-1], 2)[span_23](start_span)[span_23](end_span)
                basis_current = round(futures_price - spot_price, 2)[span_24](start_span)[span_24](end_span)
                basis_prev = round(futures_closes[-2] - spot_closes[-2], 2)[span_25](start_span)[span_25](end_span)
                basis_expansion = round(basis_current - basis_prev, 2)[span_26](start_span)[span_26](end_span)
                
                volume_current = futures_volumes[-1][span_27](start_span)[span_27](end_span)
                volume_avg = sum(futures_volumes[-5:-1]) / 4 if len(futures_volumes) >= 5 else volume_current[span_28](start_span)[span_28](end_span)
                volume_surging = volume_current > volume_avg[span_29](start_span)[span_29](end_span)

        # المؤشرات الفنية
        ema20 = calculate_ema(spot_closes, 20)[-1][span_30](start_span)[span_30](end_span)
        ema50 = calculate_ema(spot_closes, 50)[-1][span_31](start_span)[span_31](end_span)
        rsi = calculate_rsi(spot_closes, 14)[span_32](start_span)[span_32](end_span)
        tight_swing_high = max(spot_highs[-3:-1])[span_33](start_span)[span_33](end_span)
        tight_swing_low = min(spot_lows[-3:-1])[span_34](start_span)[span_34](end_span)

        if active_order is not None and active_order.get("symbol") == symbol:
            status_event = "STILL_TRIGGERED" if order_status == "TRIGGERED" else "STILL_PENDING[span_35](start_span)"[span_35](end_span)
            return active_order, status_event, spot_price, basis_current, rsi, 0[span_36](start_span)[span_36](end_span)

        rsi_buy_max = 75 if strategy_mode == "flexible" else 68[span_37](start_span)[span_37](end_span)
        rsi_buy_min = 30[span_38](start_span)[span_38](end_span)
        rsi_sell_min = 25 if strategy_mode == "flexible" else 32[span_39](start_span)[span_39](end_span)
        rsi_sell_max = 70[span_40](start_span)[span_40](end_span)
        
        # المسافات والهوامش حسب طبيعة الأصل
        offset = 0.50 if symbol == "XAU/USD" else (0.0005 if "/" in symbol else 0.20)
        max_risk = 10.00 if symbol == "XAU/USD" else (0.0050 if "/" in symbol else 3.00)

        # استراتيجية الشراء
        if ema20 > ema50 and (rsi_buy_min <= rsi < rsi_buy_max) and (basis_expansion >= 0 or volume_surging):
            proposed_entry = round(max(tight_swing_high, spot_price) + offset, 4 if "/" in symbol and "XAU" not in symbol else 2)
            sl_price = round(tight_swing_low - offset, 4 if "/" in symbol and "XAU" not in symbol else 2)
            
            if proposed_entry > spot_price > sl_price:
                risk_distance = proposed_entry - sl_price
                if risk_distance <= max_risk:
                    tp_price = round(proposed_entry + (risk_distance * 1.5), 4 if "/" in symbol and "XAU" not in symbol else 2)
                    rec_lot = calculate_recommended_lot(symbol, proposed_entry, sl_price)
                    active_order = {"symbol": symbol, "type": "Buy Stop", "entry": proposed_entry, "tp": tp_price, "sl": sl_price, "rsi": rsi, "lot": rec_lot}
                    order_status = "PENDING"
                    return active_order, "NEW_ORDER", spot_price, basis_current, rsi, rec_lot

        # استراتيجية البيع
        elif ema20 < ema50 and (rsi_sell_min < rsi <= rsi_sell_max) and (basis_expansion <= 0 or volume_surging):
            proposed_entry = round(min(tight_swing_low, spot_price) - offset, 4 if "/" in symbol and "XAU" not in symbol else 2)
            sl_price = round(tight_swing_high + offset, 4 if "/" in symbol and "XAU" not in symbol else 2)
            if proposed_entry < spot_price < sl_price:
                risk_distance = sl_price - proposed_entry
                if risk_distance <= max_risk:
                    tp_price = round(proposed_entry - (risk_distance * 1.5), 4 if "/" in symbol and "XAU" not in symbol else 2)
                    rec_lot = calculate_recommended_lot(symbol, proposed_entry, sl_price)
                    active_order = {"symbol": symbol, "type": "Sell Stop", "entry": proposed_entry, "tp": tp_price, "sl": sl_price, "rsi": rsi, "lot": rec_lot}
                    order_status = "PENDING"
                    return active_order, "NEW_ORDER", spot_price, basis_current, rsi, rec_lot

        return None, "NO_SIGNAL", spot_price, basis_current, rsi, 0
    except Exception as e:
        print(f"Execution Error [{symbol}]: {e}")
        return None, "ERROR", 0, 0, 0, 0

# ================= ================= =================
# 4. حلقات الفحص والمراقبة السريعة
# ================= ================= =================
async def fast_price_monitor_loop(bot: Bot, chat_id: str):
    global active_order, order_status
    while True:
        try:
            if active_order is not None:
                symbol = active_order.get("symbol", "XAU/USD")
                live_p = get_live_price(symbol)
                if live_p is not None:
                    if order_status == "PENDING":
                        if active_order['type'] == 'Buy Stop' and live_p <= active_order['sl']:
                            active_order, order_status = None, None
                            msg = f"🚫 **تنبيه فوري [{symbol}]: تم إلغاء صفقة الشراء المعلقة!**\nالسعر ضرب مستوى الستوب (`{live_p}`) قبل التفعيل."
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        elif active_order['type'] == 'Sell Stop' and live_p >= active_order['sl']:
                            active_order, order_status = None, None
                            msg = f"🚫 **تنبيه فوري [{symbol}]: تم إلغاء صفقة البيع المعلقة!**\nالسعر ضرب مستوى الستوب (`{live_p}`) قبل التفعيل."
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        elif active_order['type'] == 'Buy Stop' and live_p >= active_order['entry']:
                            order_status = "TRIGGERED"
                            msg = f"⚡️ **تم تفعيل صفقة الشراء [{symbol}] فوراً!**\n📍 **سعر التفعيل الحقيقي:** `{live_p}`"
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        elif active_order['type'] == 'Sell Stop' and live_p <= active_order['entry']:
                            order_status = "TRIGGERED"
                            msg = f"⚡️ **تم تفعيل صفقة البيع [{symbol}] فوراً!**\n📍 **سعر التفعيل الحقيقي:** `{live_p}`"
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    elif order_status == "TRIGGERED":
                        if active_order['type'] == 'Buy Stop':
                            if live_p >= active_order['tp']:
                                active_order, order_status = None, None
                                msg = f"🎯 **تم تحقيق الهدف بنجاح [{symbol}]!**\nسعر الإغلاق: `{live_p}`"
                                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                            elif live_p <= active_order['sl']:
                                active_order, order_status = None, None
                                msg = f"🔴 **تم ضرب وقوف الخسارة [{symbol}].**\nسعر الإغلاق: `{live_p}`"
                                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        elif active_order['type'] == 'Sell Stop':
                            if live_p <= active_order['tp']:
                                active_order, order_status = None, None
                                msg = f"🎯 **تم تحقيق الهدف بنجاح [{symbol}]!**\nسعر الإغلاق: `{live_p}`"
                                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                            elif live_p >= active_order['sl']:
                                active_order, order_status = None, None
                                msg = f"🔴 **تم ضرب وقوف الخسارة [{symbol}].**\nسعر الإغلاق: `{live_p}`"
                                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            print(f"Fast Monitor Exception: {e}")
        await asyncio.sleep(8)

async def market_scanner_loop(bot: Bot, chat_id: str):
    global send_status_reports, scan_target
    while True:
        try:
            await wait_for_next_check()
            
            # تحديد الأصول المستهدفة بالمسح
            symbols_to_scan = []
            if scan_target in ["gold_only", "gold_forex", "all"]:
                symbols_to_scan.append("XAU/USD")
            if scan_target in ["gold_forex", "all"]:
                symbols_to_scan.extend(FOREX_PAIRS)
            if scan_target in ["stocks_only", "all"]:
                symbols_to_scan.extend(STOCKS_LIST)

            for sym in symbols_to_scan:
                order, event, spot_price, basis, rsi, recommended_lot = analyze_market_symbol(sym)
                
                if order and event == "NEW_ORDER":
                    emoji = "🟢" if order['type'] == "Buy Stop" else "🔴"
                    msg = (
                        f"⚡️ **إشارة سكالبينج جديدة (M15)**\n"
                        f"⏱ **التوقيت:** {time.strftime('%H:%M')} | **الرمز:** `{sym}`\n\n"
                        f"📊 **السعر:** `{spot_price}` | **RSI:** `{rsi}`\n"
                        f"{emoji} **النوع:** {order['type']}\n"
                        f"🎯 **الدخول:** `{order['entry']}`\n"
                        f"🟢 **الهدف:** `{order['tp']}` | 🔴 **الستوب:** `{order['sl']}`\n\n"
                        f"💰 **اللوت التلقائي المقترح:** `{order['lot']}`\n"
                        f"⚖️ **المخاطرة المحددة:** {risk_percentage}%"
                    )
                    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    break # الاكتفاء بإشارة واحدة في الجولة لمنع التعارض

            # التقرير الدوري للحالة العامة
            if send_status_reports:
                status_msg = (
                    f"🔍 **تقرير فحص السوق الدوري**\n"
                    f"⏱ **التوقيت:** {time.strftime('%H:%M')}\n"
                    f"🌐 **نطاق المسح الحالي:** `{scan_target.upper()}`\n\n"
                    f"⚙️ *البوت يعمل بنجاح على فحص {len(symbols_to_scan)} أصل/رموز.*"
                )
                await bot.send_message(chat_id=chat_id, text=status_msg, parse_mode="Markdown")

        except Exception as e:
            print(f"Market Scanner Exception: {e}")
        await asyncio.sleep(10)

# ================= ================= =================
# 5. لوحة التحكم والمعالجة النصية
# ================= ================= =================
def build_settings_keyboard():
    global send_status_reports, strategy_mode, news_filter_active, risk_percentage, account_balance_cents, scan_target
    
    report_btn_text = "🔴 إيقاف التقرير الدوري" if send_status_reports else "🟢 تشغيل التقرير الدوري[span_41](start_span)"[span_41](end_span)
    mode_btn_text = "🎯 النمط: مرن (إشارات أكثر)" if strategy_mode == "flexible" else "🛡 النمط: مشدد (إشارات أقل)[span_42](start_span)"[span_42](end_span)
    news_btn_text = "🟢 فلتر الأخبار: مفعل" if news_filter_active else "🔴 فلتر الأخبار: معطل[span_43](start_span)"[span_43](end_span)
    risk_btn_text = f"🎯 نسبة المخاطرة: {risk_percentage}%[span_44](start_span)"[span_44](end_span)
    bal_btn_text = f"💰 الرصيد: {account_balance_cents} سنت (اضغط للتغيير)[span_45](start_span)"[span_45](end_span)
    
    # تحويل تسمية أزرار النطاق
    target_labels = {
        "gold_only": "🟡 الذهب فقط",
        "gold_forex": "💱 الذهب + العملات",
        "stocks_only": "📈 الأسهم فقط",
        "all": "🚀 الكل (ذهب + عملات + أسهم)"
    }
    scan_btn_text = f"🌐 نطاق المسح: {target_labels.get(scan_target, '🟡 الذهب فقط')}"

    keyboard = [
        [InlineKeyboardButton(scan_btn_text, callback_data="toggle_scan_target")],
        [InlineKeyboardButton(report_btn_text, callback_data="toggle_report")],
        [InlineKeyboardButton(mode_btn_text, callback_data="toggle_mode")],
        [InlineKeyboardButton(news_btn_text, callback_data="toggle_news")],
        [InlineKeyboardButton(risk_btn_text, callback_data="toggle_risk")],
        [InlineKeyboardButton(bal_btn_text, callback_data="prompt_balance")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global send_status_reports, strategy_mode, news_filter_active, risk_percentage, account_balance_cents, scan_target
    rep_status = "مُفعل 🟢" if send_status_reports else "معطل 🔴[span_46](start_span)"[span_46](end_span)
    mode_status = "مرن ⚡️" if strategy_mode == "flexible" else "مشدد 🛡[span_47](start_span)"[span_47](end_span)
    news_status = "مُفعل 📰" if news_filter_active else "معطل ❌[span_48](start_span)"[span_48](end_span)
    
    await update.message.reply_text(
        f"⚙️ **لوحة تحكم إعدادات البوت المطور**\n\n"
        f"▪️ نطاق المسح: **{scan_target.upper()}**\n"
        f"▪️ التقرير الدوري كل 5 دقائق: **{rep_status}**\n"
        f"▪️ نمط الفلترة والتداول: **{mode_status}**\n"
        f"▪️ فلتر الأخبار الاقتصادية: **{news_status}**\n"
        f"▪️ نسبة المخاطرة: **{risk_percentage}%**\n"
        f"▪️ رصيد الحساب الحالي: **{account_balance_cents} سنت**\n\n"
        f"💡 *لتغيير الرصيد يدوياً، أرسل رسالة بالصيغة:* `balance 150000`",[span_49](start_span)[span_49](end_span)
        reply_markup=build_settings_keyboard(),
        parse_mode="Markdown"
    )

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global account_balance_cents
    text = update.message.text.strip()
    
    if text.lower().startswith("balance ") or text.lower().startswith("رصيد "):[span_50](start_span)[span_50](end_span)
        try:
            val = int(text.split()[1])[span_51](start_span)[span_51](end_span)
            account_balance_cents = val[span_52](start_span)[span_52](end_span)
            await update.message.reply_text(f"✅ **تم تحديث رصيد الحساب بنجاح إلى:** `{account_balance_cents}` سنت", parse_mode="Markdown")[span_53](start_span)[span_53](end_span)
        except Exception:
            await update.message.reply_text("❌ **خطأ في الصيغة!** اكتب الكلمة متبوعة بالرقم فقط، مثال:\n`balance 150000`", parse_mode="Markdown")[span_54](start_span)[span_54](end_span)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global send_status_reports, strategy_mode, news_filter_active, risk_percentage, account_balance_cents, scan_target
    query = update.callback_query
    await query.answer()
    
    if query.data == "toggle_scan_target":
        modes = ["gold_only", "gold_forex", "stocks_only", "all"]
        next_idx = (modes.index(scan_target) + 1) % len(modes)
        scan_target = modes[next_idx]
    elif query.data == "toggle_report":
        send_status_reports = not send_status_reports[span_55](start_span)[span_55](end_span)
    elif query.data == "toggle_mode":
        strategy_mode = "strict" if strategy_mode == "flexible" else "flexible[span_56](start_span)"[span_56](end_span)
    elif query.data == "toggle_news":
        news_filter_active = not news_filter_active[span_57](start_span)[span_57](end_span)
    elif query.data == "toggle_risk":
        risk_percentage = 1.0 if risk_percentage == 0.5 else (2.0 if risk_percentage == 1.0 else 0.5)[span_58](start_span)[span_58](end_span)
    elif query.data == "prompt_balance":
        await query.message.reply_text("✏️ **لإدخال قيمة الرصيد يدوياً:**\nأرسل رسالة تحتوي على كلمة `balance` ثم رقم الرصيد بالسنت.\n\nمثال: `balance 250000`", parse_mode="Markdown")[span_59](start_span)[span_59](end_span)
        return

    rep_status = "مُفعل 🟢" if send_status_reports else "معطل 🔴[span_60](start_span)"[span_60](end_span)
    mode_status = "مرن ⚡️" if strategy_mode == "flexible" else "مشدد 🛡[span_61](start_span)"[span_61](end_span)
    news_status = "مُفعل 📰" if news_filter_active else "معطل ❌[span_62](start_span)"[span_62](end_span)
    
    await query.edit_message_text(
        f"⚙️ **لوحة تحكم إعدادات البوت المطور**\n\n"
        f"▪️ نطاق المسح: **{scan_target.upper()}**\n"
        f"▪️ التقرير الدوري كل 5 دقائق: **{rep_status}**\n"
        f"▪️ نمط الفلترة والتداول: **{mode_status}**\n"
        f"▪️ فلتر الأخبار الاقتصادية: **{news_status}**\n"
        f"▪️ نسبة المخاطرة: **{risk_percentage}%**\n"
        f"▪️ رصيد الحساب الحالي: **{account_balance_cents} سنت**",[span_63](start_span)[span_63](end_span)
        reply_markup=build_settings_keyboard(),
        parse_mode="Markdown"
    )

# ================= ================= =================
# 6. التشغيل الرئيسي
# ================= ================= =================
def main():
    token = os.environ.get("TELEGRAM_TOKEN")[span_64](start_span)[span_64](end_span)
    chat_id = os.environ.get("CHAT_ID")[span_65](start_span)[span_65](end_span)
    
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID environment variables!")[span_66](start_span)[span_66](end_span)
        return
    
    Thread(target=run_web_server, daemon=True).start()[span_67](start_span)[span_67](end_span)
    app_bot = Application.builder().token(token).build()[span_68](start_span)[span_68](end_span)
    
    app_bot.add_handler(CommandHandler("settings", handle_settings))[span_69](start_span)[span_69](end_span)
    app_bot.add_handler(MessageHandler(filters.Regex(r'(?i)^settings$'), handle_settings))[span_70](start_span)[span_70](end_span)
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))[span_71](start_span)[span_71](end_span)
    app_bot.add_handler(CallbackQueryHandler(button_callback))[span_72](start_span)[span_72](end_span)
    
    loop = asyncio.get_event_loop()[span_73](start_span)[span_73](end_span)
    loop.create_task(market_scanner_loop(app_bot.bot, chat_id))[span_74](start_span)[span_74](end_span)
    loop.create_task(fast_price_monitor_loop(app_bot.bot, chat_id))[span_75](start_span)[span_75](end_span)
    
    print("Multi-Asset Scalper bot is active & running...")
    app_bot.run_polling()[span_76](start_span)[span_76](end_span)

if __name__ == "__main__":
    main()[span_77](start_span)[span_77](end_span)
