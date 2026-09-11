"""Offline test: tidak butuh BOT_TOKEN / order hidup."""
import asyncio

import httpx

from geo import bearing_deg, haversine_m
from grab_client import (
    _SHORT_CACHE,
    extract_urls,
    find_grab_token_in_text,
    is_terminal,
    resolve_share_token,
    summarize,
)

SAMPLE_ACTIVE = {
    "pass": True, "token": "klb5oetmOvH6ZHzWOMdE", "sessionStatus": "ACTIVE",
    "driver": {"name": "Anggi Maylan", "location": {"latitude": -7.70288, "longitude": 109.020845},
               "vehicleModel": "Honda Vario 125", "vehiclePlateNumber": "R 5974 IP"},
    "booking": {"bookingCode": "A-9QPPCSCGXX3DAV", "bookingState": "ORDER_IN_PREPARE",
                "pickup": {"keywords": "Warung Soto Ambyar - Sidanegara"},
                "dropOff": {"keywords": "8 Jl. Tegalkamulyan"}},
    "route": {"ETAs": [251, 408]},
    "messageStatus": {"title": "Kitchen's preparing your order",
                      "processMsg": {"message": "We'll let you know when it's out for delivery."}},
    "additionalOrderDetail": {"isPendingDelivery": False, "completeFlag": 0},
}
SAMPLE_DONE = {"pass": True, "sessionStatus": "TERMINATED_MANUAL",
               "booking": {"bookingState": "COMPLETED"},
               "additionalOrderDetail": {"completeFlag": 1}}


async def main():
    # 1. extract direct token (tanpa network)
    url = "https://sharelocation.grab.com/o/klb5oetmOvH6ZHzWOMdE"
    assert find_grab_token_in_text(url) == "klb5oetmOvH6ZHzWOMdE"
    async with httpx.AsyncClient(timeout=5) as c:
        tok = await resolve_share_token(url, c)
        assert tok == "klb5oetmOvH6ZHzWOMdE", tok
    print("OK direct token (no network)")

    # 2. short link -> cache hit (tanpa network, simulasi mapping lama)
    _SHORT_CACHE["B7bQ7vQr"] = "klb5oetmOvH6ZHzWOMdE"
    async with httpx.AsyncClient(timeout=5) as c:
        tok = await resolve_share_token("https://app.grab.com/s/B7bQ7vQr", c)
        assert tok == "klb5oetmOvH6ZHzWOMdE", tok
    print("OK short cache")

    # 3. extract_urls multi-link (1 pesan 2 link = 2 tracking terpisah)
    urls = extract_urls("a https://app.grab.com/s/AAA b https://sharelocation.grab.com/o/BBB123456789012")
    assert len(urls) == 2, urls
    print("OK multi-url:", urls)

    # 4. summarize + terminal
    s = summarize(SAMPLE_ACTIVE)
    assert s["lat"] == -7.70288 and s["lon"] == 109.020845, s
    assert s["state"] == "ORDER_IN_PREPARE", s
    assert is_terminal(SAMPLE_ACTIVE) == (False, ""), is_terminal(SAMPLE_ACTIVE)
    term, reason = is_terminal(SAMPLE_DONE)
    assert term, reason
    print("OK summarize/terminal:", s["driver"], s["state"], "->", reason)

    # 5. geo
    d = haversine_m(-7.70288, 109.020845, -7.709643, 109.01494)
    assert 500 < d < 2000, d
    h = bearing_deg(-7.70288, 109.020845, -7.709643, 109.01494)
    assert 0 <= h <= 360, h
    print(f"OK geo: resto {d:.0f}m bearing {h}")

    print("ALL OFFLINE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
