import asyncio
import json as _json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz
from playwright.async_api import Page, async_playwright

from shared.runtime import ROOT, RunOptions, TimingHelper, get_logger, load_json, load_root_env
from sites.base import BookingSite

load_root_env()
log = get_logger()


class ClubSparkSite(BookingSite):
    name = "club_spark"
    description = "Tanner St Park on ClubSpark"
    env_prefix = "CLUB_SPARK"

    def __init__(self) -> None:
        self.site_dir = Path(__file__).resolve().parent
        self.shots_dir = ROOT / "screenshots"
        self.shots_dir.mkdir(exist_ok=True)
        self.network_log_path = ROOT / "network_log.club_spark.json"
        self.cookies_path = ROOT / "debug_cookies.club_spark.json"
        self._screenshots_enabled = False
        self.resource_ids_by_court = {}

        cfg = load_json(self.site_dir / "config.json")
        self.cfg = cfg

        self.tz_name = cfg.get("timezone", "Europe/London")
        self.booking_date = cfg.get("booking_date")
        self.booking_time = cfg.get("booking_time", "10:00")
        self.booking_duration_minutes = int(cfg.get("booking_duration_minutes", 60))
        self.preferred_courts = cfg.get("preferred_courts", [3])
        self.headless = cfg.get("headless", True)
        self.pre_login_secs = int(cfg.get("pre_login_seconds", 120))
        self.release_hour = int(cfg.get("release_hour", 20))
        self.release_minute = int(cfg.get("release_minute", 0))

        self.username = os.environ.get(f"{self.env_prefix}_BOOKING_USERNAME", "")
        self.password = os.environ.get(f"{self.env_prefix}_BOOKING_PASSWORD", "")
        self.card_number = os.environ.get("CARD_NUMBER", "")
        self.card_expiry = os.environ.get("CARD_EXPIRY", "")
        self.card_cvv = os.environ.get("CARD_CVV", "")

        self.base_url = "https://clubspark.lta.org.uk/TannerStPark"
        self.api_base_url = "https://clubspark.lta.org.uk"

    def validate_environment(self) -> None:
        if not self.username or not self.password:
            raise SystemExit(
                f"{self.env_prefix}_BOOKING_USERNAME and {self.env_prefix}_BOOKING_PASSWORD must be set in .env"
            )

    def booking_url_for(self, date: str) -> str:
        return f"{self.base_url}/Booking/BookByDate#?date={date}&role=guest"

    def release_time(self, timing: TimingHelper) -> datetime:
        now = timing.now_true()
        release = now.replace(
            hour=self.release_hour,
            minute=self.release_minute,
            second=0,
            microsecond=0,
        )
        if release <= now:
            release += timedelta(days=1)
        return release

    def target_date(self, timing: TimingHelper, override: Optional[str]) -> str:
        if override:
            return override
        if self.booking_date:
            return self.booking_date
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
        try:
            await page.screenshot(path=str(path), full_page=full_page, timeout=5_000)
            log.info(f"  Screenshot → screenshots/{name}.png")
        except Exception as exc:
            if full_page:
                try:
                    await page.screenshot(path=str(path), full_page=False, timeout=3_000)
                    log.info(f"  Screenshot (viewport) → screenshots/{name}.png")
                except Exception as fallback_exc:
                    log.warning(f"  Screenshot '{name}' failed (non-fatal): {fallback_exc}")
            else:
                log.warning(f"  Screenshot '{name}' failed (non-fatal): {exc}")

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
                    "headers": dict(request.headers),
                    "post_data": body,
                    "responses": [],
                }
            )

        page.on("request", _on_request)
        return entries

    async def attach_network_response_logger(self, page: Page, entries: list) -> None:
        async def _on_response(response) -> None:
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

    def save_network_log(self, entries: list) -> None:
        with open(self.network_log_path, "w") as handle:
            _json.dump(entries, handle, indent=2)
        log.info(f"Network log → {self.network_log_path.name} ({len(entries)} entries)")

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
        return await self.api_get_json(page, f"{self.api_base_url}/v0/VenueBooking/TannerStPark/GetSettings")

    async def get_venue_sessions(self, page: Page, date: str) -> dict:
        url = (
            f"{self.api_base_url}/v0/VenueBooking/TannerStPark/GetVenueSessions"
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
            await page.wait_for_url("**/TannerStPark/**", timeout=30_000)
        except Exception:
            log.error("LTA login did not redirect back to Tanner St Park")
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

        async with page.expect_navigation(timeout=20_000):
            await page.click("#submit-booking")
        log.info("Continue booking clicked")
        await self.shot(page, "club_09_booking_confirmation")
        return True

    async def pay(self, page: Page, dry_run: bool) -> bool:
        try:
            await page.wait_for_selector("#paynow", timeout=20_000)
            await page.locator("#paynow").click(force=True)
            log.info("Confirm and pay clicked")
        except Exception as exc:
            log.error(f"'Confirm and pay' not found: {exc}")
            await self.shot(page, "club_10_pay_error", full_page=False)
            return False

        try:
            await page.wait_for_selector('iframe[title="Secure card number input frame"]', timeout=10_000)
        except Exception as exc:
            log.error(f"Stripe form not found: {exc}")
            return False

        if not self.card_number:
            log.error("CARD_NUMBER not set")
            return False

        try:
            num_input = page.frame_locator('iframe[title="Secure card number input frame"]').locator(
                'input[placeholder*="1234"]'
            )
            await num_input.click()
            await num_input.press_sequentially(self.card_number, delay=50)

            exp_input = page.frame_locator('iframe[title="Secure expiration date input frame"]').locator(
                'input[placeholder*="MM"]'
            )
            await exp_input.click()
            await exp_input.press_sequentially(self.card_expiry, delay=50)

            cvc_input = page.frame_locator('iframe[title="Secure CVC input frame"]').locator(
                'input[name="cvc"]'
            )
            await cvc_input.click()
            await cvc_input.press_sequentially(self.card_cvv, delay=50)
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
            await self.shot(page, "club_12_unknown")
            return False

    async def run(self, options: RunOptions) -> None:
        self.validate_environment()
        self._screenshots_enabled = options.debug

        timing = TimingHelper(self.tz_name, log)
        timing.sync_ntp()

        date = self.target_date(timing, options.date_override)
        booking_time = options.time_override or self.booking_time
        release_time = self.release_time(timing)
        wait_secs = timing.secs_until(release_time)

        dry_run = options.debug and not options.force_pay
        mode = "DRY-RUN" if dry_run else ("DEBUG+PAY" if options.debug else "LIVE")
        log.info(
            f"=== {self.name} | {date} {booking_time} | opens {release_time.strftime('%H:%M:%S %Z')} | {mode} ==="
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
            if options.debug:
                net_entries = self.attach_network_logger(page)
                await self.attach_network_response_logger(page, net_entries)

            if not await self.login(page, date):
                await browser.close()
                return

            if not options.debug:
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
                await browser.close()
                return

            log.info("Reloading booking page after API confirmation so the UI reflects the newly released slot")
            await page.reload(wait_until="commit", timeout=10_000)
            await page.wait_for_load_state("networkidle", timeout=20_000)
            await self.dismiss_cookie_banner(page)

            if not await self.click_slot(page, booking_time):
                await browser.close()
                return

            if not await self.confirm_popup(page):
                await browser.close()
                return

            await self.pay(page, dry_run=dry_run)

            if options.debug:
                self.save_network_log(net_entries)
                try:
                    cookies = await context.cookies()
                    with open(self.cookies_path, "w") as handle:
                        _json.dump(cookies, handle, indent=2)
                    log.info(f"Cookies saved → {self.cookies_path.name}  ({len(cookies)} cookies)")
                except Exception as exc:
                    log.warning(f"Could not save cookies: {exc}")

            await browser.close()
