import os
import re
import io
import requests
from datetime import datetime, date, time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pg8000.native
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

DATABASE_URL = os.environ.get("DATABASE_URL")
DEFAULT_GOAL = int(os.environ.get("DEFAULT_GOAL_G", "150"))
NINJA_API_KEY = os.environ.get("NINJA_API_KEY")
SGT = ZoneInfo("Asia/Singapore")


# ---------- Database ----------

def _parse_db_url(url: str):
    parsed = urlparse(url)
    return {
        "user": parsed.username,
        "password": parsed.password,
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/"),
    }


def get_db():
    conn_params = _parse_db_url(DATABASE_URL)
    return pg8000.native.Connection(**conn_params)


def init_db():
    conn = get_db()
    conn.run("CREATE TABLE IF NOT EXISTS entries (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, amount REAL NOT NULL, note TEXT, entry_date DATE NOT NULL, created_at TIMESTAMP NOT NULL)")
    conn.run("CREATE TABLE IF NOT EXISTS goals (user_id BIGINT PRIMARY KEY, goal REAL NOT NULL)")
    conn.run("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, chat_id BIGINT NOT NULL)")
    conn.close()


def add_entry(user_id: int, amount: float, note):
    today = date.today()
    now = datetime.now()
    conn = get_db()
    conn.run(
        "INSERT INTO entries (user_id, amount, note, entry_date, created_at) VALUES (:user_id, :amount, :note, :entry_date, :created_at)",
        user_id=user_id, amount=amount, note=note, entry_date=today, created_at=now,
    )
    conn.close()


def get_total_for_date(user_id: int, day) -> float:
    conn = get_db()
    rows = conn.run(
        "SELECT COALESCE(SUM(amount), 0) as total FROM entries WHERE user_id = :user_id AND entry_date = :entry_date",
        user_id=user_id, entry_date=day,
    )
    conn.close()
    return float(rows[0][0])


def get_entries_for_date(user_id: int, day):
    conn = get_db()
    rows = conn.run(
        "SELECT amount, note, created_at FROM entries WHERE user_id = :user_id AND entry_date = :entry_date ORDER BY created_at",
        user_id=user_id, entry_date=day,
    )
    conn.close()
    return [{"amount": r[0], "note": r[1], "created_at": r[2]} for r in rows]


def get_totals_for_range(user_id: int, start_day, end_day):
    """Returns a dict {date: total} for every day with at least one entry in range."""
    conn = get_db()
    rows = conn.run(
        "SELECT entry_date, COALESCE(SUM(amount), 0) as total FROM entries "
        "WHERE user_id = :user_id AND entry_date BETWEEN :start_day AND :end_day "
        "GROUP BY entry_date ORDER BY entry_date",
        user_id=user_id, start_day=start_day, end_day=end_day,
    )
    conn.close()
    return {r[0]: float(r[1]) for r in rows}


def get_goal(user_id: int) -> float:
    conn = get_db()
    rows = conn.run("SELECT goal FROM goals WHERE user_id = :user_id", user_id=user_id)
    conn.close()
    return float(rows[0][0]) if rows else DEFAULT_GOAL


def set_goal(user_id: int, goal: float):
    conn = get_db()
    conn.run(
        "INSERT INTO goals (user_id, goal) VALUES (:user_id, :goal) ON CONFLICT (user_id) DO UPDATE SET goal = EXCLUDED.goal",
        user_id=user_id, goal=goal,
    )
    conn.close()


def undo_last(user_id: int) -> bool:
    conn = get_db()
    rows = conn.run(
        "SELECT id FROM entries WHERE user_id = :user_id ORDER BY id DESC LIMIT 1",
        user_id=user_id,
    )
    if not rows:
        conn.close()
        return False
    conn.run("DELETE FROM entries WHERE id = :id", id=rows[0][0])
    conn.close()
    return True


def register_user(user_id: int, chat_id: int):
    conn = get_db()
    conn.run(
        "INSERT INTO users (user_id, chat_id) VALUES (:user_id, :chat_id) ON CONFLICT (user_id) DO UPDATE SET chat_id = EXCLUDED.chat_id",
        user_id=user_id, chat_id=chat_id,
    )
    conn.close()


def get_all_user_chat_ids():
    conn = get_db()
    rows = conn.run("SELECT user_id, chat_id FROM users")
    conn.close()
    return [(r[0], r[1]) for r in rows]


# ---------- Streak calculation ----------

def calculate_streak(user_id: int) -> int:
    """Counts consecutive days (ending today or yesterday) where goal was met."""
    goal = get_goal(user_id)
    streak = 0
    day = date.today()

    # If today isn't done yet, start checking from yesterday so an in-progress
    # day doesn't break an existing streak before it's even over.
    today_total = get_total_for_date(user_id, day.isoformat())
    if today_total < goal:
        day = day - timedelta(days=1)

    while True:
        total = get_total_for_date(user_id, day.isoformat())
        if total >= goal and total > 0:
            streak += 1
            day = day - timedelta(days=1)
        else:
            break
    return streak


# ---------- Food lookup ----------

def lookup_food_protein(query: str):
    """Returns (protein_grams, display_name) or (None, None) if lookup failed."""
    if not NINJA_API_KEY:
        return None, None
    try:
        resp = requests.get(
            "https://api.api-ninjas.com/v1/nutrition",
            params={"query": query},
            headers={"X-Api-Key": NINJA_API_KEY},
            timeout=10,
        )
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        if not data:
            return None, None
        total_protein = sum(item.get("protein_g", 0) for item in data)
        names = ", ".join(item.get("name", "") for item in data)
        return round(total_protein, 1), names
    except Exception as e:
        print(f"Food lookup failed: {e}")
        return None, None


# ---------- Helpers ----------

ENTRY_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*g?\s*(.*)$", re.IGNORECASE)


def progress_bar(total: float, goal: float, length: int = 10) -> str:
    if goal <= 0:
        return ""
    filled = min(length, round(length * total / goal))
    return "🟩" * filled + "⬜" * (length - filled)


def build_progress_message(user_id: int, amount_added: float, note) -> str:
    today = date.today().isoformat()
    total = get_total_for_date(user_id, today)
    goal = get_goal(user_id)
    remaining = max(goal - total, 0)
    bar = progress_bar(total, goal)
    pct = min(100, round(100 * total / goal)) if goal > 0 else 0

    note_part = f" ({note})" if note else ""
    lines = [
        f"✅ Logged {amount_added:g}g{note_part}",
        "",
        f"Today's total: {total:g}g / {goal:g}g",
        f"{bar} {pct}%",
    ]
    if remaining > 0:
        lines.append(f"Remaining: {remaining:g}g")
    else:
        lines.append("🎉 Goal reached!")

    streak = calculate_streak(user_id)
    if streak > 0:
        lines.append(f"🔥 Streak: {streak} day{'s' if streak != 1 else ''}")

    return "\n".join(lines)


# ---------- Daily summary job ----------

def build_daily_summary_message(user_id: int) -> str:
    today = date.today().isoformat()
    total = get_total_for_date(user_id, today)
    goal = get_goal(user_id)
    remaining = max(goal - total, 0)
    bar = progress_bar(total, goal)
    pct = min(100, round(100 * total / goal)) if goal > 0 else 0

    lines = [
        "🌙 Daily Protein Summary",
        "",
        f"Today's total: {total:g}g / {goal:g}g",
        f"{bar} {pct}%",
    ]
    if remaining > 0:
        lines.append(f"You still need {remaining:g}g to hit your goal today.")
    else:
        lines.append("🎉 You hit your goal today!")

    streak = calculate_streak(user_id)
    if streak > 0:
        lines.append(f"🔥 Streak: {streak} day{'s' if streak != 1 else ''}")

    return "\n".join(lines)


async def send_daily_summaries(context: ContextTypes.DEFAULT_TYPE):
    for user_id, chat_id in get_all_user_chat_ids():
        try:
            msg = build_daily_summary_message(user_id)
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"Failed to send daily summary to user {user_id}: {e}")


# ---------- Weekly summary job ----------

def build_weekly_summary_message(user_id: int) -> str:
    today = date.today()
    start = today - timedelta(days=6)
    totals = get_totals_for_range(user_id, start.isoformat(), today.isoformat())
    goal = get_goal(user_id)

    lines = ["📅 Weekly Protein Summary", ""]
    days_hit = 0
    week_total = 0.0
    for i in range(7):
        d = start + timedelta(days=i)
        total = totals.get(d, 0.0)
        week_total += total
        hit = "✅" if total >= goal and total > 0 else "▫️"
        if total >= goal and total > 0:
            days_hit += 1
        lines.append(f"{hit} {d.strftime('%a %d %b')}: {total:g}g")

    avg = week_total / 7
    lines.append("")
    lines.append(f"Days goal hit: {days_hit}/7")
    lines.append(f"Average: {avg:.0f}g/day")

    streak = calculate_streak(user_id)
    if streak > 0:
        lines.append(f"🔥 Current streak: {streak} day{'s' if streak != 1 else ''}")

    return "\n".join(lines)


async def send_weekly_summaries(context: ContextTypes.DEFAULT_TYPE):
    for user_id, chat_id in get_all_user_chat_ids():
        try:
            msg = build_weekly_summary_message(user_id)
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"Failed to send weekly summary to user {user_id}: {e}")


# ---------- Chart generation ----------

def generate_chart(user_id: int, days: int = 7):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    today = date.today()
    start = today - timedelta(days=days - 1)
    totals = get_totals_for_range(user_id, start.isoformat(), today.isoformat())
    goal = get_goal(user_id)

    dates = [start + timedelta(days=i) for i in range(days)]
    values = [totals.get(d, 0.0) for d in dates]
    labels = [d.strftime("%d/%m") for d in dates]

    colors = ["#22c55e" if v >= goal and v > 0 else "#94a3b8" for v in values]

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar(labels, values, color=colors)
    ax.axhline(y=goal, color="#ef4444", linestyle="--", linewidth=1.5, label=f"Goal ({goal:g}g)")
    ax.set_ylabel("Protein (g)")
    ax.set_title(f"Protein intake — last {days} days")
    ax.legend()
    plt.xticks(rotation=45)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(
        "👋 Protein Tracker Bot\n\n"
        "Just send me how much protein you ate, or just the food name:\n"
        "  • `30` (logs 30g)\n"
        "  • `30 chicken breast` (logs 30g with a note)\n"
        "  • `chicken breast` (looks up protein automatically)\n\n"
        "Commands:\n"
        "/today - show today's log and total\n"
        "/week - show this week's chart and summary\n"
        "/goal 150 - set your daily protein goal (g)\n"
        "/undo - remove your last entry\n"
        "/reset - clear today's entries\n\n"
        "I'll send you a summary at 11pm SGT daily, and a weekly recap every Sunday.",
        parse_mode="Markdown",
    )


async def set_goal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        goal = get_goal(user_id)
        await update.message.reply_text(f"Your current goal is {goal:g}g/day.\nUse /goal 150 to change it.")
        return
    try:
        goal = float(context.args[0])
        if goal <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a positive number, e.g. /goal 150")
        return
    set_goal(user_id, goal)
    await update.message.reply_text(f"🎯 Daily goal set to {goal:g}g")


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today = date.today().isoformat()
    entries = get_entries_for_date(user_id, today)
    total = get_total_for_date(user_id, today)
    goal = get_goal(user_id)

    if not entries:
        await update.message.reply_text("No entries yet today. Just send me a number to log protein!")
        return

    lines = [f"📋 Today's log ({today}):"]
    for e in entries:
        time_str = e["created_at"].strftime("%H:%M")
        note_part = f" - {e['note']}" if e["note"] else ""
        lines.append(f"  {time_str}  {e['amount']:g}g{note_part}")

    bar = progress_bar(total, goal)
    pct = min(100, round(100 * total / goal)) if goal > 0 else 0
    lines.append("")
    lines.append(f"Total: {total:g}g / {goal:g}g")
    lines.append(f"{bar} {pct}%")

    streak = calculate_streak(user_id)
    if streak > 0:
        lines.append(f"🔥 Streak: {streak} day{'s' if streak != 1 else ''}")

    await update.message.reply_text("\n".join(lines))


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    summary = build_weekly_summary_message(user_id)

    try:
        chart_buf = generate_chart(user_id, days=7)
        await update.message.reply_photo(photo=InputFile(chart_buf, filename="weekly_chart.png"))
    except Exception as e:
        print(f"Chart generation failed: {e}")

    await update.message.reply_text(summary)


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if undo_last(user_id):
        today = date.today().isoformat()
        total = get_total_for_date(user_id, today)
        await update.message.reply_text(f"↩️ Removed last entry. Today's total is now {total:g}g")
    else:
        await update.message.reply_text("Nothing to undo.")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today = date.today()
    conn = get_db()
    conn.run(
        "DELETE FROM entries WHERE user_id = :user_id AND entry_date = :entry_date",
        user_id=user_id, entry_date=today,
    )
    conn.close()
    await update.message.reply_text("🗑️ Today's entries cleared.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id, update.effective_chat.id)
    text = update.message.text.strip()
    user_id = update.effective_user.id

    match = ENTRY_PATTERN.match(text)
    has_leading_number = match and match.group(1)

    if has_leading_number:
        amount = float(match.group(1))
        note = match.group(2).strip() or None
        add_entry(user_id, amount, note)
        msg = build_progress_message(user_id, amount, note)
        await update.message.reply_text(msg)
        return

    # No leading number — try food lookup instead
    await update.message.reply_text("🔍 Looking that up...")
    protein, name = lookup_food_protein(text)

    if protein is None:
        await update.message.reply_text(
            "Couldn't find that food, or lookup isn't set up. "
            "Try sending a number instead, like `30` or `30 chicken breast`.",
            parse_mode="Markdown",
        )
        return

    add_entry(user_id, protein, text)
    msg = build_progress_message(user_id, protein, name or text)
    await update.message.reply_text(msg)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set the TELEGRAM_BOT_TOKEN environment variable")

    init_db()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("goal", set_goal_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(
        send_daily_summaries,
        time=time(hour=23, minute=0, tzinfo=SGT),
    )
    app.job_queue.run_daily(
        send_weekly_summaries,
        time=time(hour=23, minute=5, tzinfo=SGT),
        days=(0,),  # python-telegram-bot v21.x: 0=Sunday, 6=Saturday
    )

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
