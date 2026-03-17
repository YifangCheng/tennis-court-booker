import asyncio
import json as _json
import os
from pathlib import Path

import pytz
from playwright.async_api import Page, async_playwright

from shared.runtime import ROOT, RunOptions, TimingHelper, get_logger, load_json, load_root_env
from sites.base import BookingSite

load_root_env()
log = get_logger()


class RaynesParkSite(BookingSite):
    name = "raynes_park"
    description = "AELTC Community Tennis Centre (Raynes Park)"
    env_prefix = "RAYNES_PARK"

    def __init__(self) -> None:
        self.site_dir = Path(__file__).resolve().parent
        self.shots_dir = ROOT / "screenshots"
        self.shots_dir.mkdir(exist_ok=True)
        self.network_log_path = ROOT / "network_log.json"
        self._screenshots_enabled = False

        cfg = load_json(self.site_dir / "config.json")
        self.cfg = cfg

        self.tz_name = cfg.get("timezone", "Europe/London")
        self.booking_date = cfg.get("booking_date")
        self.booking_time = cfg.get("booking_time", "17:00")
        self.preferred_courts = cfg.get("preferred_courts", [4, 5, 6, 7, 8, 9])
        self.court_type = cfg.get("court_type", "indoor")
        self.headless = cfg.get("headless", True)
        self.pre_login_secs = cfg.get("pre_login_seconds", 120)

        self.username = os.environ.get(f"{self.env_prefix}_BOOKING_USERNAME", "")
        self.password = os.environ.get(f"{self.env_prefix}_BOOKING_PASSWORD", "")
        self.card_number = os.environ.get("CARD_NUMBER", "")
        self.card_expiry = os.environ.get("CARD_EXPIRY", "")
        self.card_cvv = os.environ.get("CARD_CVV", "")

        self.base_url = "https://raynespark.communitysport.aeltc.com"
        self.auth_base = "https://auth.communitysport.aeltc.com"
        self.booking_url = f"{self.base_url}/Booking/BookByDate"
        self.court_resource_ids = {
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

    def validate_environment(self) -> None:
        if not self.username or not self.password:
            raise SystemExit(
                f"{self.env_prefix}_BOOKING_USERNAME and {self.env_prefix}_BOOKING_PASSWORD must be set in .env"
            )

    def build_login_url(self, timing: TimingHelper) -> str:
        now = timing.now_true().astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_out = now.replace(":", "%3a")
        ts_in = now.replace(":", "%253a")
        return_url = (
            "%2fissue%2fwsfed%3fwa%3dwsignin1.0"
            "%26wtrealm%3dhttps%253a%252f%252fraynespark.communitysport.aeltc.com"
            "%26wctx%3drm%253d0%2526id%253d0%2526ru%253dhttps%25253a%25252f%25252fraynespark.communitysport.aeltc.com"
            f"%26wct%3d{ts_in}"
            "%26prealm%3dhttps%253a%252f%252fraynespark.communitysport.aeltc.com"
            "%26error%3dFalse%26message%3d%26hf%3d13%26bf%3d14%26source%3draynespark_communitysport_aeltc_com"
        )
        return (
            f"{self.auth_base}/account/signin"
            f"?ReturnUrl={return_url}"
            f"&wa=wsignin1.0"
            f"&wtrealm=https%3a%2f%2fraynespark.communitysport.aeltc.com"
            f"&wctx=rm%3d0%26id%3d0%26ru%253dhttps%253a%252f%252fraynespark.communitysport.aeltc.com"
            f"&wct={ts_out}"
            f"&prealm=https%3a%2f%2fraynespark.communitysport.aeltc.com"
            f"&error=False&message=&hf=13&bf=14&source=raynespark_communitysport_aeltc_com"
        )

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

    async def dismiss_cookie_banner(self, page: Page) -> None:
        try:
            await page.evaluate("document.querySelector('.osano-cm-dialog')?.remove()")
            await page.evaluate("document.querySelector('.osano-cm-overlay')?.remove()")
        except Exception:
            pass

    async def login(self, page: Page, timing: TimingHelper) -> bool:
        await page.goto(self.build_login_url(timing), wait_until="networkidle", timeout=30_000)
        await page.wait_for_selector("#EmailAddress", timeout=15_000)
        await self.shot(page, "01_login_page")
        await page.fill("#EmailAddress", self.username)
        await page.fill("#Password", self.password)
        await page.click('button[type="submit"], input[type="submit"]')
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await self.shot(page, "02_after_login")
        if "signin" in page.url.lower() or "login" in page.url.lower():
            log.error("Login failed — check credentials")
            return False
        log.info("Login OK")
        return True

    async def goto_booking_page(self, page: Page, date: str) -> None:
        await page.goto(f"{self.booking_url}#?date={date}&role=member", wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(1.5)
        log.info(f"Booking page loaded ({date})")
        await self.shot(page, "03_booking_page")

    async def click_slot(self, page: Page, booking_time: str) -> bool:
        hours, minutes = booking_time.split(":")
        slot_minutes = int(hours) * 60 + int(minutes)
        resource_ids = []
        for court_num in self.preferred_courts:
            rid = self.court_resource_ids.get((self.court_type, court_num))
            if rid:
                resource_ids.append((court_num, rid))
        if not resource_ids:
            log.error(f"No resource IDs for court_type={self.court_type} courts={self.preferred_courts}")
            return False

        any_sel = f'a.book-interval.not-booked[data-test-id$="|{slot_minutes}"]'
        try:
            await page.wait_for_selector(any_sel, timeout=15_000)
        except Exception:
            log.error(f"No slot at {booking_time}")
            await self.shot(page, "04_no_slot_found")
            return False

        for court_num, rid in resource_ids:
            sel = f'a.book-interval.not-booked[data-test-id^="booking-{rid}"][data-test-id$="|{slot_minutes}"]'
            el = await page.query_selector(sel)
            if el:
                await el.click()
                log.info(f"Slot clicked — Court {court_num} ({self.court_type}) at {booking_time}")
                await self.shot(page, "04_clicked_slot")
                return True

        log.error(
            f"No available {self.court_type} slot at {booking_time} for courts {self.preferred_courts}"
        )
        await self.shot(page, "04_no_slot_found")
        return False

    async def confirm_popup(self, page: Page) -> bool:
        try:
            await page.wait_for_selector("#submit-booking", timeout=8_000)
            await self.shot(page, "05_popup")
        except Exception:
            log.error("'Continue booking' not found")
            await self.shot(page, "05_popup")
            return False

        async with page.expect_navigation(timeout=20_000):
            await page.click("#submit-booking")
        log.info("Continue booking → /Booking/Book")
        await self.shot(page, "06_booking_page")
        return True

    async def pay(self, page: Page, dry_run: bool) -> bool:
        await self.dismiss_cookie_banner(page)
        try:
            await page.wait_for_selector("button:has-text('Confirm and pay')", timeout=45_000)
            await self.dismiss_cookie_banner(page)
            await page.locator("button:has-text('Confirm and pay')").click(force=True)
            log.info("Confirm and pay clicked")
        except Exception as exc:
            log.error(f"'Confirm and pay' not found: {exc}")
            try:
                path = self.shots_dir / "07_error.png"
                await page.screenshot(path=str(path), full_page=False, timeout=3_000)
                log.error(f"Page text: {(await page.inner_text('body'))[:500]}")
            except Exception:
                pass
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
            await self.shot(page, "09_card_filled", full_page=False)
        except Exception as exc:
            log.error(f"Card fill failed: {exc}")
            return False

        if dry_run:
            log.info("[DRY-RUN] Stopping before Pay — card filled but not submitted")
            return True

        await self.dismiss_cookie_banner(page)
        await self.shot(page, "08_before_pay", full_page=False)
        try:
            await page.locator("#cs-stripe-elements-submit-button").click(force=True, timeout=10_000)
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
            await self.shot(page, "10_unknown")
            return False

    async def run(self, options: RunOptions) -> None:
        self.validate_environment()
        self._screenshots_enabled = options.debug

        timing = TimingHelper(self.tz_name, log)
        timing.sync_ntp()

        date = options.date_override or self.booking_date or timing.target_date()
        booking_time = options.time_override or self.booking_time
        midnight = timing.midnight_tonight()
        wait_secs = timing.secs_until(midnight)

        dry_run = options.debug and not options.force_pay
        mode = "DRY-RUN" if dry_run else ("DEBUG+PAY" if options.debug else "LIVE")
        log.info(f"=== {self.name} | {date} {booking_time} | opens {midnight.strftime('%H:%M:%S %Z')} | {mode} ===")

        if not options.skip_wait and not options.debug:
            sleep_time = max(0.0, wait_secs - self.pre_login_secs)
            if sleep_time > 0:
                log.info(f"Sleeping {sleep_time:.0f}s (wake {self.pre_login_secs}s before midnight)")
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

            if not await self.login(page, timing):
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
                remaining = timing.secs_until(midnight)
                if remaining > 0:
                    log.info(f"Waiting {remaining:.1f}s")
                    while True:
                        remaining = timing.secs_until(midnight)
                        if remaining <= 0.2:
                            break
                        await asyncio.sleep(min(remaining - 0.2, 1.0))
                    while timing.secs_until(midnight) > 0:
                        pass
                log.info(">>> MIDNIGHT <<<")
                await page.reload(wait_until="commit", timeout=10_000)
            else:
                log.info("Skipping midnight wait.")

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
                    cookie_path = ROOT / "debug_cookies.json"
                    with open(cookie_path, "w") as handle:
                        _json.dump(cookies, handle, indent=2)
                    log.info(f"Cookies saved → {cookie_path.name}  ({len(cookies)} cookies)")
                except Exception as exc:
                    log.warning(f"Could not save cookies: {exc}")

            await browser.close()
