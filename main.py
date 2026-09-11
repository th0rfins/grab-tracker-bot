"""Entrypoint: python main.py (polling)."""
import logging
import os

import httpx
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot import (
    cmd_help, cmd_list, cmd_start, cmd_stop, cmd_stopall, cmd_track,
    on_stop_button, on_text,
)
from config import load_config
from tracking_manager import TrackingManager

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("grab-bot")


async def _post_init(app: Application):
    # Bersihkan state lama saat restart (live_msg_id lama sudah invalid).
    # Tracking dimulai fresh saat user kirim link lagi — ini yang paling kebal.
    mgr: TrackingManager = app.bot_data["manager"]
    saved = mgr.load_state_sync()
    if saved:
        log.info("membersihkan %d state lama...", len(saved))
    try:
        from tracking_manager import STATE_FILE
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass


def main():
    cfg = load_config()
    app = Application.builder().token(cfg.bot_token).build()
    mgr = TrackingManager(cfg, app)
    app.bot_data["manager"] = mgr
    app.bot_data["http"] = httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=False, timeout=15
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("stopall", cmd_stopall))
    app.add_handler(CallbackQueryHandler(on_stop_button, pattern=r"^stop:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.post_init = _post_init
    log.info("Bot jalan (polling). Kirim link Grab untuk test.")
    app.run_polling()


if __name__ == "__main__":
    main()
