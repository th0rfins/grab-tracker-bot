"""Telegram handlers: commands + auto-detect link + stop buttons."""
import logging

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from grab_client import extract_urls, find_grab_token_in_text, resolve_share_token

log = logging.getLogger(__name__)

HELP = (
    "🍜 *Grab Live Tracker*\n\n"
    "Kirim link Grab, aku pantau posisi driver realtime via Live Location.\n\n"
    "• `https://app.grab.com/s/XXXX` (short)\n"
    "• `https://sharelocation.grab.com/o/XXXX`\n\n"
    "Commands:\n"
    "/track <link> — mulai tracking\n"
    "/list — daftar tracking aktif di chat ini\n"
    "/stop <id> — stop 1 tracking (mis. `/stop a1b2c3`)\n"
    "/stopall — stop semua di chat ini\n\n"
    "1 user bisa kirim banyak link — tiap link = Live Location + loop terpisah, "
    "ada tombol Stop masing-masing. Aman dipisah per ID."
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")


async def _begin_tracking(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str):
    mgr = ctx.application.bot_data["manager"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0

    # validasi cepat: harus mengandung grab / token
    if "grab.com" not in url.lower() and not find_grab_token_in_text(url):
        await update.message.reply_text("❌ Itu bukan link Grab. Contoh: `https://app.grab.com/s/B7bQ7vQr`", parse_mode="Markdown")
        return

    wait = await update.message.reply_text("🔎 Resolving link Grab...")
    try:
        http: httpx.AsyncClient = ctx.application.bot_data["http"]
        token = await resolve_share_token(url, http)
    except ValueError as e:
        await wait.edit_text(f"❌ {e}")
        return
    except Exception as e:
        await wait.edit_text(f"❌ Gagal resolve link: {e}")
        return

    try:
        sess, dup = await mgr.start(chat_id, user_id, token, url)
    except RuntimeError as e:
        await wait.edit_text(f"❌ {e}")
        return

    if dup:
        await wait.edit_text(
            f"ℹ️ Link ini sudah ditrack sebagai `{sess.tracking_id}` di chat ini. "
            f"Cek Live Location-nya. /list",
            parse_mode="Markdown",
        )
        return

    await wait.edit_text(
        f"✅ Tracking dimulai: `{sess.tracking_id}`\n"
        f"Live Location + status akan muncul di bawah (pesan terpisah per link).",
        parse_mode="Markdown",
    )


async def cmd_track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        await _begin_tracking(update, ctx, " ".join(ctx.args).strip())
        return
    # dukung reply ke pesan berisi link
    if update.message.reply_to_message and update.message.reply_to_message.text:
        urls = extract_urls(update.message.reply_to_message.text)
        if urls:
            await _begin_tracking(update, ctx, urls[0])
            return
    await update.message.reply_text("Kirim linknya: `/track https://app.grab.com/s/...`", parse_mode="Markdown")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Auto-detect: tiap pesan teks berisi link Grab = 1 tracking baru (terpisah)."""
    text = update.message.text or ""
    urls = extract_urls(text)
    grab_urls = [u for u in urls if "grab.com" in u.lower()]
    if not grab_urls and find_grab_token_in_text(text):
        # token telanjang? arahkan pakai format link
        return
    for u in grab_urls[:3]:  # batasi 3 per pesan biar ga spam
        await _begin_tracking(update, ctx, u)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mgr = ctx.application.bot_data["manager"]
    sessions = await mgr.list_for_chat(update.effective_chat.id)
    if not sessions:
        await update.message.reply_text("Tidak ada tracking aktif. Kirim link Grab untuk mulai.")
        return
    lines = ["🧭 *Tracking aktif:*"]
    for s in sessions:
        lines.append(f"• `{s.tracking_id}` — `{s.grab_token[:8]}...` (poll #{s.poll_count})")
    lines.append("\n/stop <id> untuk stop satuan.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mgr = ctx.application.bot_data["manager"]
    if not ctx.args:
        await update.message.reply_text("Pakai: `/stop <id>` — lihat ID via /list", parse_mode="Markdown")
        return
    tid = ctx.args[0].strip().lower()
    ok = await mgr.stop(tid, update.effective_chat.id)
    await update.message.reply_text(
        f"✅ Tracking `{tid}` dihentikan." if ok else f"❌ `{tid}` tidak ketemu di chat ini.",
        parse_mode="Markdown",
    )


async def cmd_stopall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mgr = ctx.application.bot_data["manager"]
    n = await mgr.stop_all(update.effective_chat.id)
    await update.message.reply_text(f"✅ {n} tracking dihentikan.")


async def on_stop_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = (q.data or "")
    if not data.startswith("stop:"):
        return
    tid = data.split(":", 1)[1].strip().lower()
    mgr = ctx.application.bot_data["manager"]
    ok = await mgr.stop(tid, update.effective_chat.id)
    try:
        await q.edit_message_text(
            f"{'✅' if ok else '❌'} Tracking `{tid}` {'dihentikan.' if ok else 'tidak ketemu.'}",
            parse_mode="Markdown",
        )
    except Exception:
        pass
