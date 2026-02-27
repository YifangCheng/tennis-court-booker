#!/usr/bin/env python3
"""
Tennis Court Booker — Raynes Park
Automatically books an indoor court at the configured time on the first day
that becomes bookable at tonight's midnight (i.e. today + 14 days).

Usage:
  python booker.py                                          # production — waits for midnight, then books & pays
  python booker.py --debug --now --date YYYY-MM-DD --time HH:MM   # dry-run — screenshots, no payment
  python booker.py --debug --now --pay --date YYYY-MM-DD --time HH:MM  # book now — screenshots + real payment
  python booker.py --now                                    # production logic but skip midnight wait
"""

import argparse
import asyncio
import json as _json
import logging
import ntplib
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path
import pytz
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

# ---------------------------------------------------------------------------
# Bootstrap — load .env relative to this file so LaunchAgent can find it
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
load_dotenv(_HERE / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        # No FileHandler — LaunchAgent already redirects stdout → booker.log.
        # Having both caused every line to appear twice.
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

import json

with open(_HERE / "config.json") as _f:
    _cfg = json.load(_f)

TZ_NAME          = _cfg.get("timezone",          "Europe/London")
BOOKING_DATE     = _cfg.get("booking_date",       None)   # null = auto-calculate from midnight
BOOKING_TIME     = _cfg.get("booking_time",       "17:00")
PREFERRED_COURTS = _cfg.get("preferred_courts",   [4, 5, 6, 7, 8, 9])
COURT_TYPE       = _cfg.get("court_type",         "indoor")
HEADLESS         = _cfg.get("headless",           True)
PRE_LOGIN_SECS   = _cfg.get("pre_login_seconds",  120)

# Resource IDs from the venue API (GetVenueSessions)
COURT_RESOURCE_IDS = {
    ("indoor", 4): "4198fda4-6886-4e97-9355-2d808c37873e",
    ("indoor", 5): "749e1d69-d2f6-48ac-a571-0f5b6ba820b6",
    ("indoor", 6): "9154426c-24e4-4117-921d-ca0032a80383",
    ("indoor", 7): "fad29dbc-537e-4a6b-8509-d4b1c842ba54",
    ("indoor", 8): "eb2465b3-a53c-4163-9b97-ea98ec4d216d",
    ("indoor", 9): "d4913519-e446-4c2a-9407-90ac73ef43cf",
    ("outdoor", 1): "c283db85-53e9-43ce-9375-30cf7b6d30de",
    ("outdoor", 2): "da22e6ea-be6e-4bfc-8e9a-9b5c642b33bc",
    ("outdoor", 3): "aa6abe54-7550-4b19-9f20-d998bd418358",
    ("grass", 1): "f89c880c-2872-4bf3-a9ba-ad90bd9240e4",
    ("grass", 2): "5b348cbc-c6b4-4b51-b4e7-3d5be51d6ebd",
    ("grass", 3): "d9dd1e93-19a5-4463-b0ac-4ffb84849098",
    ("grass", 4): "fe653dda-88ed-4ba2-b8b8-94ef1c4e4fea",
    ("grass", 5): "165d3984-87e6-4a59-a086-e3cc1c45eab3",
    ("grass", 6): "10c84f5d-3f12-418e-859a-cdec21e80c78",
}

USERNAME    = os.environ.get("BOOKING_USERNAME", "")
PASSWORD    = os.environ.get("BOOKING_PASSWORD", "")
CARD_NUMBER = os.environ.get("CARD_NUMBER", "")
CARD_EXPIRY = os.environ.get("CARD_EXPIRY", "")   # format: MM/YY
CARD_CVV    = os.environ.get("CARD_CVV",    "")
CARD_NAME   = os.environ.get("CARD_NAME",   "")

BASE_URL    = "https://raynespark.communitysport.aeltc.com"
AUTH_BASE   = "https://auth.communitysport.aeltc.com"
BOOKING_URL = f"{BASE_URL}/Booking/BookByDate"
VENUE_ID    = "a750357b-8670-4b34-a7e8-9c4660577b29"  # Raynes Park
VENUE_SLUG  = "raynespark_communitysport_aeltc_com"


def build_login_url() -> str:
    """
    Build the WS-Federation login URL with the current UTC timestamp.

    The site uses WS-Federation which embeds a creation-time (wct) parameter
    to prevent replay attacks. It appears in two places with different encoding:
      - outer wct:             colons → %3a   (single-encoded)
      - wct inside ReturnUrl:  colons → %253a (double-encoded, because the
                               inner %3a gets %-encoded again for the outer URL)
    """
    now     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_out  = now.replace(":", "%3a")    # outer wct  e.g. 2026-02-27T00%3a00%3a00Z
    ts_in   = now.replace(":", "%253a")  # inner wct  e.g. 2026-02-27T00%253a00%253a00Z

    # The ReturnUrl value (already percent-encoded as it must appear in the query string)
    return_url = (
        "%2fissue%2fwsfed%3fwa%3dwsignin1.0"
        "%26wtrealm%3dhttps%253a%252f%252fraynespark.communitysport.aeltc.com"
        "%26wctx%3drm%253d0%2526id%253d0%2526ru%253dhttps%25253a%25252f%25252fraynespark.communitysport.aeltc.com"
        f"%26wct%3d{ts_in}"
        "%26prealm%3dhttps%253a%252f%252fraynespark.communitysport.aeltc.com"
        "%26error%3dFalse%26message%3d%26hf%3d13%26bf%3d14%26source%3draynespark_communitysport_aeltc_com"
    )

    return (
        f"{AUTH_BASE}/account/signin"
        f"?ReturnUrl={return_url}"
        f"&wa=wsignin1.0"
        f"&wtrealm=https%3a%2f%2fraynespark.communitysport.aeltc.com"
        f"&wctx=rm%3d0%26id%3d0%26ru%3dhttps%253a%252f%252fraynespark.communitysport.aeltc.com"
        f"&wct={ts_out}"
        f"&prealm=https%3a%2f%2fraynespark.communitysport.aeltc.com"
        f"&error=False&message=&hf=13&bf=14&source=raynespark_communitysport_aeltc_com"
    )

SHOTS_DIR = _HERE / "screenshots"
SHOTS_DIR.mkdir(exist_ok=True)

_SCREENSHOTS_ENABLED = False  # set to True in debug mode only

NETWORK_LOG_PATH = _HERE / "network_log.json"

# ---------------------------------------------------------------------------
# Network request capture (debug mode only)
# ---------------------------------------------------------------------------

def attach_network_logger(page) -> list:
    """Silently capture all XHR/fetch requests. Saves to JSON, no console spam."""
    entries = []

    def _on_request(request):
        is_xhr = request.resource_type in ("xhr", "fetch")
        is_post_doc = request.resource_type == "document" and request.method != "GET"
        if not (is_xhr or is_post_doc):
            return
        try:
            body = request.post_data
        except Exception:
            buf = request.post_data_buffer
            body = f"<binary {len(buf)} bytes>" if buf else None
        entries.append({
            "method": request.method,
            "url":    request.url,
            "headers": dict(request.headers),
            "post_data": body,
            "responses": [],
        })

    page.on("request", _on_request)
    return entries


async def attach_network_response_logger(page, entries: list) -> None:
    """Silently capture responses and attach to matching request entries."""
    async def _on_response(response):
        req = response.request
        is_xhr = req.resource_type in ("xhr", "fetch")
        is_post_doc = req.resource_type == "document" and req.method != "GET"
        if not (is_xhr or is_post_doc):
            return
        try:
            body = await response.text()
        except Exception:
            body = "<could not read body>"
        for entry in reversed(entries):
            if entry["url"] == response.url:
                entry["responses"].append({"status": response.status, "body": body[:50000]})
                break

    page.on("response", _on_response)


def save_network_log(entries: list) -> None:
    with open(NETWORK_LOG_PATH, "w") as f:
        _json.dump(entries, f, indent=2)
    log.info(f"Network log → {NETWORK_LOG_PATH.name} ({len(entries)} entries)")

# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

def _tz() -> pytz.BaseTzInfo:
    return pytz.timezone(TZ_NAME)


# NTP offset: seconds to ADD to local time to get true wall-clock time.
# Populated by sync_ntp() at startup; 0.0 means "use local clock as-is".
_ntp_offset: float = 0.0


def sync_ntp(server: str = "pool.ntp.org") -> None:
    """Query an NTP server and store the local-clock offset."""
    global _ntp_offset
    try:
        resp = ntplib.NTPClient().request(server, version=3)
        _ntp_offset = resp.offset
        log.info(f"NTP sync OK  offset={_ntp_offset:+.3f}s  (server={server})")
    except Exception as e:
        log.warning(f"NTP sync failed — using local clock: {e}")


def now_true() -> datetime:
    """Current time corrected for any local-clock drift vs NTP."""
    return datetime.now(_tz()) + timedelta(seconds=_ntp_offset)


def midnight_tonight() -> datetime:
    """Start of tomorrow in local time — when the new booking window opens."""
    tz       = _tz()
    tomorrow = now_true().date() + timedelta(days=1)
    return tz.localize(datetime(tomorrow.year, tomorrow.month, tomorrow.day))


def target_date() -> str:
    """
    The court date that becomes bookable at tonight's midnight.
    'Up to 13 days in advance' means at midnight on day X, day X+13 opens.
    """
    tomorrow = now_true().date() + timedelta(days=1)
    return (tomorrow + timedelta(days=13)).strftime("%Y-%m-%d")


def secs_until(dt: datetime) -> float:
    return (dt - now_true()).total_seconds()


async def _dismiss_cookie_banner(page: Page) -> None:
    """Remove the cookie consent overlay so it doesn't block clicks."""
    try:
        await page.evaluate("document.querySelector('.osano-cm-dialog')?.remove()")
        await page.evaluate("document.querySelector('.osano-cm-overlay')?.remove()")
    except Exception:
        pass


async def shot(page: Page, name: str, full_page: bool = True) -> None:
    if not _SCREENSHOTS_ENABLED:
        return
    path = SHOTS_DIR / f"{name}.png"
    try:
        await page.screenshot(path=str(path), full_page=full_page, timeout=5_000)
        log.info(f"  Screenshot → screenshots/{name}.png")
    except Exception as e:
        if full_page:
            try:
                # Modal overlays block full-page scrolling — fall back to viewport only
                await page.screenshot(path=str(path), full_page=False, timeout=3_000)
                log.info(f"  Screenshot (viewport) → screenshots/{name}.png")
            except Exception as e2:
                log.warning(f"  Screenshot '{name}' failed (non-fatal): {e2}")
        else:
            log.warning(f"  Screenshot '{name}' failed (non-fatal): {e}")

# ---------------------------------------------------------------------------
# 1. Login
# ---------------------------------------------------------------------------

async def login(page: Page) -> bool:
    login_url = build_login_url()
    await page.goto(login_url, wait_until="networkidle", timeout=30_000)
    await page.wait_for_selector("#EmailAddress", timeout=15_000)
    await shot(page, "01_login_page")

    await page.fill("#EmailAddress", USERNAME)
    await page.fill("#Password", PASSWORD)
    await page.click('button[type="submit"], input[type="submit"]')
    await page.wait_for_load_state("networkidle", timeout=20_000)
    await shot(page, "02_after_login")

    if "signin" in page.url.lower() or "login" in page.url.lower():
        log.error("Login failed — check credentials")
        return False

    log.info("Login OK")
    return True

# ---------------------------------------------------------------------------
# 2. Navigate to booking page
# ---------------------------------------------------------------------------

async def goto_booking_page(page: Page, date: str) -> None:
    url = f"{BOOKING_URL}#?date={date}&role=member"
    await page.goto(url, wait_until="networkidle", timeout=30_000)
    await asyncio.sleep(1.5)  # SPA render
    log.info(f"Booking page loaded ({date})")
    await shot(page, "03_booking_page")

# ---------------------------------------------------------------------------
# 3. Find and click an available slot on a preferred court
# ---------------------------------------------------------------------------

async def click_slot(page: Page, booking_time: str = BOOKING_TIME) -> bool:
    """Click first available slot at booking_time on a preferred court."""
    h, m   = booking_time.split(":")
    minutes = int(h) * 60 + int(m)

    # Build list of resource IDs for preferred courts, in order
    resource_ids = []
    for court_num in PREFERRED_COURTS:
        rid = COURT_RESOURCE_IDS.get((COURT_TYPE, court_num))
        if rid:
            resource_ids.append((court_num, rid))

    if not resource_ids:
        log.error(f"No resource IDs for court_type={COURT_TYPE} courts={PREFERRED_COURTS}")
        return False

    # Wait for any slot at this time to appear on the page
    # Format: booking-{resourceID}|{date}|{minutes}
    any_sel = f'a.book-interval.not-booked[data-test-id$="|{minutes}"]'
    try:
        await page.wait_for_selector(any_sel, timeout=15_000)
    except Exception:
        log.error(f"No slot at {booking_time}")
        await shot(page, "04_no_slot_found")
        return False

    # Try each preferred court in order
    for court_num, rid in resource_ids:
        sel = f'a.book-interval.not-booked[data-test-id^="booking-{rid}"][data-test-id$="|{minutes}"]'
        el = await page.query_selector(sel)
        if el:
            await el.click()
            log.info(f"Slot clicked — Court {court_num} ({COURT_TYPE}) at {booking_time}")
            await shot(page, "04_clicked_slot")
            return True

    log.error(f"No available {COURT_TYPE} slot at {booking_time} for courts {PREFERRED_COURTS}")
    await shot(page, "04_no_slot_found")
    return False

# ---------------------------------------------------------------------------
# 4. Click "Continue booking" in the slot popup
# ---------------------------------------------------------------------------

async def confirm_popup(page: Page) -> bool:
    """Click 'Continue booking' in the popup → navigates to /Booking/Book."""
    try:
        await page.wait_for_selector("#submit-booking", timeout=8_000)
        await shot(page, "05_popup")
    except Exception:
        log.error("'Continue booking' not found")
        await shot(page, "05_popup")
        return False

    async with page.expect_navigation(timeout=20_000):
        await page.click("#submit-booking")
    log.info("Continue booking → /Booking/Book")
    await shot(page, "06_booking_page")
    return True


# ---------------------------------------------------------------------------
# 5. Click "Confirm and pay", fill card details, optionally submit
# ---------------------------------------------------------------------------

async def pay(page: Page, dry_run: bool) -> bool:
    """Confirm and pay: click button → fill Stripe → submit."""

    await _dismiss_cookie_banner(page)

    # -- Click "Confirm and pay" (45s timeout for midnight server load) --
    try:
        await page.wait_for_selector(
            "button:has-text('Confirm and pay')", timeout=45_000,
        )
        await _dismiss_cookie_banner(page)
        await page.locator("button:has-text('Confirm and pay')").click(force=True)
        log.info("Confirm and pay clicked")
    except Exception as e:
        log.error(f"'Confirm and pay' not found: {e}")
        try:
            path = SHOTS_DIR / "07_error.png"
            await page.screenshot(path=str(path), full_page=False, timeout=3_000)
            log.error(f"Page text: {(await page.inner_text('body'))[:500]}")
        except Exception:
            pass
        return False

    # -- Wait for Stripe iframes --
    try:
        await page.wait_for_selector(
            'iframe[title="Secure card number input frame"]', timeout=10_000,
        )
    except Exception as e:
        log.error(f"Stripe form not found: {e}")
        return False

    if not CARD_NUMBER:
        log.error("CARD_NUMBER not set")
        return False

    # -- Fill card fields sequentially (Stripe iframes reject .fill()) --
    try:
        num_input = page.frame_locator('iframe[title="Secure card number input frame"]') \
            .locator('input[placeholder*="1234"]')
        await num_input.click()
        await num_input.press_sequentially(CARD_NUMBER, delay=50)

        exp_input = page.frame_locator('iframe[title="Secure expiration date input frame"]') \
            .locator('input[placeholder*="MM"]')
        await exp_input.click()
        await exp_input.press_sequentially(CARD_EXPIRY, delay=50)

        cvc_input = page.frame_locator('iframe[title="Secure CVC input frame"]') \
            .locator('input[name="cvc"]')
        await cvc_input.click()
        await cvc_input.press_sequentially(CARD_CVV, delay=50)

        log.info("Card filled")
        await asyncio.sleep(1)  # let Stripe validate
        await shot(page, "09_card_filled", full_page=False)
    except Exception as e:
        log.error(f"Card fill failed: {e}")
        return False

    if dry_run:
        log.info("[DRY-RUN] Stopping before Pay — card filled but not submitted")
        return True

    # -- Submit payment --
    await _dismiss_cookie_banner(page)
    await shot(page, "08_before_pay", full_page=False)
    try:
        await page.locator("#cs-stripe-elements-submit-button").click(force=True, timeout=10_000)
        log.info("Pay clicked")
    except Exception as e:
        log.error(f"Pay button failed: {e}")
        return False

    # -- Wait for confirmation --
    try:
        await page.wait_for_selector(
            "h1:has-text('Confirmed'), h2:has-text('Confirmed'), "
            "h1:has-text('Thank you'), p:has-text('successfully booked'), "
            ".booking-confirmed, .alert-success",
            timeout=20_000,
        )
        log.info("BOOKING CONFIRMED!")
        return True
    except Exception:
        log.error("Confirmation not detected")
        await shot(page, "10_unknown")
        return False


# ---------------------------------------------------------------------------
# Main session
# ---------------------------------------------------------------------------

async def run(debug: bool, skip_wait: bool, force_pay: bool = False, date_override: Optional[str] = None, time_override: Optional[str] = None) -> None:
    global _SCREENSHOTS_ENABLED
    _SCREENSHOTS_ENABLED = debug

    # Sync with NTP before any time calculations so sleeps fire at true midnight.
    sync_ntp()

    date         = date_override or BOOKING_DATE or target_date()
    booking_time = time_override or BOOKING_TIME
    midnight     = midnight_tonight()
    wait_secs    = secs_until(midnight)

    dry_run = debug and not force_pay
    mode = "DRY-RUN" if dry_run else ("DEBUG+PAY" if debug else "LIVE")
    log.info(f"=== {date} {booking_time} | opens {midnight.strftime('%H:%M:%S %Z')} | {mode} ===")

    # Sleep until PRE_LOGIN_SECS before midnight (unless skipping)
    if not skip_wait and not debug:
        sleep_time = max(0.0, wait_secs - PRE_LOGIN_SECS)
        if sleep_time > 0:
            log.info(f"Sleeping {sleep_time:.0f}s (wake {PRE_LOGIN_SECS}s before midnight)")
            await asyncio.sleep(sleep_time)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # ── Network logging (debug only — helps discover API endpoints) ──
        net_entries = []
        if debug:
            net_entries = attach_network_logger(page)
            await attach_network_response_logger(page, net_entries)

        # ── Login ──
        if not await login(page):
            await browser.close()
            return

        # ── Block junk resources to speed up page loads ──
        # Images, fonts, and analytics are irrelevant for booking.
        # Fewer HTTP connections = faster slot data load at midnight.
        if not debug:
            for pattern in [
                "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                "**/google-analytics.com/**",
                "**/googletagmanager.com/**",
                "**/region1.google-analytics.com/**",
            ]:
                await page.route(pattern, lambda route: route.abort())

        # ── Pre-load the booking page ──
        await goto_booking_page(page, date)

        # ── Wait for exact midnight ──
        if not skip_wait and not debug:
            remaining = secs_until(midnight)
            if remaining > 0:
                log.info(f"Waiting {remaining:.1f}s")
                if remaining > 0.2:
                    await asyncio.sleep(remaining - 0.2)
                while secs_until(midnight) > 0:
                    pass  # spin-wait final 200ms for precision
            log.info(">>> MIDNIGHT <<<")
            await page.reload(wait_until="commit", timeout=10_000)
        else:
            log.info("Skipping midnight wait.")

        # ── Find & click slot ──
        if not await click_slot(page, booking_time):
            await browser.close()
            return

        # ── Confirm popup ──
        if not await confirm_popup(page):
            await browser.close()
            return

        # ── Pay ──
        await pay(page, dry_run=dry_run)

        # ── Save network log + cookies (debug only) ──
        if debug:
            save_network_log(net_entries)
            try:
                cookies = await context.cookies()
                cookie_path = _HERE / "debug_cookies.json"
                with open(cookie_path, "w") as f:
                    _json.dump(cookies, f, indent=2)
                log.info(f"Cookies saved → {cookie_path.name}  ({len(cookies)} cookies)")
            except Exception as e:
                log.warning(f"Could not save cookies: {e}")

        await browser.close()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Raynes Park tennis court booker")
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable screenshots and network logging. Stops before payment unless --pay is also set.",
    )
    parser.add_argument(
        "--now", action="store_true",
        help="Skip the midnight wait — run the booking flow immediately.",
    )
    parser.add_argument(
        "--date", default=None, metavar="YYYY-MM-DD",
        help="Override target date (e.g. 2026-03-04). Default: auto-calculate from tonight's midnight.",
    )
    parser.add_argument(
        "--time", default=None, metavar="HH:MM",
        help="Override booking time (e.g. 09:00). Default: value from config.json.",
    )
    parser.add_argument(
        "--pay", action="store_true",
        help="Actually submit payment (even in --debug mode). Use with --debug --now to book immediately.",
    )
    args = parser.parse_args()

    if not USERNAME or not PASSWORD:
        log.error("BOOKING_USERNAME and BOOKING_PASSWORD must be set in .env")
        sys.exit(1)

    asyncio.run(run(debug=args.debug, skip_wait=args.now, force_pay=args.pay, date_override=args.date, time_override=args.time))
