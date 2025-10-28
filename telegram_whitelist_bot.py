"""
Telegram Whitelist Bot — Solana only, English compact version
- Accepts only Solana addresses (base58, 32–44 chars)
- One wallet per user, editable
- Commands: !whitelist /whitelist /mywallet /editwallet /export
"""

import os
import re
import csv
import sqlite3
import logging
from datetime import datetime
from typing import Tuple

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from threading import Thread
from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok", "message": "bot alive"}

def run_webserver():
    uvicorn.run(app, host="0.0.0.0", port=8080)


DB_PATH = "whitelist.db"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_IDS = set()
if os.environ.get("ADMIN_IDS"):
    ADMIN_IDS = set(int(x.strip()) for x in os.environ.get("ADMIN_IDS").split(",") if x.strip())

ASKING_ADDRESS = 1

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Database ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS whitelist (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            wallet TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def set_wallet(tg_id: int, username: str | None, display_name: str | None, wallet: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    c.execute(
        "INSERT INTO whitelist (tg_id, username, display_name, wallet, updated_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(tg_id) DO UPDATE SET wallet=excluded.wallet, username=excluded.username, display_name=excluded.display_name, updated_at=excluded.updated_at",
        (tg_id, username, display_name, wallet, now),
    )
    conn.commit()
    conn.close()


def get_wallet(tg_id: int) -> Tuple[str | None, str | None]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT wallet, updated_at FROM whitelist WHERE tg_id = ?", (tg_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return None, None


def export_csv(path: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT tg_id, username, display_name, wallet, updated_at FROM whitelist ORDER BY updated_at DESC")
    rows = c.fetchall()
    conn.close()
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["tg_id", "username", "display_name", "wallet", "updated_at"])
        writer.writerows(rows)

# --- Solana wallet validation ---
SOLANA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

def is_valid_wallet(addr: str) -> bool:
    addr = addr.strip()
    return bool(SOLANA_RE.fullmatch(addr))

# --- Bot handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! Type !whitelist or /whitelist to add your Solana wallet (1 per user, editable).\nUse /mywallet to check or /editwallet to update."
    )


async def whitelist_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    current, when = get_wallet(user.id)
    if current:
        await update.message.reply_text(f"You already have a wallet: `{current}`. Use /editwallet to change it.")
        return ConversationHandler.END

    await update.message.reply_text("Send your Solana wallet address (base58, 32–44 chars):")
    return ASKING_ADDRESS


async def receive_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    if not is_valid_wallet(text):
        await update.message.reply_text("❌ Invalid Solana address. Must be 32–44 base58 characters. Try again or /cancel.")
        return ASKING_ADDRESS

    set_wallet(user.id, user.username, user.full_name, text)
    await update.message.reply_text("✅ Added to whitelist!")
    return ConversationHandler.END


async def editwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    current, _ = get_wallet(user.id)
    await update.message.reply_text(f"Current: `{current}`\nSend new Solana address or /cancel.")
    return ASKING_ADDRESS


async def mywallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    current, _ = get_wallet(user.id)
    if current:
        await update.message.reply_text(f"Your wallet: `{current}`")
    else:
        await update.message.reply_text("No wallet on record. Type !whitelist to add one.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("Not authorized.")
        return
    path = "whitelist_export.csv"
    export_csv(path)
    await update.message.reply_document(open(path, "rb"))


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip() if update.message and update.message.text else ""
    if txt.lower() == "!whitelist":
        return await whitelist_entry(update, context)


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Set TELEGRAM_TOKEN environment variable.")

    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler(["whitelist"], whitelist_entry),
            MessageHandler(filters.TEXT & filters.Regex(r"(?i)^!whitelist$"), whitelist_entry),
        ],
        states={
            ASKING_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_address)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("editwallet", editwallet))
    app.add_handler(CommandHandler("mywallet", mywallet))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    logger.info("Solana whitelist bot running...")
    Thread(target=run_webserver, daemon=True).start()

    app.run_polling()


if __name__ == "__main__":
    main()