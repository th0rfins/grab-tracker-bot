# Grab Live Tracker Bot 🍜📍

Telegram bot untuk tracking order GrabFood secara realtime memakai
**Telegram Live Location**. Kirim link share Grab, bot mem-poll API Grab
tiap ±10 detik dan meng-update posisi driver langsung di peta chat.

## Cara pakai

Kirim salah satu format link ke bot:

- `https://app.grab.com/s/XXXX` (short link dari aplikasi Grab)
- `https://sharelocation.grab.com/o/XXXX` (link tracking langsung)

Commands:

| Command | Fungsi |
|---|---|
| `/track <link>` | Mulai tracking (atau cukup kirim link langsung) |
| `/list` | Daftar tracking aktif di chat ini |
| `/stop <id>` | Stop 1 tracking, mis. `/stop a1b2c3` |
| `/stopall` | Stop semua tracking di chat ini |
| `/help` | Bantuan |

Tiap link = 1 Live Location message + 1 status message + tombol Stop sendiri.
Satu user/chat bisa tracking banyak link sekaligus, saling terisolasi
(ID unik, polling loop dan lock per-tracking).

Status order yang ditampilkan: nama driver, kendaraan, resto → tujuan,
status dapur (`ORDER_IN_PREPARE` dll), dan ETA. Tracking berhenti otomatis
saat order selesai/cancel/expired.

> Catatan: short link `app.grab.com/s/...` bisa expired setelah order
> selesai. Kalau bot bilang gagal resolve padahal order masih jalan, buka
> link di browser lalu kirim URL `sharelocation.grab.com/o/...`-nya.

## Jalan lokal

```bash
pip install -r requirements.txt
cp .env.example .env   # isi BOT_TOKEN dari @BotFather
python main.py
```

Test offline (tanpa token & tanpa order hidup):

```bash
python test_offline.py
```

## Env vars

| Var | Default | Keterangan |
|---|---|---|
| `BOT_TOKEN` | wajib | Token dari @BotFather |
| `POLL_INTERVAL` | `10` | Detik antar poll API Grab |
| `MAX_TRACKINGS_PER_CHAT` | `5` | Batas tracking aktif per chat |
| `LIVE_PERIOD` | `86400` | Masa live location Telegram (detik, max 86400) |
| `MIN_MOVE_METERS` | `15` | Gerak minimum sebelum posisi di-update |

## Deploy ke Railway

Bot ini polling murni (tanpa port HTTP), jadi deploy sebagai **worker**:

1. Push repo ini ke GitHub.
2. Di Railway: New Project → Deploy from GitHub → pilih repo.
3. Tambah variable `BOT_TOKEN` (dan opsional lainnya) di tab Variables.
4. Start command sudah dikonfigurasi (`python main.py` via `railway.json`
   + `Procfile`). Tidak perlu setting port/healthcheck.
5. Deploy. Log Railway akan menampilkan `Bot jalan (polling)`.

Alternatif: deploy image dari `Dockerfile` yang tersedia.

## Struktur

- `main.py` — entrypoint polling
- `bot.py` — handlers Telegram
- `tracking_manager.py` — session per-link (isolasi multi-tracking)
- `grab_client.py` — resolve short link + API Grab
- `geo.py` — haversine & bearing
- `config.py` — konfigurasi via env
