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


async def shot(page: Page, name: str) -> None:
    path = SHOTS_DIR / f"{name}.png"
    await page.screenshot(path=str(path), full_page=True)
    log.info(f"  Screenshot → screenshots/{name}.png")

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

async def click_slot(page: Page) -> bool:
    """
    Try to click an available slot at BOOKING_TIME for any court in
    PREFERRED_COURTS.  Three strategies tried in order.
    """
    log.info(f"── Step 3: Finding {BOOKING_TIME} slot ───────────────")

    # --- Strategy A: data-attribute selectors ---
    for court in PREFERRED_COURTS:
        for sel in [
            f'[data-court="{court}"][data-time="{BOOKING_TIME}"]',
            f'[data-resource="{court}"][data-slot="{BOOKING_TIME}"]',
            f'[data-resource-id="{court}"][data-start-time="{BOOKING_TIME}"]',
            f'td[data-court="{court}"][data-time="{BOOKING_TIME}"]',
        ]:
            el = await page.query_selector(sel)
            if el and await el.is_visible() and await el.is_enabled():
                log.info(f"Strategy A: clicking court {court} via '{sel}'")
                await el.click()
                await shot(page, f"04_clicked_court_{court}")
                return True

    log.info("Strategy A found nothing — trying strategy B (row-based) …")

    # --- Strategy B: find the court's row, then the matching time cell ---
    for court in PREFERRED_COURTS:
        row = await page.query_selector(
            f'tr[data-court="{court}"], '
            f'.court-row[data-court="{court}"], '
            f'[class*="court"][data-id="{court}"], '
            f'[data-resource="{court}"]'
        )
        if row:
            slot = await row.query_selector(
                f'[data-time="{BOOKING_TIME}"], '
                'td.available, td.open, .slot.available'
            )
            if slot and await slot.is_visible():
                log.info(f"Strategy B: clicking court {court} slot in row")
                await slot.click()
                await shot(page, f"04_clicked_court_{court}_rowstrat")
                return True

    log.info("Strategy B found nothing — trying strategy C (text match) …")

    # --- Strategy C: scan all visible available cells for the right time ---
    cells = await page.query_selector_all(
        '.available:not(.disabled), .bookable:not(.disabled), '
        'td.available, td.open, '
        '.slot:not(.disabled):not(.unavailable):not(.booked)'
    )
    for cell in cells:
        text  = (await cell.inner_text()).strip()
        title = (await cell.get_attribute("title") or "").strip()
        if BOOKING_TIME in text or BOOKING_TIME in title:
            log.info(f"Strategy C: clicking cell with text='{text}' title='{title}'")
            await cell.click()
            await shot(page, "04_clicked_text_match")
            return True

    log.error(
        f"Could not find any available {BOOKING_TIME} slot. "
        "Check screenshots/03_booking_page.png to inspect the page."
    )
    await shot(page, "04_no_slot_found")
    return False

# ---------------------------------------------------------------------------
# 4. Confirm the booking popup
# ---------------------------------------------------------------------------

async def confirm_popup(page: Page) -> bool:
    log.info("── Step 4: Confirming popup ───────────────────")

    modal_sel = (
        ".modal.show, .modal[style*='display: block'], "
        "[role='dialog'], .popup, .booking-popup, "
        ".booking-details, #bookingModal"
    )
    try:
        await page.wait_for_selector(modal_sel, timeout=6_000)
        await shot(page, "05_popup")
    except Exception:
        # Some sites navigate directly to a confirmation page without a modal
        log.info("No modal appeared — may have auto-navigated to confirmation page.")
        await shot(page, "05_no_popup")

    confirm_sel = (
        "button:has-text('Confirm'), button:has-text('Book'), "
        "button:has-text('Next'), button:has-text('Continue'), "
        "button:has-text('Proceed'), .btn-confirm, .btn-primary"
    )
    try:
        await page.click(confirm_sel, timeout=6_000)
        log.info("Clicked confirm.")
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await shot(page, "06_after_confirm")
        return True
    except Exception as e:
        log.error(f"Could not click confirm button: {e}")
        await shot(page, "06_confirm_failed")
        return False

# ---------------------------------------------------------------------------
# 5. Payment
# ---------------------------------------------------------------------------

async def pay(page: Page, debug: bool) -> bool:
    log.info("── Step 5: Payment ────────────────────────────")
    await shot(page, "07_payment_page")

    if debug:
        log.info("[DEBUG MODE] Skipping payment — inspect screenshots/ to verify.")
        return True

    if not CARD_NUMBER:
        log.error("CARD_NUMBER is not set in .env — cannot complete payment.")
        return False

    filled = False

    # --- Try iframe-based payment (Stripe, Braintree, etc.) ---
    iframe_sels = [
        "iframe[name*='card']",
        "iframe[src*='stripe']",
        "iframe[src*='braintree']",
        "iframe[title*='card']",
        "iframe[title*='payment']",
        "iframe[src*='pay']",
    ]
    for iframe_sel in iframe_sels:
        iframe_el = await page.query_selector(iframe_sel)
        if not iframe_el:
            continue
        log.info(f"Found payment iframe: {iframe_sel}")
        frame = page.frame_locator(iframe_sel)
        try:
            await frame.locator(
                "input[placeholder*='card number'], input[name*='cardnumber'], "
                "input[placeholder*='1234 1234']"
            ).fill(CARD_NUMBER, timeout=5_000)
            await frame.locator(
                "input[placeholder*='MM / YY'], input[placeholder*='MM/YY'], "
                "input[name*='exp']"
            ).fill(CARD_EXPIRY)
            await frame.locator(
                "input[placeholder*='CVC'], input[placeholder*='CVV'], "
                "input[name*='cvc'], input[name*='cvv']"
            ).fill(CARD_CVV)
            log.info("Filled card details in iframe.")
            filled = True
            break
        except Exception as e:
            log.warning(f"iframe fill failed ({iframe_sel}): {e}")

    # --- Fallback: direct form fields ---
    if not filled:
        log.info("No iframe — trying direct form fields …")
        try:
            await page.fill(
                'input[autocomplete="cc-number"], '
                'input[name*="card_number"], input[id*="card-number"], '
                'input[placeholder*="card number"]',
                CARD_NUMBER,
            )
            await page.fill(
                'input[autocomplete="cc-exp"], '
                'input[name*="expiry"], input[id*="expiry"], '
                'input[placeholder*="MM"]',
                CARD_EXPIRY,
            )
            await page.fill(
                'input[autocomplete="cc-csc"], '
                'input[name*="cvv"], input[name*="cvc"], '
                'input[id*="cvv"], input[id*="cvc"]',
                CARD_CVV,
            )
            if CARD_NAME:
                await page.fill(
                    'input[autocomplete="cc-name"], '
                    'input[name*="cardholder"], input[id*="card-name"]',
                    CARD_NAME,
                )
            log.info("Filled card details directly on page.")
            filled = True
        except Exception as e:
            log.error(f"Could not fill card details: {e}")
            await shot(page, "07_card_fill_error")
            return False

    await shot(page, "08_card_filled")

    # Submit payment
    try:
        await page.click(
            "button:has-text('Pay'), button:has-text('Pay now'), "
            "button:has-text('Complete payment'), button:has-text('Confirm and pay'), "
            "input[type='submit']",
            timeout=10_000,
        )
        log.info("Clicked Pay.")
    except Exception as e:
        log.error(f"Could not click Pay button: {e}")
        await shot(page, "08_pay_btn_error")
        return False

    # Wait for confirmation
    try:
        await page.wait_for_selector(
            ".confirmation, .booking-confirmed, "
            "h1:has-text('Confirmed'), h2:has-text('Confirmed'), "
            "h1:has-text('Thank you'), p:has-text('successfully booked'), "
            ".alert-success, [class*='success']",
            timeout=20_000,
        )
        await shot(page, "09_booking_confirmed")
        log.info("✓ BOOKING CONFIRMED!")
        return True
    except Exception:
        log.error("Could not detect confirmation page — check 09_unknown.png")
        await shot(page, "09_unknown")
        return False

# ---------------------------------------------------------------------------
# Main session
# ---------------------------------------------------------------------------

async def run(debug: bool, skip_wait: bool) -> None:
    date      = target_date()
    midnight  = midnight_tonight()
    wait_secs = secs_until(midnight)

    log.info("=" * 55)
    log.info(f"Target date    : {date}  at  {BOOKING_TIME}")
    log.info(f"Booking opens  : {midnight.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log.info(f"Time until open: {wait_secs / 3600:.2f} h  ({wait_secs:.0f} s)")
    if debug:
        log.info("Mode           : DEBUG (no payment will be made)")
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
        if not await click_slot(page):
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
    args = parser.parse_args()

    if not USERNAME or not PASSWORD:
        log.error("BOOKING_USERNAME and BOOKING_PASSWORD must be set in .env")
        sys.exit(1)

    asyncio.run(run(debug=args.debug, skip_wait=args.now))
