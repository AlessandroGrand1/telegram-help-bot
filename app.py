#!/usr/bin/env python3
import os
import re
import sqlite3
import logging
import html
import csv
from datetime import datetime
from contextlib import closing
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()  # loads .env file in this folder

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InputTextMessageContent,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

# --------------------------
# Configuration & Logging
# --------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")  # Optional: broadcast target (e.g., a channel ID like -100123...)
DB_PATH = os.getenv("DB_PATH", "helpbot.sqlite3")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("helpbot")

URL_REGEX = re.compile(
    r"(?i)\b((?:https?://|www\.)[\w\-]+(?:\.[\w\-]+)+(?:[\w\-.,@?^=%&:/~+#]*[\w\-@?^=%&/~+#])?)"
)

# --------------------------
# Database
# --------------------------
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn, conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              url TEXT,
              title TEXT,
              description TEXT,
              tags TEXT,
              added_by INTEGER,
              added_at TEXT,
              file_id TEXT,
              file_name TEXT,
              file_type TEXT
            )
            """
        )
    log.info("DB initialized at %s", DB_PATH)


def add_item(*, url=None, title="", description="", tags="", added_by=None, file_id=None, file_name=None, file_type=None):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            INSERT INTO items (url, title, description, tags, added_by, added_at, file_id, file_name, file_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                url,
                title[:500],
                description[:2000],
                tags.strip(),
                int(added_by) if added_by is not None else None,
                datetime.utcnow().isoformat(),
                file_id,
                file_name,
                file_type,
            ),
        )
        conn.commit()
        return cur.lastrowid


def search_items_full(q, *, files_only=False, limit=25, offset=0):
    like = f"%{q.lower()}%"
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        if files_only:
            cur.execute(
                """
                SELECT id, url, title, description, tags, file_id, file_name, file_type
                FROM items
                WHERE file_id IS NOT NULL
                  AND (
                      lower(coalesce(url,'')) LIKE ?
                   OR lower(coalesce(title,'')) LIKE ?
                   OR lower(coalesce(description,'')) LIKE ?
                   OR lower(coalesce(tags,'')) LIKE ?
                   OR lower(coalesce(file_name,'')) LIKE ?
                  )
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (like, like, like, like, like, limit, offset),
            )
        else:
            cur.execute(
                """
                SELECT id, url, title, description, tags, file_id, file_name, file_type
                FROM items
                WHERE (
                      lower(coalesce(url,'')) LIKE ?
                   OR lower(coalesce(title,'')) LIKE ?
                   OR lower(coalesce(description,'')) LIKE ?
                   OR lower(coalesce(tags,'')) LIKE ?
                   OR lower(coalesce(file_name,'')) LIKE ?
                )
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (like, like, like, like, like, limit, offset),
            )
        return cur.fetchall()


def recent_items_full(limit=25):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            SELECT id, url, title, description, tags, file_id, file_name, file_type
            FROM items
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()


def get_items_by_tag(tag, limit=12, offset=0):
    like = f"%#{tag.lower()}%"
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            SELECT id, url, title, description, tags FROM items
            WHERE lower(coalesce(tags,'')) LIKE ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (like, limit, offset),
        )
        return cur.fetchall()


def delete_item(item_id):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute("DELETE FROM items WHERE id = ?", (item_id,))
        conn.commit()
        return cur.rowcount


# --------------------------
# Helpers
# --------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def extract_urls(text: str):
    if not text:
        return []
    return [m.group(1) for m in URL_REGEX.finditer(text)]


def prettify_url(url: str) -> str:
    try:
        parsed = urlparse(url if url.startswith("http") else f"http://{url}")
        host = parsed.netloc.replace("www.", "")
        return host + (parsed.path if parsed.path not in ("/", "") else "")
    except Exception:
        return url


def build_item_caption_from_row(row) -> str:
    _id, url, title, description, tags, *_rest = row
    parts = []
    if title:
        parts.append(f"<b>{html.escape(title)}</b>")
    if url:
        parts.append(f"{html.escape(prettify_url(url))}")
    if description:
        parts.append(f"{html.escape(description[:200])}…")
    if tags:
        parts.append(f"<i>{html.escape(tags)}</i>")
    parts.append(f"ID: <code>{_id}</code>")
    return "\n".join(parts)


def build_results_keyboard(items):
    buttons = []
    for _id, url, title, description, tags in items:
        label = (title or prettify_url(url or f"file #{_id}"))[:60]
        cb_data = f"open:{_id}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=cb_data)])
    return InlineKeyboardMarkup(buttons) if buttons else None


# --------------------------
# Handlers
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Open picker here", switch_inline_query_current_chat="")],
            [InlineKeyboardButton("Open picker (any chat)", switch_inline_query="")],
        ]
    )
    await update.message.reply_html(
        "Hi! I collect and organize your team's links and materials.\n\n"
        "<b>Quick use</b>\n"
        "• Paste a link and I'll save it (use #tags anywhere).\n"
        "• Send files (PDF, PPT, DOCX) — I'll index them too.\n\n"
        "<b>Search & share from any chat</b>\n"
        "• Type <code>@{username} query</code> in ANY chat to open the picker.\n"
        "• Tip: type nothing after @ to see recent items.\n\n"
        "Or tap a button below ⬇️".format(username=context.bot.username),
        reply_markup=kb,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Commands</b>\n"
        "/picker — open the inline picker in this chat\n"
        "/add &lt;url&gt; [text + #tags] — save a link\n"
        "/search &lt;query&gt; — search saved items\n"
        "/tag &lt;tag&gt; — browse by tag\n"
        "/export — (admin) export CSV\n"
        "/delete &lt;id&gt; — (admin) remove an item\n"
        "/broadcast &lt;id&gt; — (admin) repost an item to target chat\n"
        "\n"
        "<i>Inline tips:</i> type <code>@{username}</code> in any chat to open the picker; "
        "use <code>files:</code> to filter to files only, e.g. <code>files: policy</code>."
    ).format(username=context.bot.username)
    await update.message.reply_html(text)


async def picker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Open picker here", switch_inline_query_current_chat="")],
            [InlineKeyboardButton("Open picker (any chat)", switch_inline_query="")],
        ]
    )
    await update.message.reply_text("Picker:", reply_markup=kb)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add <url> optional description with #tags")
        return
    text = " ".join(context.args)
    await handle_save_content(update, context, text=text)


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args) if context.args else ""
    if not q:
        await update.message.reply_text("Usage: /search <keywords>")
        return
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT id, url, title, description, tags FROM items "
            "WHERE lower(coalesce(url,'')) LIKE ? OR lower(coalesce(title,'')) LIKE ? OR lower(coalesce(description,'')) LIKE ? OR lower(coalesce(tags,'')) LIKE ? "
            "ORDER BY id DESC LIMIT 10",
            (f"%{q.lower()}%", f"%{q.lower()}%", f"%{q.lower()}%", f"%{q.lower()}%"),
        )
        items = cur.fetchall()
    if not items:
        await update.message.reply_text("No results.")
        return
    kb = build_results_keyboard(items)
    await update.message.reply_html("<b>Results</b>:", reply_markup=kb)


async def tag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /tag <tag>")
        return
    tag = context.args[0].lstrip("#")
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT id, url, title, description, tags FROM items WHERE lower(coalesce(tags,'')) LIKE ? ORDER BY id DESC LIMIT 10",
            (f"%#{tag.lower()}%",),
        )
        items = cur.fetchall()
    if not items:
        await update.message.reply_text(f"No items found for #{tag}.")
        return
    kb = build_results_keyboard(items)
    await update.message.reply_html(f"<b>{html.escape('#'+tag)}</b>:", reply_markup=kb)


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Admins only.")
        return
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute("SELECT id, url, title, description, tags, added_by, added_at, file_id, file_name, file_type FROM items ORDER BY id ASC")
        rows = cur.fetchall()
    path = "export_items.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id","url","title","description","tags","added_by","added_at","file_id","file_name","file_type"])
        writer.writerows(rows)
    await update.message.reply_document(document=open(path, "rb"), filename="items_export.csv")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Admins only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /delete <id>")
        return
    deleted = delete_item(int(context.args[0]))
    await update.message.reply_text("Deleted." if deleted else "Not found.")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Admins only.")
        return
    if not TARGET_CHAT_ID:
        await update.message.reply_text("No TARGET_CHAT_ID configured.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /broadcast <id>")
        return
    _id = int(context.args[0])
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute("SELECT id, url, title, description, tags FROM items WHERE id = ?", (_id,))
        row = cur.fetchone()
    if not row:
        await update.message.reply_text("Item not found.")
        return
    caption = build_item_caption_from_row(row + (None, None, None))  # pad to reuse function
    await context.bot.send_message(chat_id=int(TARGET_CHAT_ID), text=caption, parse_mode="HTML")
    await update.message.reply_text("Broadcasted.")


# -------- Inline Picker (works in ANY chat via @YourBot) --------
def _make_inline_results(rows, *, prefer_files=False):
    results = []
    for (_id, url, title, description, tags, file_id, file_name, file_type) in rows:
        caption = build_item_caption_from_row((_id, url, title, description, tags))

        if file_id:
            # If it's an image -> use photo; else -> document
            if (file_type or "").startswith("image/"):
                results.append(
                    InlineQueryResultCachedPhoto(
                        id=f"photo-{_id}",
                        photo_file_id=file_id,
                        caption=caption,
                        parse_mode="HTML",
                    )
                )
            else:
                results.append(
                    InlineQueryResultCachedDocument(
                        id=f"doc-{_id}",
                        document_file_id=file_id,
                        title=title or file_name or f"File #{_id}",
                        caption=caption,
                        parse_mode="HTML",
                    )
                )
        elif url:
            results.append(
                InlineQueryResultArticle(
                    id=f"url-{_id}",
                    title=title or url,
                    description=(tags or url or "")[:120],
                    input_message_content=InputTextMessageContent(caption, parse_mode="HTML"),
                )
            )
        else:
            # Fallback: plain note as article
            results.append(
                InlineQueryResultArticle(
                    id=f"note-{_id}",
                    title=title or f"Item #{_id}",
                    description=(tags or "")[:120],
                    input_message_content=InputTextMessageContent(caption, parse_mode="HTML"),
                )
            )
    return results


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.inline_query.query or "").strip()
    files_only = False

    # Allow "files: something" to filter to files
    if q.lower().startswith("files:"):
        files_only = True
        q = q[6:].strip()

    if q:
        rows = search_items_full(q, files_only=files_only, limit=25)
    else:
        # No query typed -> show recent items (nice picker UX)
        rows = recent_items_full(limit=25)
        if files_only:
            rows = [r for r in rows if r[5]]  # keep only items with file_id

    results = _make_inline_results(rows)
    await update.inline_query.answer(results[:50], cache_time=0, is_personal=True)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data:
        return
    if q.data.startswith("open:"):
        _id = int(q.data.split(":", 1)[1])
        with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
            cur.execute("SELECT id, url, title, description, tags FROM items WHERE id = ?", (_id,))
            row = cur.fetchone()
        if not row:
            await q.edit_message_text("Item not found.")
            return
        caption = build_item_caption_from_row(row + (None, None, None))
        await q.edit_message_text(caption, parse_mode="HTML")


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle files sent to the bot (index basic metadata)."""
    user = update.effective_user
    doc = update.message.document or update.message.photo[-1] if update.message.photo else None
    if not doc:
        return
    if not is_admin(user.id):
        await update.message.reply_text("Only admins can store files.")
        return
    if update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name
        mime = update.message.document.mime_type or ""
    else:
        file_id = update.message.photo[-1].file_id
        file_name = "photo.jpg"
        mime = "image/jpeg"
    tags = " ".join([w for w in (update.message.caption or "").split() if w.startswith("#")])
    title = (update.message.caption_html or update.message.caption or file_name or "File").strip() if (update.message.caption or file_name) else "File"
    item_id = add_item(
        url=None,
        title=title,
        description="",
        tags=tags,
        added_by=user.id,
        file_id=file_id,
        file_name=file_name,
        file_type=mime,
    )
    await update.message.reply_html(f"Stored file as item <code>{item_id}</code>.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pasted links or plain notes (admins only for notes)."""
    text = update.message.text or ""
    await handle_save_content(update, context, text=text)


async def handle_save_content(update: Update, context: ContextTypes.DEFAULT_TYPE, *, text: str):
    user = update.effective_user
    urls = extract_urls(text)
    tags = " ".join([w for w in text.split() if w.startswith("#")])
    note = " ".join([w for w in text.split() if not w.startswith("#")])

    items_created = []
    if urls:
        for url in urls:
            if not url.lower().startswith(("http://", "https://")):
                url = "https://" + url
            title = note.strip() or prettify_url(url)
            item_id = add_item(url=url, title=title, description="", tags=tags, added_by=user.id)
            items_created.append(item_id)
    else:
        if not is_admin(user.id):
            await update.message.reply_text("Please include a link, or ask an admin to save notes.")
            return
        title = note.strip() or "Note"
        item_id = add_item(url=None, title=title, description="", tags=tags, added_by=user.id)
        items_created.append(item_id)

    if len(items_created) == 1:
        reply = f"Saved. ID: <code>{items_created[0]}</code>"
    else:
        reply = f"Saved {len(items_created)} items. IDs: <code>{', '.join(map(str, items_created))}</code>"
    buttons = []
    if TARGET_CHAT_ID and items_created:
        buttons.append([InlineKeyboardButton("Broadcast latest", callback_data=f"broadcast:{items_created[-1]}")])
    await update.message.reply_html(reply, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("broadcast:"):
        return
    _id = int(q.data.split(":", 1)[1])
    if not TARGET_CHAT_ID:
        await q.edit_message_text("No TARGET_CHAT_ID configured.")
        return
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute("SELECT id, url, title, description, tags FROM items WHERE id = ?", (_id,))
        row = cur.fetchone()
    if not row:
        await q.edit_message_text("Item not found.")
        return
    caption = build_item_caption_from_row(row + (None, None, None))
    await context.bot.send_message(chat_id=int(TARGET_CHAT_ID), text=caption, parse_mode="HTML")
    await q.edit_message_text("Broadcasted.")

# --------------------------
# Main
# --------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN not set. Create a .env with BOT_TOKEN=...")
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("picker", picker_cmd))
    application.add_handler(CommandHandler("add", add_cmd))
    application.add_handler(CommandHandler("search", search_cmd))
    application.add_handler(CommandHandler("tag", tag_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("delete", delete_cmd))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Inline + callbacks
    application.add_handler(InlineQueryHandler(inline_query))
    application.add_handler(CallbackQueryHandler(broadcast_callback, pattern=r"^broadcast:"))
    application.add_handler(CallbackQueryHandler(callback_router))

    # Files + text
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot is running (long-polling). Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
