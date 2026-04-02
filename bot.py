"""
DIP PADEL Bot (@dippadel_bot)
Бронирование кортов падел в THE DIP, Николина Гора.

Запуск: BOT_TOKEN=<token> python bot.py
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

# Pricing tiers: (period_name, start_hour, end_hour, price_per_hour)
PRICING = [
    ("morning", 7, 12, 3500),
    ("day", 12, 18, 4500),
    ("evening", 18, 23, 5500),
]

DURATION_OPTIONS = [60, 90, 120]  # minutes

# Conversation states
DATE, TIME, DURATION, CONFIRM = range(4)
CANCEL_SELECT = 10

LOCATION_TEXT = (
    "THE DIP PADEL\n"
    "Николина Гора, Московская область\n\n"
    "Как добраться: Рублёво-Успенское шоссе, поворот на Николину Гору.\n"
    "Координаты: 55.7283, 37.0050\n\n"
    "Связь: @thedip_manager"
)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            court_id INTEGER NOT NULL DEFAULT 1,
            booking_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            total_price INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed',
            reminder_sent INTEGER DEFAULT 0,
            cancelled_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_bookings_date
            ON bookings(booking_date, status);
        CREATE INDEX IF NOT EXISTS idx_bookings_user
            ON bookings(user_id, status);
        """
    )
    conn.commit()
    conn.close()


def get_or_create_user(telegram_id: int, username: str | None,
                       first_name: str | None, last_name: str | None) -> int:
    """Return internal user id, creating record if needed."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    if row:
        user_id = row[0]
    else:
        cur.execute(
            "INSERT INTO users (telegram_id, username, first_name, last_name) "
            "VALUES (?, ?, ?, ?)",
            (telegram_id, username, first_name, last_name),
        )
        conn.commit()
        user_id = cur.lastrowid
    conn.close()
    return user_id


def get_booked_slots(date_str: str) -> list[str]:
    """Return list of start_time strings that are already booked."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT start_time FROM bookings "
        "WHERE booking_date = ? AND status = 'confirmed'",
        (date_str,),
    )
    slots = [row[0] for row in cur.fetchall()]
    conn.close()
    return slots


def create_booking(user_id: int, date_str: str, start_time: str,
                   end_time: str, duration: int, price: int) -> int:
    """Insert a booking and return its id."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bookings "
        "(user_id, booking_date, start_time, end_time, duration_minutes, total_price) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, date_str, start_time, end_time, duration, price),
    )
    conn.commit()
    booking_id = cur.lastrowid
    conn.close()
    return booking_id


def get_user_bookings(telegram_id: int) -> list[dict]:
    """Return active bookings for a user."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT b.id, b.booking_date, b.start_time, b.end_time, "
        "b.total_price, b.status "
        "FROM bookings b "
        "JOIN users u ON b.user_id = u.id "
        "WHERE u.telegram_id = ? AND b.status = 'confirmed' "
        "ORDER BY b.booking_date, b.start_time",
        (telegram_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def cancel_booking(booking_id: int) -> bool:
    """Cancel a booking. Returns True if successful."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE bookings SET status = 'cancelled', "
        "cancelled_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status = 'confirmed'",
        (booking_id,),
    )
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def get_pending_reminders() -> list[dict]:
    """Return bookings that need a reminder (2h before, not yet sent)."""
    now = datetime.now(TZ)
    target = now + timedelta(hours=2)
    target_date = target.strftime("%Y-%m-%d")
    target_time = target.strftime("%H:%M")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT b.id, b.booking_date, b.start_time, b.end_time, "
        "b.total_price, u.telegram_id "
        "FROM bookings b "
        "JOIN users u ON b.user_id = u.id "
        "WHERE b.status = 'confirmed' "
        "AND b.reminder_sent = 0 "
        "AND b.booking_date = ? "
        "AND b.start_time <= ?",
        (target_date, target_time),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def mark_reminder_sent(booking_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE bookings SET reminder_sent = 1 WHERE id = ?",
        (booking_id,),
    )
    conn.commit()
    conn.close()


def mark_completed_bookings() -> int:
    """Mark past bookings as completed. Returns count."""
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE bookings SET status = 'completed' "
        "WHERE status = 'confirmed' "
        "AND (booking_date < ? OR (booking_date = ? AND end_time <= ?))",
        (today, today, current_time),
    )
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def price_for_hour(hour: int) -> int:
    """Return price per hour for a given start hour."""
    for _, start, end, price in PRICING:
        if start <= hour < end:
            return price
    return 0


def calculate_total(start_hour: int, duration_minutes: int) -> int:
    """Calculate total price accounting for tier boundaries."""
    total = 0
    minutes_left = duration_minutes
    h = start_hour
    while minutes_left > 0:
        chunk = min(minutes_left, 60)
        rate = price_for_hour(h)
        total += int(rate * chunk / 60)
        minutes_left -= chunk
        h += 1
    return total


def format_date(date_str: str) -> str:
    """Format 2026-04-05 -> '05.04 (Сб)'."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return f"{dt.strftime('%d.%m')} ({days_ru[dt.weekday()]})"


def available_dates(days: int = 7) -> list[str]:
    """Return next N days as YYYY-MM-DD strings."""
    today = datetime.now(TZ).date()
    return [(today + timedelta(days=i)).isoformat() for i in range(days)]


def available_hours() -> list[int]:
    """Return all bookable start hours."""
    hours: list[int] = []
    for _, start, end, _ in PRICING:
        hours.extend(range(start, end))
    return hours


# ---------------------------------------------------------------------------
# Handlers: /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    get_or_create_user(user.id, user.username, user.first_name, user.last_name)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Забронировать корт", callback_data="menu_book")],
        [InlineKeyboardButton("Мои бронирования", callback_data="menu_mybookings")],
        [
            InlineKeyboardButton("Тарифы", callback_data="menu_prices"),
            InlineKeyboardButton("Контакты", callback_data="menu_contacts"),
        ],
    ])

    await update.message.reply_text(
        "Добро пожаловать в THE DIP PADEL!\n"
        "Николина Гора\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Handlers: Prices
# ---------------------------------------------------------------------------

PRICES_TEXT = (
    "ТАРИФЫ THE DIP PADEL\n\n"
    "Утро (07:00-12:00): 3 500 руб/час\n"
    "День (12:00-18:00): 4 500 руб/час\n"
    "Вечер (18:00-23:00): 5 500 руб/час\n\n"
    "Бесплатная отмена за 24 часа до начала."
)


async def cmd_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(PRICES_TEXT)


async def cb_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(PRICES_TEXT)


# ---------------------------------------------------------------------------
# Handlers: Contacts
# ---------------------------------------------------------------------------

async def cmd_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(LOCATION_TEXT)


async def cb_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(LOCATION_TEXT)


# ---------------------------------------------------------------------------
# Handlers: Booking Conversation
# ---------------------------------------------------------------------------

async def booking_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for booking: show date selection."""
    # Could come from /book command or inline button
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        send = query.edit_message_text
    else:
        send = update.message.reply_text

    dates = available_dates(7)
    buttons = []
    row: list[InlineKeyboardButton] = []
    for d in dates:
        row.append(InlineKeyboardButton(format_date(d), callback_data=f"date_{d}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("<< Отмена", callback_data="booking_cancel")])

    await send("Выберите дату:", reply_markup=InlineKeyboardMarkup(buttons))
    return DATE


async def booking_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked a date, show available time slots."""
    query = update.callback_query
    await query.answer()

    date_str = query.data.replace("date_", "")
    context.user_data["booking_date"] = date_str

    booked = set(get_booked_slots(date_str))
    hours = available_hours()

    current_period = ""
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for h in hours:
        period_label = ""
        for name, start, end, price in PRICING:
            if start <= h < end:
                period_label = {
                    "morning": f"Утро ({price} руб/час)",
                    "day": f"День ({price} руб/час)",
                    "evening": f"Вечер ({price} руб/час)",
                }[name]
                break

        if period_label != current_period:
            if row:
                buttons.append(row)
                row = []
            current_period = period_label
            buttons.append([
                InlineKeyboardButton(f"-- {period_label} --", callback_data="noop")
            ])

        slot = f"{h:02d}:00"
        is_booked = slot in booked
        label = f"{'--' if is_booked else ''}{slot}{'--' if is_booked else ''}"
        cb_data = f"time_{h}" if not is_booked else "noop"
        row.append(InlineKeyboardButton(label, callback_data=cb_data))
        if len(row) == 4:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("<< Назад к датам", callback_data="back_to_dates")
    ])

    await query.edit_message_text(
        f"Доступные слоты на {format_date(date_str)}:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return TIME


async def booking_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked a time, show duration options."""
    query = update.callback_query
    await query.answer()

    if query.data == "back_to_dates":
        return await booking_start(update, context)

    hour = int(query.data.replace("time_", ""))
    context.user_data["booking_hour"] = hour

    buttons = []
    for d in DURATION_OPTIONS:
        end_hour = hour + d // 60
        end_min = d % 60
        end_str = f"{end_hour:02d}:{end_min:02d}"
        price = calculate_total(hour, d)
        label = f"{d // 60}{'.' + str(d % 60) if d % 60 else ''} ч -- {price} руб"
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"dur_{d}")
        ])
    buttons.append([
        InlineKeyboardButton("<< Назад", callback_data="back_to_times")
    ])

    await query.edit_message_text(
        f"Время: {hour:02d}:00\nВыберите длительность:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return DURATION


async def booking_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked duration, show confirmation."""
    query = update.callback_query
    await query.answer()

    if query.data == "back_to_times":
        return await booking_date(update, context)

    duration = int(query.data.replace("dur_", ""))
    hour = context.user_data["booking_hour"]
    date_str = context.user_data["booking_date"]

    end_minutes = hour * 60 + duration
    end_h, end_m = divmod(end_minutes, 60)
    start_str = f"{hour:02d}:00"
    end_str = f"{end_h:02d}:{end_m:02d}"
    price = calculate_total(hour, duration)

    context.user_data["booking_start"] = start_str
    context.user_data["booking_end"] = end_str
    context.user_data["booking_duration"] = duration
    context.user_data["booking_price"] = price

    cancel_deadline = (
        datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=hour, tzinfo=TZ
        ) - timedelta(hours=24)
    ).strftime("%d.%m.%Y %H:%M")

    text = (
        "Подтвердите бронирование:\n\n"
        f"Корт: 1\n"
        f"Дата: {format_date(date_str)}\n"
        f"Время: {start_str} - {end_str}\n"
        f"Стоимость: {price:,} руб\n\n"
        f"Бесплатная отмена до {cancel_deadline}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Да, бронирую!", callback_data="confirm_yes"),
            InlineKeyboardButton("Отмена", callback_data="booking_cancel"),
        ]
    ])

    await query.edit_message_text(text, reply_markup=keyboard)
    return CONFIRM


async def booking_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save the booking."""
    query = update.callback_query
    await query.answer()

    if query.data == "booking_cancel":
        await query.edit_message_text("Бронирование отменено.")
        return ConversationHandler.END

    user = update.effective_user
    user_id = get_or_create_user(
        user.id, user.username, user.first_name, user.last_name
    )

    data = context.user_data
    booking_id = create_booking(
        user_id=user_id,
        date_str=data["booking_date"],
        start_time=data["booking_start"],
        end_time=data["booking_end"],
        duration=data["booking_duration"],
        price=data["booking_price"],
    )

    await query.edit_message_text(
        f"Готово! Бронирование #{booking_id}\n\n"
        f"Дата: {format_date(data['booking_date'])}\n"
        f"Время: {data['booking_start']} - {data['booking_end']}\n"
        f"Стоимость: {data['booking_price']:,} руб\n\n"
        f"Напоминание придёт за 2 часа.\n"
        f"Отмена: /cancel"
    )

    # Clean up user_data
    for key in list(data.keys()):
        if key.startswith("booking_"):
            del data[key]

    return ConversationHandler.END


async def booking_cancel_conv(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel the booking conversation."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Бронирование отменено.")
    else:
        await update.message.reply_text("Бронирование отменено.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Handlers: My Bookings
# ---------------------------------------------------------------------------

async def cmd_mybookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    bookings = get_user_bookings(user.id)

    if not bookings:
        text = "У вас нет активных бронирований."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    lines = ["Ваши бронирования:\n"]
    buttons: list[list[InlineKeyboardButton]] = []

    for b in bookings:
        lines.append(
            f"#{b['id']}  {format_date(b['booking_date'])} "
            f"{b['start_time']}-{b['end_time']}  "
            f"{b['total_price']:,} руб"
        )
        buttons.append([
            InlineKeyboardButton(
                f"Отменить #{b['id']}", callback_data=f"cancelbook_{b['id']}"
            )
        ])

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(buttons) if buttons else None

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Handlers: Cancel Booking
# ---------------------------------------------------------------------------

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bookings to cancel."""
    await cmd_mybookings(update, context)


async def cb_cancel_booking(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Process cancellation of a specific booking."""
    query = update.callback_query
    await query.answer()

    booking_id = int(query.data.replace("cancelbook_", ""))

    # Check if cancellation is free (>24h before)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT booking_date, start_time FROM bookings WHERE id = ?",
        (booking_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        await query.edit_message_text("Бронирование не найдено.")
        return

    booking_dt = datetime.strptime(
        f"{row['booking_date']} {row['start_time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=TZ)
    now = datetime.now(TZ)
    is_free = (booking_dt - now) > timedelta(hours=24)

    success = cancel_booking(booking_id)
    if success:
        if is_free:
            await query.edit_message_text(
                f"Бронирование #{booking_id} отменено (бесплатно)."
            )
        else:
            await query.edit_message_text(
                f"Бронирование #{booking_id} отменено.\n"
                "Внимание: отмена менее чем за 24 часа."
            )
    else:
        await query.edit_message_text("Не удалось отменить бронирование.")


# ---------------------------------------------------------------------------
# Handlers: Help
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "THE DIP PADEL -- Помощь\n\n"
        "/start -- Главное меню\n"
        "/book -- Забронировать корт\n"
        "/mybookings -- Мои бронирования\n"
        "/cancel -- Отменить бронирование\n"
        "/prices -- Тарифы\n"
        "/contacts -- Контакты и адрес\n"
        "/help -- Эта справка"
    )


# ---------------------------------------------------------------------------
# Callback router for noop
# ---------------------------------------------------------------------------

async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

async def job_send_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check for upcoming bookings and send reminders."""
    reminders = get_pending_reminders()
    for r in reminders:
        try:
            await context.bot.send_message(
                chat_id=r["telegram_id"],
                text=(
                    f"Напоминание! Через 2 часа у вас падел.\n\n"
                    f"Дата: {format_date(r['booking_date'])}\n"
                    f"Время: {r['start_time']} - {r['end_time']}\n\n"
                    f"До встречи в THE DIP!"
                ),
            )
            mark_reminder_sent(r["id"])
        except Exception as exc:
            logger.error("Failed to send reminder for booking %s: %s", r["id"], exc)


async def job_mark_completed(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark past bookings as completed."""
    count = mark_completed_bookings()
    if count:
        logger.info("Marked %d bookings as completed", count)


# ---------------------------------------------------------------------------
# Post-init: set commands & schedule jobs
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    """Set bot commands and schedule recurring jobs."""
    await application.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("book", "Забронировать корт"),
        BotCommand("mybookings", "Мои бронирования"),
        BotCommand("cancel", "Отменить бронирование"),
        BotCommand("prices", "Тарифы"),
        BotCommand("contacts", "Контакты"),
        BotCommand("help", "Справка"),
    ])

    job_queue = application.job_queue
    # Reminder check every 15 minutes
    job_queue.run_repeating(job_send_reminders, interval=900, first=10)
    # Mark completed bookings daily at midnight Moscow time
    job_queue.run_daily(
        job_mark_completed,
        time=time(hour=0, minute=5, tzinfo=TZ),
    )

    logger.info("DIP PADEL Bot initialized. Jobs scheduled.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN environment variable is not set. "
            "Run: BOT_TOKEN=<your_token> python bot.py"
        )

    init_db()

    app = Application.builder().token(token).post_init(post_init).build()

    # Booking conversation
    booking_conv = ConversationHandler(
        entry_points=[
            CommandHandler("book", booking_start),
            CallbackQueryHandler(booking_start, pattern="^menu_book$"),
        ],
        states={
            DATE: [CallbackQueryHandler(booking_date, pattern=r"^date_")],
            TIME: [
                CallbackQueryHandler(booking_time, pattern=r"^time_"),
                CallbackQueryHandler(booking_start, pattern="^back_to_dates$"),
            ],
            DURATION: [
                CallbackQueryHandler(booking_duration, pattern=r"^dur_"),
                CallbackQueryHandler(booking_date, pattern="^back_to_times$"),
            ],
            CONFIRM: [
                CallbackQueryHandler(booking_confirm, pattern="^confirm_yes$"),
                CallbackQueryHandler(booking_cancel_conv, pattern="^booking_cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(booking_cancel_conv, pattern="^booking_cancel$"),
            CommandHandler("start", cmd_start),
        ],
    )

    app.add_handler(booking_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("mybookings", cmd_mybookings))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("prices", cmd_prices))
    app.add_handler(CommandHandler("contacts", cmd_contacts))
    app.add_handler(CommandHandler("help", cmd_help))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(cmd_mybookings, pattern="^menu_mybookings$"))
    app.add_handler(CallbackQueryHandler(cb_prices, pattern="^menu_prices$"))
    app.add_handler(CallbackQueryHandler(cb_contacts, pattern="^menu_contacts$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_booking, pattern=r"^cancelbook_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern="^noop$"))

    logger.info("Starting DIP PADEL Bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
