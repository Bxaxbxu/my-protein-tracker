import os
import re
from datetime import datetime, date, time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pg8000.native
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

DATABASE_URL = os.environ.get("DATABASE_URL")
DEFAULT_GOAL = int(os.environ.get("DEFAULT_GOAL_G", "150"))
SGT = ZoneInfo("Asia/Singapore")


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


def add_entry(user_id: int, amount: float, note: str | None):
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


ENTRY_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*g?\s*(.*)$", re.IGNORECASE)


def progress_bar(total: float, goal: float, length: int = 10) -> str:
    if goal <= 0:
        return ""
    filled = min(length, round(length * total / goal))
    return "🟩" * filled + "⬜" * (length - filled)


def build_progress_message(user_id: int, amount_added: float, note: str | None) -> str:
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
    return "\n".join(lines)


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
    return "\n".join(lines)


async def send_daily_summaries(context: ContextTypes.DEFAULT_TYPE):
    for user_id, chat_id in get_all_user_chat_ids():
        try:
            msg = build_daily_summary_message(user_id)
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"Failed to send summary to user {user_id}: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(
        "👋 Protein Tracker Bot\n\n"
        "Just send me how much protein you ate:\n"
        "  • `30`\n"
        "  • `30 chicken breast`\n\n"
        "Commands:\n"
        "/today - show today's log and total\n"
        "/goal 150 - set your daily protein goal (g)\n"
        "/undo - remove your last entry\n"
        "/reset - clear today's entries\n\n"
        "I'll also send you a daily summary at 11pm SGT.",
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

    await update.message.reply_text("\n".join(lines))


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
    match = ENTRY_PATTERN.match(text)

    if not match:
        await update.message.reply_text(
            "I didn't understand that. Send a
