import asyncio
import json as _json
import os
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Optional

import pytz
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Page, async_playwright

from shared.runtime import ROOT, RunOptions, TimingHelper, get_logger, load_json, load_root_env
from sites.base import BookingSite

load_root_env()
log = get_logger()


class ClubSparkSite(BookingSite):
    name = "club_spark"
    description = "ClubSpark venue booker"
    env_prefix = "CLUB_SPARK"

    def __init__(self) -> None:
        self.site_dir = Path(__file__).resolve().parent
        self.shots_dir = ROOT / "screenshots"
        self.shots_dir.mkdir(exist_ok=True)
        self.network_log_path = ROOT / "network_log.club_spark.json"
        self.live_network_log_path = ROOT / "network_log.club_spark.live.json"
        self.cookies_path = ROOT / "debug_cookies.club_spark.json"
        self.live_timeout_shot_path = self.shots_dir / "club_10_payment_timeout_live.png"
        self._screenshots_enabled = False
        self.resource_ids_by_court = {}
        self._capture_live_diagnostics = False

        cfg = load_json(self.site_dir / "config.json")
        self.cfg = cfg

        self.tz_name = cfg.get("timezone", "Europe/London")
        self.booking_duration_minutes = int(cfg.get("booking_duration_minutes", 60))
        self.headless = cfg.get("headless", True)
        self.pre_login_secs = int(cfg.get("pre_login_seconds", 120))
        self.booking_open_time = cfg.get("booking_open_time", "20:00")

        self.card_number = os.environ.get("CARD_NUMBER", "")
        self.card_expiry = os.environ.get("CARD_EXPIRY", "")
        self.card_cvv = os.environ.get("CARD_CVV", "")

        self.api_base_url = "https://clubspark.lta.org.uk"
        self.venue = ""
        self.base_url = ""
        self.booking_time = ""
        self.preferred_courts: list[int] = []
        self.account_name = "a"
        self.username = ""
        self.password = ""

    def configure_account(self, account_override: Optional[str]) -> None:
        account = (account_override or self.cfg.get("account") or "a").strip()
        normalized = account.upper().replace("-", "_")
        username_key = f"{self.env_prefix}_{normalized}_BOOKING_USERNAME"
        password_key = f"{self.env_prefix}_{normalized}_BOOKING_PASSWORD"
        self.username = os.environ.get(username_key, "")
        self.password = os.environ.get(password_key, "")
        self.account_name = account
        self._username_env_key = username_key
        self._password_env_key = password_key

    def validate_environment(self) -> None:
        if not self.username or not self.password:
            raise SystemExit(f"{self._username_env_key} and {self._password_env_key} must be set in .env")
        if not self.venue:
            raise SystemExit("club_spark requires --venue VENUE_SLUG, for example --venue TannerStPark")
        if not self.booking_time:
            raise SystemExit("club_spark requires --time HH:MM")
        if not self.preferred_courts:
            raise SystemExit("club_spark requires --court COURT_NUMBER")

    def configure_venue(self, venue_override: Optional[str]) -> None:
        venue = (venue_override or self.cfg.get("venue") or "").strip()
        if not venue:
            self.venue = ""
            self.base_url = ""
            return
        self.venue = venue
        self.base_url = f"{self.api_base_url}/{venue}"

    def configure_booking(self, options: RunOptions) -> None:
        self.booking_time = (options.time_override or "").strip()
        if options.court_override is None:
            self.preferred_courts = []
        else:
            self.preferred_courts = [int(options.court_override)]

    def booking_url_for(self, date: str) -> str:
        return f"{self.base_url}/Booking/BookByDate#?date={date}&role=guest"

    def release_time(self, timing: TimingHelper) -> datetime:
        now = timing.now_true()
        open_hour, open_minute = self.booking_open_time.split(":")
        release = now.replace(
            hour=int(open_hour),
            minute=int(open_minute),
            second=0,
            microsecond=0,
        )
        if release <= now:
            release += timedelta(days=1)
        return release

    def target_date(self, timing: TimingHelper, override: Optional[str]) -> str:
        if override:
            return override
        return (timing.now_true().date() + timedelta(days=7)).strftime("%Y-%m-%d")

    def booking_end_time(self, booking_time: str) -> str:
        slot_start = datetime.strptime(booking_time, "%H:%M")
        slot_end = slot_start + timedelta(minutes=self.booking_duration_minutes)
        return slot_end.strftime("%H:%M")

    def booking_minutes(self, booking_time: str) -> int:
        hours, minutes = booking_time.split(":")
        return int(hours) * 60 + int(minutes)

    async def shot(self, page: Page, name: str, full_page: bool = True) -> None:
        if not self._screenshots_enabled:
            return
        path = self.shots_dir / f"{name}.png"
        await self.write_screenshot(page, path, full_page=full_page)

    async def write_screenshot(self, page: Page, path: Path, full_page: bool = True) -> None:
        try:
            await page.screenshot(path=str(path), full_page=full_page, timeout=5_000)
            if path.parent == self.shots_dir:
                log.info(f"  Screenshot → screenshots/{path.name}")
            else:
                log.info(f"  Screenshot → {path}")
        except Exception as exc:
            if full_page:
                try:
                    await page.screenshot(path=str(path), full_page=False, timeout=3_000)
                    if path.parent == self.shots_dir:
                        log.info(f"  Screenshot (viewport) → screenshots/{path.name}")
                    else:
                        log.info(f"  Screenshot (viewport) → {path}")
                except Exception as fallback_exc:
                    log.warning(f"  Screenshot '{path.name}' failed (non-fatal): {fallback_exc}")
            else:
                log.warning(f"  Screenshot '{path.name}' failed (non-fatal): {exc}")

    def attach_network_logger(self, page: Page) -> list:
        entries = []

        def _on_request(request) -> None:
            is_xhr = request.resource_type in ("xhr", "fetch")
            is_post_doc = request.resource_type == "document" and request.method != "GET"
            if not (is_xhr or is_post_doc):
                return
            try:
                body = request.post_data
            except Exception:
                buf = request.post_data_buffer
                body = f"<binary {len(buf)} bytes>" if buf else None
            entries.append(
                {
                    "method": request.method,
                    "url": request.url,
                    "resource_type": request.resource_type,
                    "headers": dict(request.headers),
                    "post_data": body,
                    "responses": [],
                }
            )

        def _on_request_failed(request) -> None:
            for entry in reversed(entries):
                if entry["url"] == request.url and entry["method"] == request.method:
                    entry.setdefault("failures", []).append(request.failure)
                    break

        page.on("request", _on_request)
        page.on("requestfailed", _on_request_failed)
        return entries

    async def attach_network_response_logger(self, page: Page, entries: list) -> None:
        async def _on_response(response) -> None:
            try:
                req = response.request
                is_xhr = req.resource_type in ("xhr", "fetch")
                is_post_doc = req.resource_type == "document" and req.method != "GET"
                if not (is_xhr or is_post_doc):
                    return
                body = await response.text()
            except TargetClosedError:
                return
            except Exception:
                body = "<could not read body>"
            for entry in reversed(entries):
                if entry["url"] == response.url:
                    entry["responses"].append({"status": response.status, "body": body[:50000]})
                    break

        page.on("response", _on_response)

    def save_network_log(self, entries: list) -> None:
        self.save_network_log_to(entries, self.network_log_path)

    def save_network_log_to(self, entries: list, path: Path) -> None:
        with open(path, "w") as handle:
            _json.dump(entries, handle, indent=2)
        log.info(f"Network log → {path.name} ({len(entries)} entries)")

    async def api_get_json(self, page: Page, url: str) -> dict:
        response = await page.context.request.get(
            url,
            headers={
                "x-requested-with": "XMLHttpRequest",
                "referer": f"{self.base_url}/Booking/BookByDate",
            },
        )
        if not response.ok:
            raise RuntimeError(f"Request failed ({response.status}) for {url}")
        return await response.json()

    async def get_settings(self, page: Page) -> dict:
        return await self.api_get_json(page, f"{self.api_base_url}/v0/VenueBooking/{self.venue}/GetSettings")

    async def get_venue_sessions(self, page: Page, date: str) -> dict:
        url = (
            f"{self.api_base_url}/v0/VenueBooking/{self.venue}/GetVenueSessions"
            f"?resourceID=&startDate={date}&endDate={date}&roleId="
        )
        return await self.api_get_json(page, url)

    def court_number(self, resource: dict) -> Optional[int]:
        number = resource.get("Number")
        if isinstance(number, int):
            return number + 1
        name = resource.get("Name", "")
        if name.startswith("Court "):
            try:
                return int(name.split("Court ", 1)[1].strip())
            except ValueError:
                return None
        return None

    def cache_resource_map(self, sessions: dict) -> None:
        mapping = {}
        for resource in sessions.get("Resources", []):
            court_num = self.court_number(resource)
            if court_num is not None:
                mapping[court_num] = resource.get("ID")
        if mapping:
            self.resource_ids_by_court = mapping

    def session_is_available(self, session: dict, start_minutes: int) -> bool:
        if session.get("Category") != 0:
            return False
        if session.get("Capacity", 0) <= 0:
            return False
        session_start = int(session.get("StartTime", -1))
        session_end = int(session.get("EndTime", -1))
        if start_minutes < session_start:
            return False
        if start_minutes + self.booking_duration_minutes > session_end:
            return False
        return True

    def find_slot_from_sessions(self, sessions: dict, booking_time: str):
        start_minutes = self.booking_minutes(booking_time)
        resources = sessions.get("Resources", [])

        preferred = list(self.preferred_courts) if self.preferred_courts else []
        if preferred:
            ordered_resources = []
            for wanted_court in preferred:
                for resource in resources:
                    if self.court_number(resource) == wanted_court:
                        ordered_resources.append(resource)
                        break
        else:
            ordered_resources = resources

        for resource in ordered_resources:
            for day in resource.get("Days", []):
                for session in day.get("Sessions", []):
                    if self.session_is_available(session, start_minutes):
                        return resource, session

        return None, None

    async def wait_for_slot_via_api(self, page: Page, booking_time: str, date: str) -> bool:
        try:
            sessions = await self.get_venue_sessions(page, date)
            self.cache_resource_map(sessions)
        except Exception as exc:
            log.error(f"Could not load ClubSpark booking API data: {exc}")
            return False

        resource, session = self.find_slot_from_sessions(sessions, booking_time)
        if not resource or not session:
            log.error(f"No API slot found at {booking_time}")
            return False

        log.info(
            f"API slot found at {booking_time} on resource {resource['ID']} (session {session['ID']})"
        )
        return True

    async def dismiss_cookie_banner(self, page: Page) -> None:
        button_selectors = [
            "button:has-text('Reject Non-Essential')",
            "button:has-text('Reject non-essential')",
            "button:has-text('Accept All')",
            "button:has-text('Accept all')",
            "button:has-text('OK')",
            "button:has-text('Ok')",
            "button:has-text('Close')",
            "[aria-label='Close']",
        ]
        for selector in button_selectors:
            try:
                button = page.locator(selector).first
                if await button.is_visible(timeout=500):
                    await button.click(timeout=2_000)
                    await asyncio.sleep(0.2)
                    break
            except Exception:
                pass

        selectors = [
            "#CybotCookiebotDialog",
            "#CybotCookiebotDialogBodyUnderlay",
            ".osano-cm-dialog",
            ".osano-cm-overlay",
        ]
        for selector in selectors:
            try:
                await page.evaluate(
                    """sel => {
                        const el = document.querySelector(sel);
                        if (el) el.remove();
                    }""",
                    selector,
                )
            except Exception:
                pass

    async def visible_messages(self, page: Page) -> list[str]:
        selectors = [
            ".validation-summary-errors",
            ".field-validation-error",
            ".alert-danger",
            ".alert-warning",
            ".alert-error",
            ".error-message",
            ".error",
            "[role='alert']",
            ".notification",
            ".message",
        ]
        messages: list[str] = []
        seen = set()
        for selector in selectors:
            try:
                locators = page.locator(selector)
                count = await locators.count()
            except Exception:
                continue
            for index in range(min(count, 5)):
                try:
                    locator = locators.nth(index)
                    if not await locator.is_visible(timeout=150):
                        continue
                    text = " ".join((await locator.inner_text(timeout=500)).split())
                except Exception:
                    continue
                if len(text) < 3 or text.lower() in seen:
                    continue
                messages.append(text[:200])
                seen.add(text.lower())
                if len(messages) >= 5:
                    return messages
        return messages

    async def paynow_status(self, page: Page) -> tuple[bool, Optional[str], Optional[str]]:
        paynow = page.locator("#paynow").first
        try:
            if not await paynow.is_visible(timeout=250):
                return False, None, None
        except Exception:
            return False, None, None

        disabled = None
        for attr in ("disabled", "aria-disabled"):
            try:
                value = await paynow.get_attribute(attr)
            except Exception:
                value = None
            if value is not None:
                disabled = f"{attr}={value}"
                break
        try:
            classes = await paynow.get_attribute("class")
        except Exception:
            classes = None
        return True, disabled, classes

    async def stripe_is_ready(self, page: Page) -> bool:
        stripe_selectors = [
            'iframe[title="Secure card number input frame"]',
            'iframe[title="Secure expiration date input frame"]',
            'iframe[title="Secure CVC input frame"]',
            'iframe[title*="Secure card"]',
            'iframe[title*="card number"]',
            'iframe[title*="expiration"]',
            'iframe[title*="CVC"]',
            'iframe[src*="js.stripe.com"]',
            'iframe[name^="__privateStripeFrame"]',
            ".StripeElement",
            "[class*='StripeElement']",
        ]
        for selector in stripe_selectors:
            try:
                if await page.locator(selector).first.is_visible(timeout=250):
                    return True
            except Exception:
                pass
        for selector in (
            'iframe[src*="js.stripe.com"]',
            'iframe[name^="__privateStripeFrame"]',
            ".StripeElement",
            "[class*='StripeElement']",
        ):
            try:
                if await page.locator(selector).count():
                    return True
            except Exception:
                pass
        return False

    async def fill_stripe_input(self, page: Page, selectors: list[str], value: str, field_name: str) -> None:
        frame_selectors = [
            'iframe[title="Secure card number input frame"]',
            'iframe[title="Secure expiration date input frame"]',
            'iframe[title="Secure CVC input frame"]',
            'iframe[title*="Secure"]',
            'iframe[src*="js.stripe.com"]',
            'iframe[name^="__privateStripeFrame"]',
        ]
        for frame_selector in frame_selectors:
            try:
                frame_count = await page.locator(frame_selector).count()
            except Exception:
                frame_count = 0
            for index in range(frame_count):
                frame = page.frame_locator(frame_selector).nth(index)
                for selector in selectors:
                    locator = frame.locator(selector).first
                    try:
                        await locator.wait_for(timeout=750)
                        await locator.click()
                        await locator.press_sequentially(value, delay=50)
                        return
                    except Exception:
                        continue
        raise RuntimeError(f"Could not find Stripe {field_name} input")

    async def sign_in_link_visible(self, page: Page) -> bool:
        locator = page.locator('[data-testid="sign-in-link"]').first
        try:
            return await locator.is_visible(timeout=2_000)
        except Exception:
            return False

    async def login(self, page: Page, date: str) -> bool:
        await page.goto(self.booking_url_for(date), wait_until="networkidle", timeout=30_000)
        await self.dismiss_cookie_banner(page)
        await self.shot(page, "club_01_booking_landing")

        await page.locator('[data-testid="sign-in-link"]').first.click(force=True)
        await page.wait_for_load_state("networkidle", timeout=30_000)
        await self.shot(page, "club_02_sign_in")

        await page.locator('button[name="idp"][value="LTA2"]').click()
        await page.locator('input[placeholder="Username"]').wait_for(timeout=30_000)
        await self.shot(page, "club_03_lta_login")

        await page.locator('input[placeholder="Username"]').fill(self.username)
        await page.locator('input[placeholder="Password"], input[type="password"]').fill(self.password)
        await page.locator('button[title="Log in"], button:has-text("Log in")').first.click()

        try:
            await page.wait_for_url(f"**/{self.venue}/**", timeout=30_000)
        except Exception:
            log.error(f"LTA login did not redirect back to {self.venue}")
            await self.shot(page, "club_04_login_failed")
            return False

        await page.wait_for_load_state("networkidle", timeout=30_000)
        await self.dismiss_cookie_banner(page)
        await self.shot(page, "club_04_after_login")

        if await self.sign_in_link_visible(page):
            log.error("Login appears to have failed — sign-in link is still visible")
            await self.shot(page, "club_04_login_failed")
            return False

        log.info("Login OK")
        return True

    async def goto_booking_page(self, page: Page, date: str) -> None:
        await page.goto(self.booking_url_for(date), wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(1)
        log.info(f"Booking page loaded ({date})")
        await self.shot(page, "club_05_booking_page")

    async def select_duration(self, page: Page, booking_time: str) -> bool:
        if self.booking_duration_minutes <= 30:
            return True

        end_time = self.booking_end_time(booking_time)
        container = page.locator("#select2-booking-duration-container")
        try:
            await container.wait_for(timeout=8_000)
        except Exception:
            log.error("Booking duration dropdown not found")
            return False

        current = await container.get_attribute("title")
        if current == end_time:
            return True

        await container.click()
        option = page.locator(".select2-results__option", has_text=end_time).first
        try:
            await option.wait_for(timeout=5_000)
            await option.click()
            log.info(f"Booking duration set to 1 hour (end {end_time})")
            await self.shot(page, "club_07_duration_selected", full_page=False)
            return True
        except Exception as exc:
            log.error(f"Could not set booking duration to {end_time}: {exc}")
            return False

    async def click_slot(self, page: Page, booking_time: str) -> bool:
        slot_minutes = self.booking_minutes(booking_time)
        any_sel = f'a.book-interval.not-booked[data-test-id$="|{slot_minutes}"]'
        try:
            await page.wait_for_selector(any_sel, timeout=20_000)
        except Exception:
            log.error(f"No slot at {booking_time}")
            await self.shot(page, "club_06_no_slot_found")
            return False

        if self.preferred_courts:
            for court_num in self.preferred_courts:
                resource_id = self.resource_ids_by_court.get(court_num)
                if not resource_id:
                    continue
                selector = (
                    f'a.book-interval.not-booked[data-test-id^="booking-{resource_id}"]'
                    f'[data-test-id$="|{slot_minutes}"]'
                )
                el = await page.query_selector(selector)
                if el:
                    await el.click()
                    log.info(f"Preferred slot clicked at {booking_time} (court {court_num})")
                    await self.shot(page, "club_06_clicked_slot")
                    return await self.select_duration(page, booking_time)

            log.error(f"No preferred court slot found at {booking_time} for courts {self.preferred_courts}")
            await self.shot(page, "club_06_no_slot_found")
            return False

        fallback = await page.query_selector(any_sel)
        if not fallback:
            log.error(f"No clickable slot found at {booking_time}")
            await self.shot(page, "club_06_no_slot_found")
            return False

        await fallback.click()
        log.info(f"Fallback slot clicked at {booking_time}")
        await self.shot(page, "club_06_clicked_slot")
        return await self.select_duration(page, booking_time)

    async def confirm_popup(self, page: Page) -> bool:
        try:
            await page.wait_for_selector("#submit-booking", timeout=8_000)
            await self.shot(page, "club_08_popup")
        except Exception:
            log.error("'Continue booking' not found")
            return False

        await self.dismiss_cookie_banner(page)
        await page.click("#submit-booking")
        log.info("Continue booking clicked")

        try:
            await page.wait_for_selector("#paynow", timeout=20_000)
        except Exception:
            try:
                await page.wait_for_url("**/Booking/Book**", timeout=5_000)
                await page.wait_for_selector("#paynow", timeout=15_000)
            except Exception as exc:
                log.error(f"Booking confirmation did not appear after Continue booking: {exc}")
                await self.shot(page, "club_09_confirmation_error", full_page=False)
                return False

        await self.shot(page, "club_09_booking_confirmation")
        return True

    async def payment_state(self, page: Page) -> tuple[str, str]:
        confirmation_selector = (
            "h1:has-text('Confirmed'), h2:has-text('Confirmed'), "
            "h1:has-text('Thank you'), p:has-text('successfully booked'), "
            ".booking-confirmed, .alert-success"
        )
        loading_selector = (
            ".loading, .spinner, .loading-spinner, .cs-loading, .blockUI, "
            "[aria-busy='true'], [data-testid*='loading']"
        )
        paynow = page.locator("#paynow").first
        await self.dismiss_cookie_banner(page)
        try:
            if await page.locator(confirmation_selector).first.is_visible(timeout=250):
                return "confirmed", "confirmation page visible"
        except Exception:
            pass
        if await self.stripe_is_ready(page):
            return "stripe_ready", "stripe elements detected"
        fatal_texts = [
            "something went wrong",
            "payment failed",
            "unable to process",
            "session expired",
            "try again later",
            "technical issue",
            "error",
        ]
        try:
            body_text = (await page.locator("body").inner_text(timeout=500)).lower()
        except Exception:
            body_text = ""
        messages = await self.visible_messages(page)
        if messages:
            error_like = [
                message for message in messages
                if any(word in message.lower() for word in ["error", "failed", "unable", "required", "select", "choose"])
            ]
            if error_like:
                return "fatal_error", error_like[0]
        for text in fatal_texts:
            if text in body_text:
                return "fatal_error", text
        try:
            if await page.locator(loading_selector).first.is_visible(timeout=250):
                return "loading", "visible loading indicator"
        except Exception:
            pass
        if any(text in body_text for text in ["loading", "processing", "please wait", "creating payment"]):
            return "loading", "page text indicates payment is still loading"
        visible, disabled, classes = await self.paynow_status(page)
        if visible:
            if disabled is not None:
                return "loading", f"confirm and pay button is disabled ({disabled})"
            if classes and "disabled" in classes.lower():
                return "loading", f"confirm and pay button class indicates disabled ({classes})"
            if messages:
                return "paynow_visible", f"confirm and pay still visible; messages={messages[0]}"
            return "paynow_visible", "confirm and pay button still visible"
        return "waiting", "payment session pending"

    async def wait_for_payment_ready(self, page: Page) -> str:
        deadline = monotonic() + 180
        last_log_at = 0.0
        retried_paynow = False
        paynow = page.locator("#paynow").first
        while monotonic() < deadline:
            state, detail = await self.payment_state(page)
            now = monotonic()
            if state == "stripe_ready":
                log.info("Stripe session ready")
                return "stripe_ready"
            if state == "confirmed":
                log.info("Booking confirmed before Stripe form appeared")
                return "confirmed"
            if state == "fatal_error":
                log.error(f"Payment page reported an error: {detail}")
                await self.shot(page, "club_10_payment_error", full_page=False)
                return "fatal_error"
            if state == "paynow_visible" and not retried_paynow and now + 30 < deadline:
                try:
                    await paynow.click(force=True, timeout=5_000)
                    retried_paynow = True
                    log.warning("Payment session did not advance; retried Confirm and pay once")
                except Exception as exc:
                    log.warning(f"Could not retry Confirm and pay: {exc}")
            if now - last_log_at >= 5:
                log.info(f"Waiting for payment session: {state} ({detail})")
                last_log_at = now
            await asyncio.sleep(0.5)
        try:
            url = page.url
        except Exception:
            url = "<unavailable>"
        try:
            body = (await page.locator("body").inner_text(timeout=1_000))[:400].replace("\n", " ")
        except Exception:
            body = "<could not read page text>"
        messages = await self.visible_messages(page)
        visible, disabled, classes = await self.paynow_status(page)
        log.error(
            f"Payment session timed out after prolonged wait. url={url} "
            f"paynow_visible={visible} paynow_disabled={disabled} paynow_classes={classes} "
            f"messages={messages} body={body}"
        )
        await self.shot(page, "club_10_payment_timeout", full_page=False)
        if self._capture_live_diagnostics and not self._screenshots_enabled:
            await self.write_screenshot(page, self.live_timeout_shot_path, full_page=False)
        return "timeout"

    async def wait_for_booking_confirmation(self, page: Page) -> bool:
        deadline = monotonic() + 120
        confirmation_selector = (
            "h1:has-text('Confirmed'), h2:has-text('Confirmed'), "
            "h1:has-text('Thank you'), p:has-text('successfully booked'), "
            ".booking-confirmed, .alert-success"
        )
        fatal_texts = ["payment failed", "something went wrong", "technical issue", "error"]
        last_log_at = 0.0
        while monotonic() < deadline:
            try:
                if await page.locator(confirmation_selector).first.is_visible(timeout=250):
                    log.info("BOOKING CONFIRMED!")
                    return True
            except Exception:
                pass
            try:
                body_text = (await page.locator("body").inner_text(timeout=500)).lower()
            except Exception:
                body_text = ""
            for text in fatal_texts:
                if text in body_text:
                    log.error(f"Confirmation page reported an error: {text}")
                    await self.shot(page, "club_12_confirmation_error", full_page=False)
                    return False
            now = monotonic()
            if now - last_log_at >= 5:
                log.info("Waiting for final booking confirmation")
                last_log_at = now
            await asyncio.sleep(0.5)
        log.error("Confirmation not detected before timeout")
        await self.shot(page, "club_12_unknown")
        return False

    async def pay(self, page: Page, dry_run: bool) -> bool:
        try:
            await page.wait_for_selector("#paynow", timeout=20_000)
            await self.dismiss_cookie_banner(page)
            await page.locator("#paynow").click(force=True)
            log.info("Confirm and pay clicked")
            await self.shot(page, "club_10_after_confirm_and_pay", full_page=False)
        except Exception as exc:
            log.error(f"'Confirm and pay' not found: {exc}")
            await self.shot(page, "club_10_pay_error", full_page=False)
            return False

        payment_state = await self.wait_for_payment_ready(page)
        if payment_state == "confirmed":
            return True
        if payment_state != "stripe_ready":
            return False

        if not self.card_number:
            log.error("CARD_NUMBER not set")
            return False

        try:
            await self.fill_stripe_input(
                page,
                [
                    'input[placeholder*="1234"]',
                    'input[name="cardnumber"]',
                    'input[autocomplete="cc-number"]',
                    'input[inputmode="numeric"]',
                ],
                self.card_number,
                "card number",
            )
            await self.fill_stripe_input(
                page,
                [
                    'input[placeholder*="MM"]',
                    'input[name="exp-date"]',
                    'input[autocomplete="cc-exp"]',
                ],
                self.card_expiry,
                "expiry",
            )
            await self.fill_stripe_input(
                page,
                [
                    'input[name="cvc"]',
                    'input[placeholder*="CVC"]',
                    'input[autocomplete="cc-csc"]',
                ],
                self.card_cvv,
                "CVC",
            )
            log.info("Card filled")
            await asyncio.sleep(1)
            # Stripe/modal pages are not reliable screenshot targets.
            await self.shot(page, "club_11_card_filled", full_page=False)
        except Exception as exc:
            log.error(f"Card fill failed: {exc}")
            return False

        if dry_run:
            log.info("[DRY-RUN] Stopping before Pay — card filled but not submitted")
            return True

        pay_button = page.locator(
            "#cs-stripe-elements-submit-button, button[type='submit']:has-text('Pay')"
        ).first
        try:
            await pay_button.click(force=True, timeout=10_000)
            log.info("Pay clicked")
        except Exception as exc:
            log.error(f"Pay button failed: {exc}")
            return False

        return await self.wait_for_booking_confirmation(page)

    async def run(self, options: RunOptions) -> None:
        self.configure_venue(options.venue_override)
        self.configure_booking(options)
        self.configure_account(options.account_override)
        self.validate_environment()
        self._screenshots_enabled = options.debug
        self._capture_live_diagnostics = not options.debug

        timing = TimingHelper(self.tz_name, log)
        timing.sync_ntp()

        date = self.target_date(timing, options.date_override)
        booking_time = self.booking_time
        release_time = self.release_time(timing)
        wait_secs = timing.secs_until(release_time)

        dry_run = options.debug and not options.force_pay
        mode = "DRY-RUN" if dry_run else ("DEBUG+PAY" if options.debug else "LIVE")
        log.info(
            f"=== {self.name} | venue={self.venue} | account={self.account_name} | {date} {booking_time} | "
            f"opens {release_time.strftime('%H:%M:%S %Z')} | {mode} ==="
        )

        if not options.skip_wait and not options.debug:
            sleep_time = max(0.0, wait_secs - self.pre_login_secs)
            if sleep_time > 0:
                log.info(f"Sleeping {sleep_time:.0f}s (wake {self.pre_login_secs}s before release)")
                await asyncio.sleep(sleep_time)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
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

            net_entries = []
            if options.debug or self._capture_live_diagnostics:
                net_entries = self.attach_network_logger(page)
                await self.attach_network_response_logger(page, net_entries)

            try:
                if not await self.login(page, date):
                    return

                for pattern in [
                    "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                    "**/google-analytics.com/**",
                    "**/googletagmanager.com/**",
                    "**/region1.google-analytics.com/**",
                ]:
                    await page.route(pattern, lambda route: route.abort())

                await self.goto_booking_page(page, date)

                if not options.skip_wait and not options.debug:
                    remaining = timing.secs_until(release_time)
                    if remaining > 0:
                        log.info(f"Waiting {remaining:.1f}s")
                        while True:
                            remaining = timing.secs_until(release_time)
                            if remaining <= 0.2:
                                break
                            await asyncio.sleep(min(remaining - 0.2, 1.0))
                        while timing.secs_until(release_time) > 0:
                            pass
                    log.info(">>> RELEASE TIME <<<")
                else:
                    log.info("Skipping release-time wait.")

                slot_available = await self.wait_for_slot_via_api(page, booking_time, date)
                if not slot_available:
                    log.error("API booking path did not find a matching slot; stopping without UI fallback")
                    return

                log.info("Reloading booking page after API confirmation so the UI reflects the newly released slot")
                await page.reload(wait_until="commit", timeout=10_000)
                await page.wait_for_load_state("networkidle", timeout=20_000)
                await self.dismiss_cookie_banner(page)

                if not await self.click_slot(page, booking_time):
                    return

                if not await self.confirm_popup(page):
                    return

                await self.pay(page, dry_run=dry_run)
            finally:
                if options.debug:
                    self.save_network_log(net_entries)
                    try:
                        cookies = await context.cookies()
                        with open(self.cookies_path, "w") as handle:
                            _json.dump(cookies, handle, indent=2)
                        log.info(f"Cookies saved → {self.cookies_path.name}  ({len(cookies)} cookies)")
                    except Exception as exc:
                        log.warning(f"Could not save cookies: {exc}")
                elif net_entries:
                    self.save_network_log_to(net_entries, self.live_network_log_path)

                await browser.close()
