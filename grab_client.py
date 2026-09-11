"""Grab share-link resolver + booking API client."""
import re
from urllib.parse import urlparse, parse_qs

import httpx

SHORT_RE = re.compile(r"app\.grab\.com/s/([A-Za-z0-9]+)", re.IGNORECASE)
SHARE_RE = re.compile(r"sharelocation\.grab\.com/o/([A-Za-z0-9]+)", re.IGNORECASE)
TOKEN_RE = re.compile(r"\b[A-Za-z0-9]{15,}\b")

API_BASE = "https://api.grab.com"
UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"
)

# short_code -> share token. Diisi saat resolve sukses; dibaca sebelum network.
_SHORT_CACHE: dict[str, str] = {}


def extract_urls(text: str) -> list[str]:
    """Ambil semua URL dari teks (entity-independent, biar kebal)."""
    if not text:
        return []
    # tambah skema jika user kirim tanpa https? Grab selalu ada skema, skip.
    pat = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
    urls = pat.findall(text)
    # strip trailing punctuation umum
    return [u.rstrip(").,!'\"") for u in urls]


def find_grab_token_in_text(text: str) -> str | None:
    """Best-effort: cari shareOrderLink / /o/ token langsung dari teks mentah."""
    if not text:
        return None
    m = SHARE_RE.search(text)
    if m:
        return m.group(1)
    # ?shareOrderLink=xxx
    try:
        for u in extract_urls(text):
            q = parse_qs(urlparse(u).query)
            if "shareOrderLink" in q and q["shareOrderLink"]:
                return q["shareOrderLink"][0]
    except Exception:
        pass
    return None


async def resolve_share_token(url: str, client: httpx.AsyncClient) -> str:
    """Resolve app.grab.com/s/xxx -> sharelocation token (klb5...).

    Raises ValueError jika bukan link Grab valid.
    """
    url = url.strip()
    # 1. Kalau URL sudah mengandung token /o/, langsung pakai (tanpa network).
    direct = find_grab_token_in_text(url)
    if direct:
        return direct

    m = SHORT_RE.search(url)
    if not m:
        raise ValueError("Bukan link Grab. Kirim link app.grab.com/s/...")

    short_url = f"https://app.grab.com/s/{m.group(1)}"
    short_code = m.group(1)
    # Cache bawaan: short_code -> token (penting karena mapping edge bisa
    # expired setelah order selesai; resolve ulang tanpa cache akan gagal).
    cached = _SHORT_CACHE.get(short_code)
    if cached:
        return cached

    async def _try(req):
        try:
            r = await req
            loc = r.headers.get("location", "")
            tok = find_grab_token_in_text(loc)
            if tok:
                return tok
            final = str(getattr(r, "url", ""))
            tok = find_grab_token_in_text(final)
            if tok:
                return tok
            try:
                body = r.text[:6000] if hasattr(r, "text") else ""
            except Exception:
                body = ""
            return find_grab_token_in_text(body)
        except Exception:
            return None

    # 1) GET tanpa follow (baca Location) — cara tercepat saat mapping masih hidup.
    tok = await _try(client.get(short_url, follow_redirects=False))
    # 2) GET dengan follow (final URL bisa /open?...shareOrderLink=...)
    if not tok:
        tok = await _try(client.get(short_url, follow_redirects=True))
    # 3) HEAD tanpa follow (kadang edge hanya jawab 302 untuk HEAD)
    if not tok:
        try:
            tok = await _try(client.head(short_url, follow_redirects=False))
        except Exception:
            tok = None
    if tok:
        _SHORT_CACHE[short_code] = tok
        return tok

    raise ValueError(
        "Gagal resolve short link (kemungkinan order sudah selesai/expired, "
        "sehingga mapping app.grab.com/s/... sudah dihapus Grab). "
        "Kalau order masih jalan, buka link di browser lalu kirim URL "
        "sharelocation.grab.com/o/...-nya ke sini."
    )


async def fetch_booking(token: str, client: httpx.AsyncClient, timeout: float) -> dict:
    """GET /api/v1/safety/sharemyride/<token>/bookingdetails.

    Return dict JSON. Raise GrabGone (404/kosong) jika sesi habis,
    Raise RuntimeError untuk error sementara (bisa retry).
    """
    api = f"{API_BASE}/api/v1/safety/sharemyride/{token}/bookingdetails"
    try:
        r = await client.get(
            api,
            params={"fullData": "true"},
            headers={
                "Accept": "application/json",
                "Origin": "https://sharelocation.grab.com",
                "Referer": f"https://sharelocation.grab.com/o/{token}",
                "User-Agent": UA,
            },
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        raise RuntimeError(f"network: {e}") from e

    if r.status_code == 404:
        raise GrabGone("Order tidak ditemukan / link expired (404).")
    if r.status_code != 200:
        raise RuntimeError(f"Grab API {r.status_code}")

    try:
        data = r.json()
    except Exception as e:
        raise RuntimeError("respon Grab bukan JSON") from e

    if not data or not data.get("pass"):
        raise GrabGone("Sesi tracking sudah berakhir / link invalid.")
    return data


class GrabGone(Exception):
    """Sesi terminal: stop tracking, jangan retry."""


TERMINAL_SUBSTR = ("COMPLETE", "CANCEL", "DELIVER", "EXPIRED", "TERMINAT")


def is_terminal(data: dict) -> tuple[bool, str]:
    """Cek bookingState/sessionStatus/completeFlag. Return (terminal, reason)."""
    sess = str(data.get("sessionStatus", "")).upper()
    if sess and sess != "ACTIVE":
        return True, f"session {sess}"
    booking = data.get("booking") or {}
    state = str(booking.get("bookingState", "")).upper()
    if any(s in state for s in TERMINAL_SUBSTR):
        return True, f"state {state}"
    add = data.get("additionalOrderDetail") or {}
    try:
        if int(add.get("completeFlag", 0)) == 1:
            return True, "completeFlag=1"
    except Exception:
        pass
    return False, ""


def summarize(data: dict) -> dict:
    """Ambil field penting untuk display + live-loc."""
    driver = data.get("driver") or {}
    loc = driver.get("location") or {}
    booking = data.get("booking") or {}
    pickup = booking.get("pickup") or {}
    drop = booking.get("dropOff") or {}
    msg = data.get("messageStatus") or {}
    proc = msg.get("processMsg") or {}
    route = data.get("route") or {}
    return {
        "lat": loc.get("latitude"),
        "lon": loc.get("longitude"),
        "driver": driver.get("name", "-"),
        "vehicle": f"{driver.get('vehicleModel','-')} {driver.get('vehiclePlateNumber','')}".strip(),
        "state": booking.get("bookingState", "-"),
        "code": booking.get("bookingCode", "-"),
        "pickup": pickup.get("keywords", "-"),
        "dropoff": drop.get("keywords", "-"),
        "title": msg.get("title", "-"),
        "subtitle": proc.get("message", "-"),
        "etas": route.get("ETAs"),
        "session": data.get("sessionStatus", "-"),
    }
