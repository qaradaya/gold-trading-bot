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
    return "Pro Scalper M15 Gold Bot is Live!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

active_order = None
order_status = None 
send_status_reports = True  
strategy_mode = "flexible"   

async def wait_for_next_check():
    while True:
        now = datetime.utcnow()
        if now.minute % 5 == 0 and now.second < 3:
            break
        await asyncio.sleep(1)

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

def get_live_price():
    api_key = os.environ.get("TWELVE_DATA_API_KEY")
    if not api_key:
        return None
    try:
        url = f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={api_key}"
        res = requests.get(url, timeout=4).json()
        if "price" in res:
            return float(res["price"])
    except Exception as e:
        print(f"Live Price Error: {e}")
    return None

def analyze_gold_market():
    global active_order, order_status, strategy_mode
    
    now_utc = datetime.utcnow()
    if now_utc.hour == 21:
        return None, "MARKET_CLOSED", 0, 0, 0

    api_key = os.environ.get("TWELVE_DATA_API_KEY")
    if not api_key:
        return None, "NO_KEY", 0, 0, 0

    try:
        url_spot = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=15min&outputsize=40&apikey={api_key}"
        res_spot = requests.get(url_spot, timeout=6).json()

        url_futures = f"https://api.twelvedata.com/time_series?symbol=MGC&interval=15min&outputsize=40&apikey={api_key}"
        res_futures = requests.get(url_futures, timeout=6).json()

        if "values" not in res_spot:
            return None, "API_ERROR", 0, 0, 0

        spot_values = res_spot["values"]
        spot_values.reverse()

        spot_closes = [float(item["close"]) for item in spot_values]
        spot_highs = [float(item["high"]) for item in spot_values]
        spot_lows = [float(item["low"]) for item in spot_values]

        if "values" in res_futures:
            futures_values = res_futures["values"]
            futures_values.reverse()
            futures_closes = [float(item["close"]) for item in futures_values]
            futures_volumes = [float(item.get("volume", 0)) for item in futures_values]
        else:
            futures_closes = spot_closes
            futures_volumes = [100] * len(spot_closes)

        spot_price = round(spot_closes[-1], 2)
        futures_price = round(futures_closes[-1], 2)

        basis_current = round(futures_price - spot_price, 2)
        basis_prev = round(futures_closes[-2] - spot_closes[-2], 2)
        basis_expansion = round(basis_current - basis_prev, 2)

        volume_current = futures_volumes[-1]
        volume_avg = sum(futures_volumes[-5:-1]) / 4 if len(futures_volumes) >= 5 else volume_current
        volume_surging = volume_current > volume_avg

        ema20 = calculate_ema(spot_closes, 20)[-1]
        ema50 = calculate_ema(spot_closes, 50)[-1]
        rsi = calculate_rsi(spot_closes, 14)

        tight_swing_high = max(spot_highs[-3:-1])
        tight_swing_low = min(spot_lows[-3:-1])

        if active_order is not None:
            status_event = "STILL_TRIGGERED" if order_status == "TRIGGERED" else "STILL_PENDING"
            return active_order, status_event, spot_price, basis_current, rsi

        # حد إضافي للسلامة لمنع الشراء وقت الانهيار المباشر
        rsi_buy_max = 75 if strategy_mode == "flexible" else 68
        rsi_buy_min = 30  # حظر الشراء لو الـ RSI حاد الهبوط تحت 30
        
        rsi_sell_min = 25 if strategy_mode == "flexible" else 32
        rsi_sell_max = 70 # حظر البيع لو الـ RSI مرتفع جدا فوق 70

        basis_threshold = 0.05 if strategy_mode == "flexible" else 0.10
        max_risk = 10.00 if strategy_mode == "flexible" else 8.00

        # شرط الشراء
        if ema20 > ema50 and (rsi_buy_min <= rsi < rsi_buy_max) and (basis_expansion >= basis_threshold or volume_surging):
            proposed_entry = round(min(max(tight_swing_high, spot_price) + 0.50, spot_price + 3.00), 2)
            sl_price = round(tight_swing_low - 0.50, 2)
            
            if proposed_entry > spot_price > sl_price:
                risk_distance = proposed_entry - sl_price
                if risk_distance <= max_risk:
                    tp_price = round(proposed_entry + (risk_distance * 1.5), 2)
                    active_order = {"type": "Buy Stop", "entry": proposed_entry, "tp": tp_price, "sl": sl_price, "rsi": rsi}
                    order_status = "PENDING"
                    return active_order, "NEW_ORDER", spot_price, basis_current, rsi

        # شرط البيع
        elif ema20 < ema50 and (rsi_sell_min < rsi <= rsi_sell_max) and (basis_expansion <= -basis_threshold or volume_surging):
            proposed_entry = round(max(min(tight_swing_low, spot_price) - 0.50, spot_price - 3.00), 2)
            sl_price = round(tight_swing_high + 0.50, 2)

            if proposed_entry < spot_price < sl_price:
                risk_distance = sl_price - proposed_entry
                if risk_distance <= max_risk:
                    tp_price = round(proposed_entry - (risk_distance * 1.5), 2)
                    active_order = {"type": "Sell Stop", "entry": proposed_entry, "tp": tp_price, "sl": sl_price, "rsi": rsi}
                    order_status = "PENDING"
                    return active_order, "NEW_ORDER", spot_price, basis_current, rsi

        return None, "NO_SIGNAL", spot_price, basis_current, rsi

    except Exception as e:
        print(f"Execution Error: {e}")
        return None, "ERROR", 0, 0, 0

# المراقبة الفورية للسعر
async def fast_price_monitor_loop(bot: Bot, chat_id: str):
    global active_order, order_status
    while True:
        if active_order is not None:
            live_p = get_live_price()
            if live_p is not None:
                if order_status == "PENDING":
                    if active_order['type'] == 'Buy Stop' and live_p <= active_order['sl']:
                        active_order = None
                        order_status = None
                        msg = f"🚫 **تنبيه فوري: تم إلغاء صفقة الشراء المعلقة!**\nالسعر ضرب مستوى الستوب (`{live_p}`) قبل التفعيل."
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    
                    elif active_order['type'] == 'Sell Stop' and live_p >= active_order['sl']:
                        active_order = None
                        order_status = None
                        msg = f"🚫 **تنبيه فوري: تم إلغاء صفقة البيع المعلقة!**\nالسعر ضرب مستوى الستوب (`{live_p}`) قبل التفعيل."
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

                    elif active_order['type'] == 'Buy Stop' and live_p >= active_order['entry']:
                        order_status = "TRIGGERED"
                        msg = f"⚡️ **تم تفعيل صفقة الشراء فوراً!**\n📍 **سعر التفعيل الحقيقي:** `{live_p}`"
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

                    elif active_order['type'] == 'Sell Stop' and live_p <= active_order['entry']:
                        order_status = "TRIGGERED"
                        msg = f"⚡️ **تم تفعيل صفقة البيع فوراً!**\n📍 **سعر التفعيل الحقيقي:** `{live_p}`"
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

                elif order_status == "TRIGGERED":
                    if active_order['type'] == 'Buy Stop':
                        if live_p >= active_order['tp']:
                            active_order = None
                            order_status = None
                            msg = f"🎯 **تم تحقيق الهدف بنجاح!**\nسعر الإغلاق: `{live_p}`"
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        elif live_p <= active_order['sl']:
                            active_order = None
                            order_status = None
                            msg = f"🔴 **تم ضرب وقوف الخسارة.**\nسعر الإغلاق: `{live_p}`"
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

                    elif active_order['type'] == 'Sell Stop':
                        if live_p <= active_order['tp']:
                            active_order = None
                            order_status = None
                            msg = f"🎯 **تم تحقيق الهدف بنجاح!**\nسعر الإغلاق: `{live_p}`"
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        elif live_p >= active_order['sl']:
                            active_order = None
                            order_status = None
                            msg = f"🔴 **تم ضرب وقوف الخسارة.**\nسعر الإغلاق: `{live_p}`"
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

        await asyncio.sleep(4)

async def market_scanner_loop(bot: Bot, chat_id: str):
    global send_status_reports
    while True:
        await wait_for_next_check()
        order, event, spot_price, basis, rsi = analyze_gold_market()
        
        # إرسال التقرير الدوري دائماً عند تفعيل الخيار
        if send_status_reports and (event == "NO_SIGNAL" or event in ["STILL_PENDING", "STILL_TRIGGERED"]):
            status_msg = (
                f"🔍 **تقرير فحص السوق (نشط)**\n"
                f"⏱ **التوقيت:** {time.strftime('%H:%M')} | **XAUUSD**\n\n"
                f"📊 **سعر المنصة:** `{spot_price}`\n"
                f"📈 **RSI:** `{rsi}` | **الآجل/الفوري:** `{basis}`\n\n"
                f"⚙️ *البوت يعمل بنجاح ولا توجد إشارة جديدة.*"
            )
            try:
                await bot.send_message(chat_id=chat_id, text=status_msg, parse_mode="Markdown")
            except Exception as e:
                print(f"Send Error: {e}")

        elif order and event == "NEW_ORDER":
            emoji = "🟢" if order['type'] == "Buy Stop" else "🔴"
            msg = (
                f"⚡️ **إشارة سكالبينج جديدة (M15)**\n"
                f"⏱ **التوقيت:** {time.strftime('%H:%M')} | **XAUUSD**\n\n"
                f"📊 **السعر:** `{spot_price}` | **RSI:** `{rsi}`\n"
                f"{emoji} **النوع:** {order['type']}\n"
                f"🎯 **الدخول:** `{order['entry']}`\n"
                f"🟢 **الهدف:** `{order['tp']}` | 🔴 **الستوب:** `{order['sl']}`"
            )
            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                print(f"Send Error: {e}")

        await asyncio.sleep(10)

def build_settings_keyboard():
    global send_status_reports, strategy_mode
    report_btn_text = "🔴 إيقاف التقرير الدوري" if send_status_reports else "🟢 تشغيل التقرير الدوري"
    mode_btn_text = "🎯 النمط الحقيقي: مرن (إشارات أكثر)" if strategy_mode == "flexible" else "🛡 النمط الحقيقي: مشدد (إشارات أقل)"
    
    keyboard = [
        [InlineKeyboardButton(report_btn_text, callback_data="toggle_report")],
        [InlineKeyboardButton(mode_btn_text, callback_data="toggle_mode")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global send_status_reports, strategy_mode
    rep_status = "مُفعل 🟢" if send_status_reports else "معطل 🔴"
    mode_status = "مرن (كثافة إشارات أسرع) ⚡️" if strategy_mode == "flexible" else "مشدد (أعلى تصفية للسيولة) 🛡"
    
    await update.message.reply_text(
        f"⚙️ **لوحة تحكم إعدادات البوت**\n\n"
        f"▪️ التقرير الدوري كل 5 دقائق: **{rep_status}**\n"
        f"▪️ نمط الفلترة والتداول: **{mode_status}**",
        reply_markup=build_settings_keyboard(),
        parse_mode="Markdown"
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global send_status_reports, strategy_mode
    query = update.callback_query
    await query.answer()

    if query.data == "toggle_report":
        send_status_reports = not send_status_reports
    elif query.data == "toggle_mode":
        strategy_mode = "strict" if strategy_mode == "flexible" else "flexible"

    rep_status = "مُفعل 🟢" if send_status_reports else "معطل 🔴"
    mode_status = "مرن (كثافة إشارات أسرع) ⚡️" if strategy_mode == "flexible" else "مشدد (أعلى تصفية للسيولة) 🛡"

    await query.edit_message_text(
        f"⚙️ **لوحة تحكم إعدادات البوت**\n\n"
        f"▪️ التقرير الدوري كل 5 دقائق: **{rep_status}**\n"
        f"▪️ نمط الفلترة والتداول: **{mode_status}**",
        reply_markup=build_settings_keyboard(),
        parse_mode="Markdown"
    )

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID environment variables!")
        return

    Thread(target=run_web_server, daemon=True).start()

    app_bot = Application.builder().token(token).build()

    app_bot.add_handler(CommandHandler("settings", handle_settings))
    app_bot.add_handler(MessageHandler(filters.Regex(r'(?i)^settings$'), handle_settings))
    app_bot.add_handler(CallbackQueryHandler(button_callback))

    loop = asyncio.get_event_loop()
    loop.create_task(market_scanner_loop(app_bot.bot, chat_id))
    loop.create_task(fast_price_monitor_loop(app_bot.bot, chat_id))

    print("Bot fast-monitor active & listening...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
