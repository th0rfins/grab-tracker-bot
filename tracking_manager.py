"""Session manager: 1 link = 1 session = 1 live-location message + 1 polling task.

Isolasi: tiap session punya asyncio.Task + asyncio.Lock + stop_event sendiri.
Stop/crash di satu session TIDAK menyentuh session lain (try/except per-loop).
"""
import asyncio
import json
import logging
import os
import secrets
import time

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from config import Config
from geo import bearing_deg, haversine_m
from grab_client import GrabGone, fetch_booking, is_terminal, summarize

log = logging.getLogger(__name__)
STATE_FILE = os.getenv("STATE_FILE", "state.json")


class TrackingSession:
    __slots__ = (
        "tracking_id", "chat_id", "user_id", "grab_token", "origin_url",
        "live_msg_id", "status_msg_id", "task", "lock", "stop_event",
        "last_lat", "last_lon", "last_heading", "last_state",
        "last_status_text", "last_status_edit", "last_live_edit",
        "live_expires_at", "errors", "created_at", "poll_count",
    )

    def __init__(self, tracking_id, chat_id, user_id, grab_token, origin_url):
        self.tracking_id = tracking_id
        self.chat_id = chat_id
        self.user_id = user_id
        self.grab_token = grab_token
        self.origin_url = origin_url
        self.live_msg_id = None
        self.status_msg_id = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.last_lat = None
        self.last_lon = None
        self.last_heading = None
        self.last_state = None
        self.last_status_text = ""
        self.last_status_edit = 0.0
        self.last_live_edit = 0.0
        self.live_expires_at = 0.0
        self.errors = 0
        self.created_at = time.time()
        self.poll_count = 0


class TrackingManager:
    def __init__(self, config: Config, application):
        self.cfg = config
        self.app = application
        self._sessions: dict[str, TrackingSession] = {}  # tracking_id -> session
        self._lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=False,
            )
        return self._http

    # ---------- public API ----------

    async def start(self, chat_id: int, user_id: int, grab_token: str, origin_url: str):
        """Buat session baru. Return (session, is_duplicate)."""
        async with self._lock:
            # Dedupe: token sama di chat sama -> kembalikan yg ada
            for s in self._sessions.values():
                if s.chat_id == chat_id and s.grab_token == grab_token:
                    return s, True
            mine = [s for s in self._sessions.values() if s.chat_id == chat_id]
            if len(mine) >= self.cfg.max_trackings_per_chat:
                raise RuntimeError(
                    f"Batas {self.cfg.max_trackings_per_chat} tracking aktif per chat. "
                    f"Stop salah satu dulu (/list)."
                )
            tid = secrets.token_hex(3)  # 6 hex char, unik global
            while tid in self._sessions:
                tid = secrets.token_hex(3)
            sess = TrackingSession(tid, chat_id, user_id, grab_token, origin_url)
            self._sessions[tid] = sess
            sess.task = asyncio.create_task(self._poll_loop(sess), name=f"grab-{tid}")
            self._save_state()
            return sess, False

    async def stop(self, tracking_id: str, request_chat_id: int) -> bool:
        """Stop 1 tracking. Return True jika ketemu & milik chat tsb."""
        async with self._lock:
            sess = self._sessions.get(tracking_id)
            if not sess or sess.chat_id != request_chat_id:
                return False
            # hapus dari dict DULU biar /list langsung konsisten
            del self._sessions[tracking_id]
            self._save_state()
        sess.stop_event.set()
        if sess.task:
            sess.task.cancel()
        # best-effort: matikan live-loc di Telegram (jangan gagalkan stop)
        try:
            if sess.live_msg_id:
                async with sess.lock:
                    await self.app.bot.stop_message_live_location(
                        chat_id=sess.chat_id, message_id=sess.live_msg_id
                    )
        except Exception as e:
            log.debug("stop live-loc %s: %s", tracking_id, e)
        return True

    async def stop_all(self, chat_id: int) -> int:
        async with self._lock:
            tids = [tid for tid, s in self._sessions.items() if s.chat_id == chat_id]
            sessions = [self._sessions.pop(tid) for tid in tids]
            self._save_state()
        for sess in sessions:
            sess.stop_event.set()
            if sess.task:
                sess.task.cancel()
            try:
                if sess.live_msg_id:
                    await self.app.bot.stop_message_live_location(
                        chat_id=sess.chat_id, message_id=sess.live_msg_id
                    )
            except Exception:
                pass
        return len(sessions)

    async def list_for_chat(self, chat_id: int) -> list[TrackingSession]:
        async with self._lock:
            return [s for s in self._sessions.values() if s.chat_id == chat_id]

    async def get(self, tracking_id: str) -> TrackingSession | None:
        async with self._lock:
            return self._sessions.get(tracking_id)

    async def shutdown(self):
        async with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            s.stop_event.set()
            if s.task:
                s.task.cancel()
        if sessions:
            await asyncio.gather(*[s.task for s in sessions if s.task],
                                 return_exceptions=True)
        if self._http:
            await self._http.aclose()

    # ---------- persistence (resume after restart) ----------
    def _save_state(self):
        try:
            data = [
                {"tracking_id": s.tracking_id, "chat_id": s.chat_id,
                 "user_id": s.user_id, "grab_token": s.grab_token,
                 "origin_url": s.origin_url}
                for s in self._sessions.values()
            ]
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log.debug("save state: %s", e)

    def load_state_sync(self) -> list[dict]:
        try:
            if not os.path.exists(STATE_FILE):
                return []
            with open(STATE_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    # ---------- internal loop (satu task per tracking) ----------
    async def _poll_loop(self, sess: TrackingSession):
        tid = sess.tracking_id
        log.info("poll start %s chat=%s token=%s...", tid, sess.chat_id, sess.grab_token[:6])
        backoff = 5.0
        try:
            http = await self._http_client()
            while not sess.stop_event.is_set():
                # --- fetch (isolated try) ---
                try:
                    data = await fetch_booking(sess.grab_token, http, self.cfg.request_timeout)
                except GrabGone as e:
                    await self._finish(sess, f"⛔ Tracking `{tid}` berhenti: {e}")
                    return
                except RetryAfter as e:  # pragma: no cover - defensif
                    await asyncio.sleep(getattr(e, "retry_after", 5) + 1)
                    continue
                except Exception as e:
                    sess.errors += 1
                    log.warning("fetch %s err %d: %s", tid, sess.errors, e)
                    if sess.errors >= 30:
                        await self._finish(sess, f"⛔ Tracking `{tid}` dihentikan: error beruntun ({e}). Kirim link lagi untuk mulai baru.")
                        return
                    await asyncio.sleep(min(backoff, 60))
                    backoff = min(backoff * 1.5, 60)
                    continue

                sess.errors = 0
                backoff = 5.0
                sess.poll_count += 1

                terminal, reason = is_terminal(data)
                if terminal:
                    await self._finish(sess, f"✅ Tracking `{tid}` selesai ({reason}).")
                    return

                s = summarize(data)
                lat, lon = s["lat"], s["lon"]
                if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                    # driver belum share lokasi / belum dapat driver — update status aja
                    await self._maybe_update_status(sess, s, note="📍 Lokasi driver belum tersedia.")
                    await asyncio.sleep(self.cfg.poll_interval)
                    continue

                async with sess.lock:
                    try:
                        await self._ensure_messages(sess, s, lat, lon)
                    except Forbidden:
                        # user block bot -> hentikan diam-diam
                        await self._remove_only(sess)
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("telegram update %s: %s", tid, e)

                await asyncio.sleep(self.cfg.poll_interval)
        except asyncio.CancelledError:
            log.info("poll cancel %s", tid)
            raise
        except Exception as e:  # last-resort guard: 1 loop crash ≠ semua crash
            log.exception("poll crash %s: %s", tid, e)
            try:
                await self._finish(sess, f"⛔ Tracking `{tid}` error internal, dihentikan.")
            except Exception:
                pass

    # ---------- telegram ops (dipanggil dengan sess.lock dipegang) ----------
    def _status_text(self, sess: TrackingSession, s: dict, note: str = "") -> str:
        etas = s.get("etas")
        eta_txt = f"{etas}" if etas else "-"
        lines = [
            f"🧭 *Tracking `{sess.tracking_id}`* — `{s['state']}`",
            f"🚗 {s['driver']} • {s['vehicle']}",
            f"🍜 {s['pickup']}",
            f"📦 {s['dropoff']}",
            f"ℹ️ {s['title']} — {s['subtitle']}",
            f"⏱️ ETA: {eta_txt} • update #{sess.poll_count}",
            f"🔗 `{sess.origin_url[:60]}`",
        ]
        if note:
            lines.append(note)
        lines.append(f"\n/stop `{sess.tracking_id}` untuk berhenti.")
        return "\n".join(lines)[:3800]

    def _stop_kb(self, sess: TrackingSession) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(f"⏹ Stop {sess.tracking_id}", callback_data=f"stop:{sess.tracking_id}")
        ]])

    async def _ensure_messages(self, sess: TrackingSession, s: dict, lat: float, lon: float):
        bot = self.app.bot
        now = time.time()

        # 1) live-location message: kirim pertama kali / renew jika mau expired / kirim ulang jika hilang
        need_new_live = (
            sess.live_msg_id is None
            or now > sess.live_expires_at - 300
        )
        if need_new_live:
            heading = None
            if sess.last_lat is not None:
                try:
                    heading = bearing_deg(sess.last_lat, sess.last_lon, lat, lon)
                except Exception:
                    heading = None
            try:
                m = await bot.send_location(
                    chat_id=sess.chat_id, latitude=lat, longitude=lon,
                    live_period=self.cfg.live_period,
                    heading=heading,
                    horizontal_accuracy=20,
                )
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                m = await bot.send_location(
                    chat_id=sess.chat_id, latitude=lat, longitude=lon,
                    live_period=self.cfg.live_period,
                )
            sess.live_msg_id = m.message_id
            sess.live_expires_at = now + self.cfg.live_period
            sess.last_live_edit = now
            sess.last_lat, sess.last_lon = lat, lon
            if heading is not None:
                sess.last_heading = heading
        else:
            # update posisi jika bergerak cukup jauh / heading berubah / max 30 dtk sekali
            moved = haversine_m(sess.last_lat, sess.last_lon, lat, lon) if sess.last_lat else 1e9
            try:
                heading = bearing_deg(sess.last_lat, sess.last_lon, lat, lon) if sess.last_lat else None
            except Exception:
                heading = None
            stale = (now - sess.last_live_edit) > 30
            heading_changed = (
                heading is not None and sess.last_heading is not None
                and abs(heading - sess.last_heading) > 25 and moved > 5
            )
            if moved >= self.cfg.min_move_meters or stale or heading_changed or sess.last_state != s["state"]:
                try:
                    await bot.edit_message_live_location(
                        chat_id=sess.chat_id, message_id=sess.live_msg_id,
                        latitude=lat, longitude=lon,
                        heading=heading if heading is not None else sess.last_heading,
                        horizontal_accuracy=20,
                    )
                    sess.last_live_edit = now
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except BadRequest as e:
                    msg = str(e).lower()
                    if "live" in msg or "not found" in msg or "can't be edited" in msg or "message to edit" in msg:
                        sess.live_msg_id = None  # paksa kirim baru di iterasi berikut
                        sess.live_expires_at = 0
                    else:
                        raise
                else:
                    sess.last_lat, sess.last_lon = lat, lon
                    if heading is not None:
                        sess.last_heading = heading

        # 2) status message: kirim sekali, edit hanya jika berubah / max 60 dtk
        text = self._status_text(sess, s)
        if sess.status_msg_id is None:
            m = await bot.send_message(
                chat_id=sess.chat_id, text=text, parse_mode="Markdown",
                reply_markup=self._stop_kb(sess), disable_web_page_preview=True,
            )
            sess.status_msg_id = m.message_id
            sess.last_status_text = text
            sess.last_status_edit = now
        elif text != sess.last_status_text and (text_changed_significant(sess.last_status_text, text) or (now - sess.last_status_edit) > 60):
            try:
                await bot.edit_message_text(
                    chat_id=sess.chat_id, message_id=sess.status_msg_id,
                    text=text, parse_mode="Markdown",
                    reply_markup=self._stop_kb(sess), disable_web_page_preview=True,
                )
                sess.last_status_text = text
                sess.last_status_edit = now
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    pass
                elif "not found" in str(e).lower():
                    sess.status_msg_id = None
                else:
                    raise
        sess.last_state = s["state"]

    async def _maybe_update_status(self, sess: TrackingSession, s: dict, note: str):
        async with sess.lock:
            if sess.status_msg_id is None:
                text = self._status_text(sess, s, note)
                try:
                    m = await self.app.bot.send_message(
                        chat_id=sess.chat_id, text=text, parse_mode="Markdown",
                        reply_markup=self._stop_kb(sess), disable_web_page_preview=True,
                    )
                    sess.status_msg_id = m.message_id
                    sess.last_status_text = text
                    sess.last_status_edit = time.time()
                except Exception as e:
                    log.debug("status send %s: %s", sess.tracking_id, e)
            else:
                text = self._status_text(sess, s, note)
                if text != sess.last_status_text and (time.time() - sess.last_status_edit) > 60:
                    try:
                        await self.app.bot.edit_message_text(
                            chat_id=sess.chat_id, message_id=sess.status_msg_id,
                            text=text, parse_mode="Markdown",
                            reply_markup=self._stop_kb(sess), disable_web_page_preview=True,
                        )
                        sess.last_status_text = text
                        sess.last_status_edit = time.time()
                    except TelegramError as e:
                        log.debug("status edit %s: %s", sess.tracking_id, e)
            sess.last_state = s["state"]

    async def _finish(self, sess: TrackingSession, text: str):
        """Hentikan 1 session + stop live-loc + notifikasi. Terisolasi."""
        async with self._lock:
            self._sessions.pop(sess.tracking_id, None)
            self._save_state()
        sess.stop_event.set()
        async with sess.lock:
            try:
                if sess.live_msg_id:
                    await self.app.bot.stop_message_live_location(
                        chat_id=sess.chat_id, message_id=sess.live_msg_id
                    )
            except Exception:
                pass
            try:
                await self.app.bot.send_message(chat_id=sess.chat_id, text=text, parse_mode="Markdown")
            except Exception:
                pass

    async def _remove_only(self, sess: TrackingSession):
        async with self._lock:
            self._sessions.pop(sess.tracking_id, None)
            self._save_state()
        sess.stop_event.set()


def text_changed_significant(old: str, new: str) -> bool:
    """Cegah spam edit: anggap signifikan jika baris state/info berubah."""
    if not old:
        return True
    oo = old.splitlines()[:5]
    nn = new.splitlines()[:5]
    return oo != nn
