import psutil
import win32api
import json
import os
from datetime import datetime
from telegram import Update
from telegram.ext import Application, ContextTypes, CommandHandler
from config import TOKEN, CHAT_ID, DATA_FILE, CHECK_INTERVAL, MIN_CHANGE_PERCENT, EDIT_COOLDOWN, CRITICAL_LEVEL, CRITICAL_ALERT_LIFETIME

class BatteryBotData:
    def __init__(self):
        self.last_message_id = None
        self.last_percent = None
        self.last_charge_time = None
        self.last_update_timestamp = 0
        self.critical_alert_sent = False
        self.last_battery_check_timestamp = 0

    def save(self):
        # Сохраняем только при изменении данных
        try:
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.__dict__, f, ensure_ascii=False)
        except Exception:
            pass

    @classmethod
    def load(cls):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            obj = cls()
            obj.__dict__.update(data)
            return obj
        except Exception:
            return cls()

async def update_battery_message(context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data.setdefault('battery_data', BatteryBotData.load())

    now_ts = datetime.now().timestamp()
    last_check = bot_data.last_battery_check_timestamp
    # Проверка выхода из сна по задержке между вызовами
    if last_check and now_ts - last_check > CHECK_INTERVAL * 2:
        try:
            await context.bot.send_message(chat_id=CHAT_ID, text="💤 Ноутбук вышел из режима сна, бот продолжает работу.")
        except Exception:
            pass

    bot_data.last_battery_check_timestamp = now_ts

    battery = psutil.sensors_battery()
    if battery is None:
        return

    current_percent = battery.percent

    # Критический уровень заряда
    if current_percent < CRITICAL_LEVEL and not battery.power_plugged:
        if not bot_data.critical_alert_sent:
            await send_critical_alert(context, battery)
            bot_data.critical_alert_sent = True
            bot_data.save()
        return
    elif bot_data.critical_alert_sent and (current_percent >= CRITICAL_LEVEL or battery.power_plugged):
        bot_data.critical_alert_sent = False

    should_update = (
        bot_data.last_percent is None or
        abs(current_percent - bot_data.last_percent) >= MIN_CHANGE_PERCENT or
        current_percent == 100
    )
    if should_update and now_ts - bot_data.last_update_timestamp >= EDIT_COOLDOWN:
        current_time = get_charge_time() if battery.power_plugged else None
        await send_status(context, battery)
        bot_data.last_percent = current_percent
        bot_data.last_charge_time = current_time
        bot_data.last_update_timestamp = now_ts

    bot_data.save()

async def send_status(context, battery, force=False):
    bot_data = context.bot_data['battery_data']
    status = format_battery_status(battery)
    try:
        if bot_data.last_message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=CHAT_ID,
                    message_id=bot_data.last_message_id,
                    text=status
                )
            except Exception:
                msg = await context.bot.send_message(chat_id=CHAT_ID, text=status)
                bot_data.last_message_id = msg.message_id
        else:
            msg = await context.bot.send_message(
                chat_id=CHAT_ID,
                text=status,
                disable_notification=not force
            )
            bot_data.last_message_id = msg.message_id
        bot_data.save()
    except Exception:
        pass

async def send_critical_alert(context, battery):
    text = (
        "⚠️ *КРИТИЧЕСКИЙ УРОВЕНЬ ЗАРЯДА!*\n"
        f"Текущий заряд: {battery.percent}%\n"
        "🔌 Подключите зарядку немедленно!"
    )
    try:
        msg = await context.bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="Markdown",
            disable_notification=False
        )
        context.job_queue.run_once(
            delete_message,
            when=CRITICAL_ALERT_LIFETIME,
            data={"message_id": msg.message_id},
            name=f"delete_alert_{msg.message_id}"
        )
    except Exception:
        pass

async def delete_message(context: ContextTypes.DEFAULT_TYPE):
    message_id = context.job.data.get("message_id")
    try:
        await context.bot.delete_message(chat_id=CHAT_ID, message_id=message_id)
    except Exception:
        pass

def format_battery_status(battery):
    percent = battery.percent
    lines = [
        f"🔋 Уровень заряда: {percent}%",
        "⚡ Заряжается" if battery.power_plugged else "🔌 От батареи"
    ]

    if battery.power_plugged:
        if percent < 100:
            time_sec = get_charge_time()
            if time_sec and time_sec > 0:
                lines.append(format_time(time_sec))
        else:
            lines.append("✅ Полностью заряжено")
    else:
        est_runtime = estimate_runtime()
        if est_runtime:
            lines.append(f"⏳ Оставшееся время работы: {format_time(est_runtime)}")

    if percent < 20 and not battery.power_plugged:
        lines.append(f"⚠ Низкий заряд! {percent}%")

    lines.append(f"🕒 Обновлено: {datetime.now().strftime('%H:%M:%S')}")
    return "\n".join(lines)

def get_charge_time():
    try:
        power_status = win32api.GetSystemPowerStatus()
        return power_status.get('BatteryLifeTime', None) if isinstance(power_status, dict) else None
    except Exception:
        return None

def estimate_runtime():
    try:
        power_status = win32api.GetSystemPowerStatus()
        if isinstance(power_status, dict) and power_status.get('BatteryLifeTime', -1) != -1:
            return power_status['BatteryLifeTime']
    except Exception:
        pass
    return None

def format_time(seconds):
    hours, remainder = divmod(seconds, 3600)
    mins = remainder // 60
    return f"{hours}ч {mins}мин" if hours > 0 else f"{mins}мин"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    battery = psutil.sensors_battery()
    if battery is None:
        await update.message.reply_text("❌ Датчик батареи не найден")
        return
    await update.message.reply_text(
        "🔋 Battery Bot - Мониторинг батареи\n"
        "Статус будет автоматически обновляться в закреплённом сообщении\n"
        "Текущий статус:\n" + format_battery_status(battery)
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    battery = psutil.sensors_battery()
    if battery is None:
        await update.message.reply_text("❌ Датчик батареи не найден")
        return
    await update.message.reply_text(
        "🔋 Текущий статус батареи:\n" + format_battery_status(battery)
    )

async def notify_startup(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text="🟢 Ноутбук включён или вышел из сна. Бот запущен.")
    except Exception:
        pass

def main():
    os.makedirs(os.path.dirname(DATA_FILE) or '.', exist_ok=True)

    app = Application.builder().token(TOKEN).build()
    app.bot_data['battery_data'] = BatteryBotData.load()

    app.job_queue.run_repeating(
        update_battery_message,
        interval=CHECK_INTERVAL,   # выставляйте побольше, например 600 (10 минут)
        first=10,
        job_kwargs={'misfire_grace_time': 300, 'coalesce': True}
    )

    app.job_queue.run_once(notify_startup, when=1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_command))

    print("🟢 Бот успешно запущен и работает.")
    app.run_polling()

if __name__ == "__main__":
    main()
