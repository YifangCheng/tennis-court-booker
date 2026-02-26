#!/usr/bin/env python3
"""
Tennis Court Booker — Raynes Park
Automatically books a court (4-9) at 17:00 on the first day that becomes
bookable at tonight's midnight (i.e. today + 14 days).

Usage:
  python booker.py                  # real run — waits for midnight, then books
  python booker.py --debug          # screenshots at every step, skips payment
  python booker.py --now            # skip the midnight wait (use with --debug first)
  python booker.py --debug --now    # run right now in debug mode — start here!
"""

import argparse
import asyncio
import logging
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
        logging.FileHandler(_HERE / "booker.log"),
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
BOOKING_TIME     = _cfg.get("booking_time",       "17:00")
PREFERRED_COURTS = _cfg.get("preferred_courts",   [4, 5, 6, 7, 8, 9])
HEADLESS         = _cfg.get("headless",           True)
PRE_LOGIN_SECS   = _cfg.get("pre_login_seconds",  120)

USERNAME    = os.environ.get("BOOKING_USERNAME", "")
PASSWORD    = os.environ.get("BOOKING_PASSWORD", "")
CARD_NUMBER = os.environ.get("CARD_NUMBER", "")
CARD_EXPIRY = os.environ.get("CARD_EXPIRY", "")   # format: MM/YY
CARD_CVV    = os.environ.get("CARD_CVV",    "")
CARD_NAME   = os.environ.get("CARD_NAME",   "")

BASE_URL    = "https://raynespark.communitysport.aeltc.com"
AUTH_BASE   = "https://auth.communitysport.aeltc.com"
BOOKING_URL = f"{BASE_URL}/Booking/BookByDate"


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

# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

def _tz() -> pytz.BaseTzInfo:
    return pytz.timezone(TZ_NAME)


def midnight_tonight() -> datetime:
    """Start of tomorrow in local time — when the new booking window opens."""
    tz       = _tz()
    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    return tz.localize(datetime(tomorrow.year, tomorrow.month, tomorrow.day))


def target_date() -> str:
    """
    The court date that becomes bookable at tonight's midnight.
    'Up to 13 days in advance' means at midnight on day X, day X+13 opens.
    """
    tz       = _tz()
    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    return (tomorrow + timedelta(days=13)).strftime("%Y-%m-%d")


def secs_until(dt: datetime) -> float:
    return (dt - datetime.now(_tz())).total_seconds()


async def shot(page: Page, name: str, full_page: bool = True) -> None:
    path = SHOTS_DIR / f"{name}.png"
    try:
        await page.screenshot(path=str(path), full_page=full_page, timeout=5_000)
        log.info(f"  Screenshot → screenshots/{name}.png")
    except Exception:
        if full_page:
            try:
                # Modal overlays block full-page scrolling — fall back to viewport only
                await page.screenshot(path=str(path), full_page=False, timeout=3_000)
                log.info(f"  Screenshot (viewport) → screenshots/{name}.png")
            except Exception as e:
                log.warning(f"  Screenshot '{name}' failed (non-fatal): {e}")
        else:
            log.warning(f"  Screenshot '{name}' failed (non-fatal)")

# ---------------------------------------------------------------------------
# 1. Login
# ---------------------------------------------------------------------------

async def login(page: Page) -> bool:
    log.info("── Step 1: Login ──────────────────────────────")
    login_url = build_login_url()
    log.info(f"Auth URL built with timestamp: {login_url[:80]}…")
    await page.goto(login_url, wait_until="networkidle", timeout=30_000)
    await page.wait_for_selector("#EmailAddress", timeout=15_000)
    await shot(page, "01_login_page")

    await page.fill("#EmailAddress", USERNAME)
    await page.fill("#Password", PASSWORD)
    await page.click('button[type="submit"], input[type="submit"]')
    await page.wait_for_load_state("networkidle", timeout=20_000)
    await shot(page, "02_after_login")

    if "signin" in page.url.lower() or "login" in page.url.lower():
        log.error("Login failed — are BOOKING_USERNAME / BOOKING_PASSWORD correct?")
        return False

    log.info("Login successful.")
    return True

# ---------------------------------------------------------------------------
# 2. Navigate to booking page
# ---------------------------------------------------------------------------

async def goto_booking_page(page: Page, date: str) -> None:
    url = f"{BOOKING_URL}#?date={date}&role=member"
    log.info(f"── Step 2: Booking page ({url}) ──────────────")
    await page.goto(url, wait_until="networkidle", timeout=30_000)
    # SPA needs a moment to render the timetable
    await asyncio.sleep(1.5)
    await shot(page, "03_booking_page")

# ---------------------------------------------------------------------------
# 3. Find and click an available 17:00 slot
# ---------------------------------------------------------------------------

async def click_slot(page: Page, booking_time: str = BOOKING_TIME) -> bool:
    """
    Click the first available slot at booking_time.

    The site encodes time as minutes-from-midnight in data-test-id:
      e.g. 09:00 → 540,  17:00 → 1020
    Available slots have class 'book-interval not-booked'.
    """
    log.info(f"── Step 3: Finding {booking_time} slot ────────────────")

    h, m   = booking_time.split(":")
    minutes = int(h) * 60 + int(m)
    sel    = f'a.book-interval.not-booked[data-test-id*="|{minutes}"]'
    log.info(f"Selector: {sel}")

    try:
        await page.wait_for_selector(sel, timeout=5_000)
    except Exception:
        log.error(f"No available slot found for {booking_time} (minutes={minutes}). Check 04_no_slot_found.png.")
        await shot(page, "04_no_slot_found")
        return False

    el = await page.query_selector(sel)
    text = (await el.inner_text()).strip()
    log.info(f"Clicking slot: '{text}'")
    await el.click()
    await shot(page, "04_clicked_slot")
    return True

# ---------------------------------------------------------------------------
# 4. Click "Continue booking" in the slot popup
# ---------------------------------------------------------------------------

async def confirm_popup(page: Page) -> bool:
    """After clicking a slot, a popup appears. Click the green 'Continue booking' button."""
    log.info("── Step 4: Continue booking popup ────────────")

    try:
        await page.wait_for_selector("#submit-booking", timeout=8_000)
        await shot(page, "05_popup")
    except Exception:
        log.error("'Continue booking' button did not appear. Check 05_popup.png.")
        await shot(page, "05_popup")
        return False

    # Click and wait for navigation to /Booking/Book
    async with page.expect_navigation(timeout=20_000):
        await page.click("#submit-booking")
    log.info("Clicked 'Continue booking', navigated to booking page.")
    await shot(page, "06_booking_page")
    return True


# ---------------------------------------------------------------------------
# 5. Click "Confirm and pay", fill card details, optionally submit
# ---------------------------------------------------------------------------

async def pay(page: Page, debug: bool) -> bool:
    """
    On the /Booking/Book page:
      1. Click green 'Confirm and pay' button
      2. Card details popup appears — fill in card fields
      3. Real mode:  click 'Pay £...' to complete
         Debug mode: stop after filling (do NOT click pay)
    """
    log.info("── Step 5: Confirm and pay ────────────────────")

    # -- 5a. Click "Confirm and pay" --
    try:
        await page.wait_for_selector(
            "button:has-text('Confirm and pay')",
            timeout=10_000,
        )
        await shot(page, "07_confirm_and_pay_page")
        await page.click("button:has-text('Confirm and pay')")
        log.info("Clicked 'Confirm and pay'.")
    except Exception as e:
        log.error(f"Could not find 'Confirm and pay' button: {e}")
        await shot(page, "07_confirm_and_pay_error")
        return False

    # -- 5b. Wait for Stripe payment form --
    # Stripe Elements renders each field inside its own iframe.
    # The container divs (on the main page) have ids from the label for= attributes.
    try:
        await page.wait_for_selector(
            'iframe[title="Secure card number input frame"]',
            timeout=10_000,
        )
        await shot(page, "08_card_popup", full_page=False)
        log.info("Stripe payment form appeared.")
    except Exception as e:
        log.error(f"Stripe payment form did not appear: {e}")
        await shot(page, "08_card_popup_error")
        return False

    if not CARD_NUMBER:
        log.error("CARD_NUMBER is not set in .env — cannot fill payment.")
        return False

    # -- 5c. Fill Stripe iframe fields --
    # Each Stripe field is in its own iframe, identified by the title attribute.
    try:
        await page.frame_locator('iframe[title="Secure card number input frame"]') \
            .locator('input[placeholder*="1234"]').fill(CARD_NUMBER)
        log.info("Filled card number.")

        await page.frame_locator('iframe[title="Secure expiration date input frame"]') \
            .locator('input[placeholder*="MM"]').fill(CARD_EXPIRY)
        log.info("Filled expiry.")

        await page.frame_locator('iframe[title="Secure CVC input frame"]') \
            .locator('input[name="cvc"]').fill(CARD_CVV)
        log.info("Filled CVC.")

        await shot(page, "09_card_filled", full_page=False)
        log.info("Card details filled.")
    except Exception as e:
        log.error(f"Could not fill card details: {e}")
        await shot(page, "09_card_fill_error")
        return False

    # -- 5d. Submit (real mode only) --
    if debug:
        log.info("[DEBUG] Card details filled. Stopping before 'Pay £...' — inspect 09_card_filled.png.")
        return True

    try:
        await page.click(
            "button:has-text('Pay £'), button:has-text('Pay now'), "
            "button:has-text('Pay')",
            timeout=10_000,
        )
        log.info("Clicked Pay.")
    except Exception as e:
        log.error(f"Could not click Pay button: {e}")
        await shot(page, "10_pay_btn_error")
        return False

    # Wait for confirmation
    try:
        await page.wait_for_selector(
            "h1:has-text('Confirmed'), h2:has-text('Confirmed'), "
            "h1:has-text('Thank you'), p:has-text('successfully booked'), "
            ".booking-confirmed, .alert-success",
            timeout=20_000,
        )
        await shot(page, "10_booking_confirmed")
        log.info("✓ BOOKING CONFIRMED!")
        return True
    except Exception:
        log.error("Could not detect confirmation — check 10_unknown.png")
        await shot(page, "10_unknown")
        return False

# ---------------------------------------------------------------------------
# Main session
# ---------------------------------------------------------------------------

async def run(debug: bool, skip_wait: bool, date_override: Optional[str] = None, time_override: Optional[str] = None) -> None:
    date         = date_override  if date_override  else target_date()
    booking_time = time_override  if time_override  else BOOKING_TIME
    midnight     = midnight_tonight()
    wait_secs    = secs_until(midnight)

    log.info("=" * 55)
    log.info(f"Target date    : {date}  at  {booking_time}")
    if date_override or time_override:
        log.info("Mode           : MANUAL OVERRIDE")
    log.info(f"Booking opens  : {midnight.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log.info(f"Time until open: {wait_secs / 3600:.2f} h  ({wait_secs:.0f} s)")
    if debug:
        log.info("Mode           : DEBUG (card details filled but Pay not clicked)")
    log.info("=" * 55)

    # Sleep until PRE_LOGIN_SECS before midnight (unless skipping)
    if not skip_wait and not debug:
        sleep_time = max(0.0, wait_secs - PRE_LOGIN_SECS)
        if sleep_time > 0:
            log.info(f"Sleeping {sleep_time:.0f} s  (waking {PRE_LOGIN_SECS}s before midnight) …")
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

        # ── Login ──
        if not await login(page):
            await browser.close()
            return

        # ── Pre-load the booking page while waiting for midnight ──
        await goto_booking_page(page, date)

        # ── Wait for exact midnight ──
        if not skip_wait and not debug:
            remaining = secs_until(midnight)
            if remaining > 0:
                log.info(f"Waiting final {remaining:.3f} s …")
                await asyncio.sleep(remaining)
            log.info(">>> MIDNIGHT — executing booking now! <<<")
            await page.reload(wait_until="networkidle", timeout=20_000)
            await asyncio.sleep(0.5)
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
        await pay(page, debug=debug)

        await browser.close()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Raynes Park tennis court booker")
    parser.add_argument(
        "--debug", action="store_true",
        help="Take screenshots at each step but do NOT submit payment",
    )
    parser.add_argument(
        "--now", action="store_true",
        help="Skip the midnight wait — useful for testing with --debug",
    )
    parser.add_argument(
        "--date", default=None, metavar="YYYY-MM-DD",
        help="Override target date (e.g. 2026-03-04). Default: auto-calculate from tonight's midnight.",
    )
    parser.add_argument(
        "--time", default=None, metavar="HH:MM",
        help="Override booking time (e.g. 09:00). Default: value from config.json.",
    )
    args = parser.parse_args()

    if not USERNAME or not PASSWORD:
        log.error("BOOKING_USERNAME and BOOKING_PASSWORD must be set in .env")
        sys.exit(1)

    asyncio.run(run(debug=args.debug, skip_wait=args.now, date_override=args.date, time_override=args.time))
