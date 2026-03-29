import asyncio
import html as _html
import json as _json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

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
        self.logs_dir = ROOT / "logs"
        self.logs_dir.mkdir(exist_ok=True)
        self.network_log_path = ROOT / "network_log.club_spark.json"
        self.live_network_log_path = ROOT / "network_log.club_spark.live.json"
        self.cookies_path = ROOT / "debug_cookies.club_spark.json"
        self.live_timeout_shot_path = self.shots_dir / "club_10_payment_timeout_live.png"
        self._screenshots_enabled = False
        self._capture_live_diagnostics = False
        self._network_entries: list[dict] = []

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
        self.precreate_stripe_pm_lead_seconds = float(
            os.environ.get("CLUB_SPARK_PRECREATE_STRIPE_PM_LEAD_SECONDS", "15")
        )
        self.precreated_stripe_pm_max_age_seconds = float(
            os.environ.get("CLUB_SPARK_PRECREATED_STRIPE_PM_MAX_AGE_SECONDS", "90")
        )
        self.precreate_booking_payment_lead_seconds = float(
            os.environ.get("CLUB_SPARK_PRECREATE_BOOKING_PAYMENT_LEAD_SECONDS", "3")
        )
        self.precreated_booking_payment_max_age_seconds = float(
            os.environ.get("CLUB_SPARK_PRECREATED_BOOKING_PAYMENT_MAX_AGE_SECONDS", "30")
        )
        self.request_timeout_ms = int(
            float(os.environ.get("CLUB_SPARK_REQUEST_TIMEOUT_SECONDS", "30")) * 1000
        )
        self.page_timeout_ms = int(
            float(os.environ.get("CLUB_SPARK_PAGE_TIMEOUT_SECONDS", "20")) * 1000
        )

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

    def artifact_label(self, run_date_stamp: str) -> str:
        venue_label = re.sub(r"[^A-Za-z0-9_-]+", "_", self.venue or "venue").strip("_") or "venue"
        account_label = re.sub(r"[^A-Za-z0-9_-]+", "_", self.account_name or "account").strip("_") or "account"
        return f"{self.name}_{account_label}_{venue_label}_{run_date_stamp}"

    def configure_artifact_paths(self, run_date_stamp: str) -> None:
        label = self.artifact_label(run_date_stamp)
        self.network_log_path = self.logs_dir / f"network_log_{label}.json"
        self.live_network_log_path = self.logs_dir / f"network_log_{label}.live.json"
        self.cookies_path = self.logs_dir / f"debug_cookies_{label}.json"

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

    def booking_minutes(self, booking_time: str) -> int:
        hours, minutes = booking_time.split(":")
        return int(hours) * 60 + int(minutes)

    def minutes_to_time(self, total_minutes: int) -> str:
        return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"

    def money_text(self, value: float) -> str:
        return f"{value:.2f}"

    def build_slot_details(self, resource: dict, session: dict, date: str, booking_time: str) -> dict:
        start_minutes = self.booking_minutes(booking_time)
        end_minutes = start_minutes + self.booking_duration_minutes
        interval_minutes = max(1, int(session.get("Interval") or 30))
        interval_count = max(1, (self.booking_duration_minutes + interval_minutes - 1) // interval_minutes)
        court_cost_per_interval = float(session.get("CourtCost", session.get("Cost", 0.0)) or 0.0)
        lighting_cost_per_interval = float(session.get("LightingCost", 0.0) or 0.0)
        court_cost = round(court_cost_per_interval * interval_count, 2)
        lighting_cost = round(lighting_cost_per_interval * interval_count, 2)
        total_cost = round(court_cost + lighting_cost, 2)

        return {
            "date": date,
            "resource_id": resource.get("ID", ""),
            "resource_group_id": resource.get("ResourceGroupID", ""),
            "resource_name": resource.get("Name", ""),
            "court_number": self.court_number(resource),
            "session_id": session.get("ID", ""),
            "category": int(session.get("Category", 0) or 0),
            "sub_category": int(session.get("SubCategory", 0) or 0),
            "start_time": start_minutes,
            "end_time": end_minutes,
            "interval_minutes": interval_minutes,
            "interval_count": interval_count,
            "court_cost": court_cost,
            "lighting_cost": lighting_cost,
            "total_cost": total_cost,
        }

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
            is_direct_booking_doc = (
                request.resource_type == "document"
                and request.method == "GET"
                and "/Booking/Book?" in request.url
            )
            if not (is_xhr or is_post_doc or is_direct_booking_doc):
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

    async def attach_network_response_logger(self, page: Page, entries: list, capture_bodies: bool = True) -> None:
        async def _on_response(response) -> None:
            req = None
            try:
                req = response.request
                is_xhr = req.resource_type in ("xhr", "fetch")
                is_post_doc = req.resource_type == "document" and req.method != "GET"
                is_direct_booking_doc = (
                    req.resource_type == "document"
                    and req.method == "GET"
                    and "/Booking/Book?" in req.url
                )
                if not (is_xhr or is_post_doc or is_direct_booking_doc):
                    return
                body = None
                if capture_bodies:
                    body = await response.text()
            except TargetClosedError:
                return
            except Exception:
                if req is None:
                    return
                body = "<could not read body>" if capture_bodies else None
            for entry in reversed(entries):
                if entry["url"] == response.url and entry["method"] == req.method:
                    response_entry = {"status": response.status}
                    if capture_bodies:
                        response_entry["body"] = body[:50000] if isinstance(body, str) else body
                    entry["responses"].append(response_entry)
                    break

        page.on("response", _on_response)

    def save_network_log(self, entries: list) -> None:
        self.save_network_log_to(entries, self.network_log_path)

    def save_network_log_to(self, entries: list, path: Path) -> None:
        with open(path, "w") as handle:
            _json.dump(entries, handle, indent=2)
        log.info(f"Network log → {path.name} ({len(entries)} entries)")

    def latest_network_entry(self, url_fragment: str) -> Optional[dict]:
        for entry in reversed(self._network_entries):
            if url_fragment in entry.get("url", ""):
                return entry
        return None

    def latest_network_post_params(self, url_fragment: str) -> dict[str, str]:
        entry = self.latest_network_entry(url_fragment)
        if not entry:
            return {}
        post_data = entry.get("post_data") or ""
        params = parse_qs(post_data)
        return {key: values[-1] for key, values in params.items() if values}

    def latest_network_response_json(self, url_fragment: str) -> Optional[dict]:
        entry = self.latest_network_entry(url_fragment)
        if not entry:
            return None
        for response in reversed(entry.get("responses", [])):
            body = response.get("body") or ""
            if not body or body == "<could not read body>":
                continue
            try:
                return _json.loads(body)
            except Exception:
                continue
        return None

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

    async def get_current_user(self, page: Page) -> dict:
        return await self.api_get_json(page, f"{self.api_base_url}/v2/User/GetCurrentUser")

    def venue_contact_for_user(self, user: dict, venue_id: str) -> Optional[dict]:
        for contact in user.get("VenueContacts", []):
            if contact.get("VenueID") == venue_id or contact.get("VenueUrlSegment") == self.venue:
                return {
                    "first_name": user.get("FirstName", ""),
                    "last_name": user.get("LastName", ""),
                    "email": user.get("EmailAddress", ""),
                    "venue_contact_id": contact.get("VenueContactID", ""),
                    "venue_id": contact.get("VenueID", ""),
                    "venue_name": contact.get("VenueName", self.venue),
                }
        return None

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

    def session_is_available(self, session: dict, start_minutes: int) -> bool:
        if not self.session_matches_time(session, start_minutes):
            return False
        if session.get("Capacity", 0) <= 0:
            return False
        return True

    def session_matches_time(self, session: dict, start_minutes: int) -> bool:
        if session.get("Category") != 0:
            return False
        session_start = int(session.get("StartTime", -1))
        session_end = int(session.get("EndTime", -1))
        if start_minutes < session_start:
            return False
        if start_minutes + self.booking_duration_minutes > session_end:
            return False
        return True

    def find_slot_from_sessions(self, sessions: dict, booking_time: str, require_availability: bool = True):
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
                    matches = (
                        self.session_is_available(session, start_minutes)
                        if require_availability
                        else self.session_matches_time(session, start_minutes)
                    )
                    if matches:
                        return resource, session

        return None, None

    async def wait_for_slot_via_api(self, page: Page, booking_time: str, date: str) -> Optional[dict]:
        try:
            sessions = await self.get_venue_sessions(page, date)
        except Exception as exc:
            log.error(f"Could not load ClubSpark booking API data: {exc}")
            return None

        resource, session = self.find_slot_from_sessions(sessions, booking_time)
        if not resource or not session:
            log.error(f"No API slot found at {booking_time}")
            return None

        log.info(
            f"API slot found at {booking_time} on resource {resource['ID']} (session {session['ID']})"
        )
        return self.build_slot_details(resource, session, date, booking_time)

    async def prefetch_slot_candidate(self, page: Page, booking_time: str, date: str) -> Optional[dict]:
        try:
            sessions = await self.get_venue_sessions(page, date)
        except Exception as exc:
            log.warning(f"Could not prefetch ClubSpark slot candidate: {exc}")
            return None

        resource, session = self.find_slot_from_sessions(sessions, booking_time, require_availability=False)
        if not resource or not session:
            log.warning(f"Could not prefetch slot candidate at {booking_time}")
            return None

        log.info(
            f"Prefetched slot candidate at {booking_time} on resource {resource['ID']} (session {session['ID']})"
        )
        return self.build_slot_details(resource, session, date, booking_time)

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

    def direct_booking_url(self, slot: dict) -> str:
        query = urlencode(
            {
                "Contacts[0].IsPrimary": "true",
                "Contacts[0].IsJunior": "false",
                "Contacts[0].IsPlayer": "true",
                "ResourceID": slot["resource_id"],
                "Date": slot["date"],
                "SessionID": slot["session_id"],
                "StartTime": str(slot["start_time"]),
                "EndTime": str(slot["end_time"]),
                "Category": str(slot["category"]),
                "SubCategory": str(slot["sub_category"]),
                "VenueID": slot["resource_group_id"],
                "ResourceGroupID": slot["resource_group_id"],
            }
        )
        return f"{self.base_url}/Booking/Book?{query}"

    def stripe_runtime_from_content(self, content: str) -> Optional[dict]:
        key = None
        stripe_account = None
        stripe_js_id = None
        key_match = re.search(r"pk_(?:live|test)_[A-Za-z0-9]+", content)
        if key_match:
            key = key_match.group(0)
        account_match = re.search(r"acct_[A-Za-z0-9]+", content)
        if account_match:
            stripe_account = account_match.group(0)
        session_match = re.search(
            r"(?:stripe_js_id|clientSessionId|client_session_id)[^A-Za-z0-9-]+([0-9a-fA-F-]{16,})",
            content,
            flags=re.IGNORECASE,
        )
        if session_match:
            stripe_js_id = session_match.group(1)
        if not key or not stripe_account:
            return None
        return {
            "key": key,
            "stripe_account": stripe_account,
            "stripe_js_id": stripe_js_id,
        }

    def booking_unsuccessful_reason(self, url: str) -> Optional[str]:
        if "BookingUnsuccessful" not in url:
            return None
        parsed = urlsplit(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        return params.get("reason", ["unknown"])[-1] or "unknown"

    def input_value_from_html(self, content: str, name: str) -> Optional[str]:
        patterns = [
            rf'<input[^>]*name=["\']{re.escape(name)}["\'][^>]*value=["\']([^"\']+)["\']',
            rf'<input[^>]*value=["\']([^"\']+)["\'][^>]*name=["\']{re.escape(name)}["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, content, flags=re.IGNORECASE)
            if match:
                return _html.unescape(match.group(1))
        return None

    async def fetch_direct_booking_bootstrap(self, page: Page, slot: dict) -> Optional[dict]:
        url = self.direct_booking_url(slot)
        try:
            response = await page.context.request.get(
                url,
                headers={
                    "accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
                        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
                    ),
                    "referer": self.booking_url_for(slot["date"]),
                },
                timeout=self.request_timeout_ms,
                fail_on_status_code=False,
            )
        except Exception:
            return None

        try:
            content = await response.text()
        except Exception:
            content = ""

        if response.status != 200:
            return {
                "response_url": response.url,
                "rejection_reason": self.booking_unsuccessful_reason(response.url),
                "verification_token": None,
                "stripe_runtime": None,
            }

        return {
            "response_url": response.url,
            "rejection_reason": self.booking_unsuccessful_reason(response.url),
            "verification_token": self.input_value_from_html(content, "__RequestVerificationToken"),
            "stripe_runtime": self.stripe_runtime_from_content(content),
        }

    async def direct_booking_bootstrap_from_page(self, page: Page) -> dict:
        verification_token = await self.request_verification_token(page)
        stripe_runtime = await self.stripe_runtime(page)
        return {
            "response_url": page.url,
            "rejection_reason": self.booking_unsuccessful_reason(page.url),
            "verification_token": verification_token,
            "stripe_runtime": stripe_runtime,
        }

    async def goto_direct_booking_page(self, page: Page, slot: dict) -> bool:
        url = self.direct_booking_url(slot)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
            await self.dismiss_cookie_banner(page)
            rejection_reason = self.booking_unsuccessful_reason(page.url)
            if rejection_reason:
                log.error(f"Direct booking page rejected by ClubSpark: reason={rejection_reason} url={page.url}")
                return False
            log.info("Direct booking page loaded")
            await self.shot(page, "club_09_booking_confirmation")
            return True
        except Exception as exc:
            try:
                body = (await page.locator("body").inner_text(timeout=500))[:300].replace("\n", " ")
            except Exception:
                body = "<could not read page text>"
            log.warning(f"Direct booking page did not load cleanly: {exc}. url={url} body={body}")
            return False

    async def prepare_direct_booking(self, page: Page, slot: dict) -> Optional[dict]:
        bootstrap_task = asyncio.create_task(self.fetch_direct_booking_bootstrap(page, slot))
        page_task = asyncio.create_task(self.goto_direct_booking_page(page, slot))
        booking_bootstrap = None
        page_loaded = False

        while True:
            pending_tasks = [task for task in (bootstrap_task, page_task) if task is not None]
            if not pending_tasks:
                break

            done, _ = await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)

            if bootstrap_task in done:
                try:
                    booking_bootstrap = bootstrap_task.result()
                except Exception:
                    booking_bootstrap = None
                bootstrap_task = None

                rejection_reason = (booking_bootstrap or {}).get("rejection_reason")
                if rejection_reason:
                    if page_task is not None and not page_task.done():
                        page_task.cancel()
                        try:
                            await page_task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
                    log.error(
                        "Direct booking bootstrap rejected by ClubSpark: "
                        f"reason={rejection_reason} url={(booking_bootstrap or {}).get('response_url')}"
                    )
                    return None

                bootstrap_token = (booking_bootstrap or {}).get("verification_token")
                bootstrap_runtime = (booking_bootstrap or {}).get("stripe_runtime")
                if bootstrap_token and bootstrap_runtime:
                    if page_task is not None and not page_task.done():
                        page_task.cancel()
                        try:
                            await page_task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
                    log.info("Direct booking bootstrap won the race; skipping booking page navigation")
                    return booking_bootstrap

            if page_task in done:
                try:
                    page_loaded = page_task.result()
                except Exception:
                    page_loaded = False
                page_task = None

                if page_loaded:
                    page_bootstrap = await self.direct_booking_bootstrap_from_page(page)
                    if bootstrap_task is not None:
                        if bootstrap_task.done():
                            try:
                                booking_bootstrap = bootstrap_task.result()
                            except Exception:
                                booking_bootstrap = None
                        else:
                            bootstrap_task.cancel()
                            try:
                                await bootstrap_task
                            except asyncio.CancelledError:
                                pass
                            except Exception:
                                pass
                    if booking_bootstrap is None:
                        booking_bootstrap = page_bootstrap
                    else:
                        booking_bootstrap["response_url"] = (
                            booking_bootstrap.get("response_url") or page_bootstrap.get("response_url")
                        )
                        booking_bootstrap["rejection_reason"] = (
                            booking_bootstrap.get("rejection_reason") or page_bootstrap.get("rejection_reason")
                        )
                        booking_bootstrap["verification_token"] = (
                            booking_bootstrap.get("verification_token") or page_bootstrap.get("verification_token")
                        )
                        booking_bootstrap["stripe_runtime"] = (
                            booking_bootstrap.get("stripe_runtime") or page_bootstrap.get("stripe_runtime")
                        )
                    if not booking_bootstrap.get("verification_token") or not booking_bootstrap.get("stripe_runtime"):
                        log.info("Direct booking page loaded before request bootstrap completed; continuing from page state")
                    return booking_bootstrap

                if bootstrap_task is None:
                    break

        bootstrap_token = (booking_bootstrap or {}).get("verification_token")
        bootstrap_runtime = (booking_bootstrap or {}).get("stripe_runtime")
        if bootstrap_token and bootstrap_runtime:
            log.warning("Direct booking page load timed out; continuing with request bootstrap")
            return booking_bootstrap

        log.error("Direct booking page load failed")
        return None

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

    async def request_verification_token(self, page: Page) -> Optional[str]:
        locator = page.locator('input[name="__RequestVerificationToken"]').first
        try:
            if await locator.count():
                value = await locator.get_attribute("value")
                if value:
                    return value
                value = await locator.input_value(timeout=500)
                if value:
                    return value
        except Exception:
            pass

        try:
            cookies = await page.context.cookies()
        except Exception:
            cookies = []
        for cookie in cookies:
            if cookie.get("name") == "__RequestVerificationToken" and "clubspark.lta.org.uk" in cookie.get("domain", ""):
                return cookie.get("value")
        return None

    async def wait_for_direct_submit_prerequisites(
        self,
        page: Page,
        timeout_seconds: float = 0.0,
        poll_interval: float = 0.05,
    ) -> tuple[Optional[str], Optional[dict]]:
        deadline = monotonic() + max(timeout_seconds, 0.0)
        form_token = None
        runtime = None
        while True:
            if not form_token:
                form_token = await self.request_verification_token(page)
            if not runtime:
                runtime = await self.stripe_runtime(page)
            if form_token and runtime:
                return form_token, runtime
            if monotonic() >= deadline:
                return form_token, runtime
            await asyncio.sleep(poll_interval)

    async def stripe_runtime(self, page: Page) -> Optional[dict]:
        key = None
        stripe_account = None
        stripe_js_id = None
        wallet_params = self.latest_network_post_params("merchant-ui-api.stripe.com/elements/wallet-config")
        key = key or wallet_params.get("key")
        stripe_account = stripe_account or wallet_params.get("_stripe_account")
        stripe_js_id = stripe_js_id or wallet_params.get("stripe_js_id")
        if not key or not stripe_account:
            iframe_runtime = await self.stripe_runtime_from_iframes(page)
            if iframe_runtime:
                key = key or iframe_runtime.get("key")
                stripe_account = stripe_account or iframe_runtime.get("stripe_account")
                stripe_js_id = stripe_js_id or iframe_runtime.get("stripe_js_id")
        if not key or not stripe_account:
            html_runtime = await self.stripe_runtime_from_page(page)
            if html_runtime:
                key = key or html_runtime.get("key")
                stripe_account = stripe_account or html_runtime.get("stripe_account")
        if not key or not stripe_account:
            return None

        tracking = self.latest_network_response_json("m.stripe.com/6") or {}
        cookies = await page.context.cookies()
        cookie_map = {cookie.get("name"): cookie.get("value") for cookie in cookies}

        return {
            "key": key,
            "stripe_account": stripe_account,
            "stripe_js_id": stripe_js_id,
            "guid": tracking.get("guid"),
            "muid": cookie_map.get("__stripe_mid") or tracking.get("muid"),
            "sid": cookie_map.get("__stripe_sid") or tracking.get("sid"),
        }

    async def stripe_runtime_from_iframes(self, page: Page) -> Optional[dict]:
        frame_sources = await page.locator('iframe[src*="js.stripe.com"]').evaluate_all(
            """frames => frames
                .map(frame => frame.getAttribute('src') || '')
                .filter(Boolean)"""
        )
        key = None
        stripe_account = None
        stripe_js_id = None
        for source in frame_sources:
            parsed = urlsplit(source)
            raw_parts = [parsed.query, parsed.fragment]
            for raw in raw_parts:
                if not raw:
                    continue
                params = parse_qs(raw, keep_blank_values=True)
                if not key:
                    key = params.get("key", [None])[-1] or params.get("apiKey", [None])[-1]
                if not stripe_account:
                    stripe_account = params.get("stripeAccount", [None])[-1] or params.get("_stripe_account", [None])[-1]
                if not stripe_js_id:
                    stripe_js_id = (
                        params.get("controllerId", [None])[-1]
                        or params.get("clientSessionId", [None])[-1]
                        or params.get("client_session_id", [None])[-1]
                    )
            if key and stripe_account:
                return {
                    "key": key,
                    "stripe_account": stripe_account,
                    "stripe_js_id": stripe_js_id,
                }
        return None

    async def stripe_runtime_from_page(self, page: Page) -> Optional[dict]:
        try:
            content = await page.content()
        except Exception:
            return None

        return self.stripe_runtime_from_content(content)

    async def wait_for_stripe_runtime(
        self,
        page: Page,
        timeout_seconds: float = 8.0,
        poll_interval: float = 0.1,
    ) -> Optional[dict]:
        deadline = monotonic() + max(timeout_seconds, 0.0)
        while True:
            runtime = await self.stripe_runtime(page)
            if runtime:
                return runtime
            if monotonic() >= deadline:
                return None
            await asyncio.sleep(poll_interval)

    async def log_stripe_runtime_diagnostics(self, page: Page) -> None:
        try:
            frame_sources = await page.locator('iframe[src*="js.stripe.com"]').evaluate_all(
                """frames => frames
                    .map(frame => frame.getAttribute('src') || '')
                    .filter(Boolean)
                    .slice(0, 3)"""
            )
        except Exception:
            frame_sources = []
        try:
            cookies = await page.context.cookies()
            cookie_names = sorted(
                cookie.get("name")
                for cookie in cookies
                if "stripe" in cookie.get("name", "").lower()
            )
        except Exception:
            cookie_names = []
        log.warning(
            f"Stripe runtime diagnostics: iframe_count={len(frame_sources)} "
            f"iframe_srcs={frame_sources} stripe_cookies={cookie_names}"
        )

    def card_expiry_parts(self) -> tuple[str, str]:
        digits = "".join(char for char in self.card_expiry if char.isdigit())
        if len(digits) < 4:
            raise RuntimeError("CARD_EXPIRY must contain MMYY or MM/YY")
        return digits[:2], digits[-2:]

    async def create_stripe_payment_method_direct(
        self,
        page: Page,
        current_user: dict,
        runtime: Optional[dict] = None,
        runtime_wait_seconds: float = 8.0,
    ) -> Optional[str]:
        if not self.card_number or not self.card_expiry or not self.card_cvv:
            log.error("CARD_NUMBER, CARD_EXPIRY, and CARD_CVV must be set for direct payment")
            return None

        runtime = runtime or await self.wait_for_stripe_runtime(
            page,
            timeout_seconds=runtime_wait_seconds,
        )
        if not runtime:
            log.error("Direct submit unavailable: missing Stripe runtime metadata")
            await self.log_stripe_runtime_diagnostics(page)
            return None

        exp_month, exp_year = self.card_expiry_parts()
        card_number = "".join(char for char in self.card_number if char.isdigit())
        cvc = "".join(char for char in self.card_cvv if char.isdigit())
        form = {
            "type": "card",
            "billing_details[name]": f"{current_user['first_name']} {current_user['last_name']}".strip(),
            "billing_details[email]": current_user.get("email", ""),
            "card[number]": card_number,
            "card[cvc]": cvc,
            "card[exp_month]": exp_month,
            "card[exp_year]": exp_year,
            "key": runtime["key"],
            "_stripe_account": runtime["stripe_account"],
            "referrer": self.api_base_url,
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_subtype]": "split-card-element",
            "client_attribution_metadata[merchant_integration_version]": "2017",
        }
        if runtime.get("guid"):
            form["guid"] = runtime["guid"]
        if runtime.get("muid"):
            form["muid"] = runtime["muid"]
        if runtime.get("sid"):
            form["sid"] = runtime["sid"]
        if runtime.get("stripe_js_id"):
            form["client_attribution_metadata[client_session_id]"] = runtime["stripe_js_id"]

        response = await page.context.request.post(
            "https://api.stripe.com/v1/payment_methods",
            form=form,
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://js.stripe.com",
                "referer": "https://js.stripe.com/",
            },
            timeout=self.request_timeout_ms,
            fail_on_status_code=False,
            max_redirects=0,
        )
        try:
            payload = await response.json()
        except Exception:
            payload = {}

        if response.status != 200:
            detail = payload.get("error", {}).get("message") or response.status_text
            log.warning(f"Direct Stripe payment method failed: {detail}")
            return None

        payment_method_id = payload.get("id")
        if not payment_method_id:
            detail = payload.get("error", {}).get("message") or "missing payment method id"
            log.warning(f"Direct Stripe payment method failed: {detail}")
            return None

        log.info("Stripe payment method created directly")
        return payment_method_id

    def booking_description(self, slot: dict, current_user: dict) -> str:
        venue_name = current_user.get("venue_name", self.venue)
        display_date = datetime.strptime(slot["date"], "%Y-%m-%d").strftime("%d %b %Y")
        start_time = self.minutes_to_time(slot["start_time"])
        end_time = self.minutes_to_time(slot["end_time"])
        return f"Court booking at {venue_name} for {display_date} {start_time}-{end_time}"

    def slot_signature(self, slot: dict) -> tuple:
        return (
            slot.get("date"),
            slot.get("session_id"),
            slot.get("resource_id"),
            slot.get("resource_group_id"),
            slot.get("start_time"),
            slot.get("end_time"),
        )

    async def precreate_stripe_payment_method(
        self,
        page: Page,
        slot: dict,
        current_user: dict,
    ) -> Optional[dict]:
        booking_bootstrap = await self.fetch_direct_booking_bootstrap(page, slot)
        rejection_reason = (booking_bootstrap or {}).get("rejection_reason")
        if rejection_reason:
            log.warning(
                "Stripe payment method precreation skipped: "
                f"booking bootstrap rejected with reason={rejection_reason}"
            )
            return None

        runtime = (booking_bootstrap or {}).get("stripe_runtime")
        if not runtime:
            log.warning("Stripe payment method precreation skipped: no Stripe runtime before release")
            return None

        payment_method_id = await self.create_stripe_payment_method_direct(
            page,
            current_user,
            runtime=runtime,
            runtime_wait_seconds=0.0,
        )
        if not payment_method_id:
            log.warning("Stripe payment method precreation failed")
            return None

        log.info("Stripe payment method precreated before release")
        return {
            "payment_method_id": payment_method_id,
            "created_at_monotonic": monotonic(),
        }

    def usable_precreated_stripe_payment_method(self, precreated_payment_method: Optional[dict]) -> Optional[str]:
        if not precreated_payment_method:
            return None
        payment_method_id = precreated_payment_method.get("payment_method_id")
        created_at = precreated_payment_method.get("created_at_monotonic")
        if not payment_method_id or created_at is None:
            return None
        age_seconds = monotonic() - created_at
        if age_seconds > self.precreated_stripe_pm_max_age_seconds:
            log.warning(
                f"Precreated Stripe payment method expired after {age_seconds:.1f}s; creating a fresh one"
            )
            return None
        return payment_method_id

    async def precreate_booking_payment(
        self,
        page: Page,
        slot: dict,
        current_user: dict,
        precreated_payment_method: Optional[dict],
    ) -> Optional[dict]:
        payment_method_id = self.usable_precreated_stripe_payment_method(precreated_payment_method)
        if not payment_method_id:
            log.warning("ClubSpark payment token precreation skipped: no usable precreated Stripe payment method")
            return None

        booking_token = await self.create_booking_payment_direct(
            page,
            slot,
            current_user,
            payment_method_id,
            referer_url=self.direct_booking_url(slot),
        )
        if not booking_token:
            log.warning("ClubSpark payment token precreation failed")
            return None

        log.info("ClubSpark payment token precreated before release")
        return {
            "booking_token": booking_token,
            "created_at_monotonic": monotonic(),
            "slot_signature": self.slot_signature(slot),
        }

    def usable_precreated_booking_payment(self, slot: dict, precreated_booking_payment: Optional[dict]) -> Optional[str]:
        if not precreated_booking_payment:
            return None

        booking_token = precreated_booking_payment.get("booking_token")
        created_at = precreated_booking_payment.get("created_at_monotonic")
        slot_signature = precreated_booking_payment.get("slot_signature")
        if not booking_token or created_at is None:
            return None
        if slot_signature != self.slot_signature(slot):
            log.warning("Precreated ClubSpark payment token does not match the resolved slot; creating a fresh one")
            return None

        age_seconds = monotonic() - created_at
        if age_seconds > self.precreated_booking_payment_max_age_seconds:
            log.warning(
                f"Precreated ClubSpark payment token expired after {age_seconds:.1f}s; creating a fresh one"
            )
            return None
        return booking_token

    async def create_booking_payment_direct(
        self,
        page: Page,
        slot: dict,
        current_user: dict,
        payment_method_id: str,
        referer_url: Optional[str] = None,
    ) -> Optional[str]:
        payload = {
            "PaymentMethodId": payment_method_id,
            "Cost": slot["total_cost"],
            "VenueID": slot["resource_group_id"],
            "PaymentParams": '["booking-default"]',
            "ScopeID": slot["session_id"],
            "Description": self.booking_description(slot, current_user),
            "Metadata": None,
        }
        response = await page.context.request.post(
            f"{self.api_base_url}/Payment/CreatePayment/",
            data=_json.dumps(payload),
            headers={
                "accept": "*/*",
                "content-type": "application/json",
                "referer": referer_url or page.url,
            },
            timeout=self.request_timeout_ms,
            fail_on_status_code=False,
            max_redirects=0,
        )
        try:
            body_text = await response.text()
        except Exception:
            body_text = ""
        try:
            data = _json.loads(body_text) if body_text else {}
        except Exception:
            data = {}

        if response.status != 200:
            detail = data.get("Error") or body_text[:200].replace("\n", " ") or response.status_text
            log.warning(f"Direct CreatePayment failed with status {response.status}: {detail}")
            return None
        if data.get("RequiresAction"):
            log.warning(f"Direct CreatePayment requires additional action: {body_text[:200].replace(chr(10), ' ')}")
            return None
        if data.get("Error"):
            log.warning(f"Direct CreatePayment failed: {data['Error']}")
            return None

        booking_token = data.get("ID")
        if not booking_token:
            log.warning("Direct CreatePayment did not return a booking token")
            return None

        log.info("ClubSpark payment token created directly")
        return booking_token

    async def confirm_booking_direct(
        self,
        page: Page,
        slot: dict,
        current_user: dict,
        form_token: str,
        booking_token: str,
        referer_url: Optional[str] = None,
    ) -> bool:
        response = await page.context.request.post(
            f"{self.base_url}/Booking/ConfirmBooking",
            form={
                "SendSMS": "false",
                "promo-code": "",
                "__RequestVerificationToken": form_token,
                "SessionID": slot["session_id"],
                "ResourceID": slot["resource_id"],
                "ResourceGroupID": slot["resource_group_id"],
                "MatchID": "",
                "RoleID": "",
                "Date": slot["date"],
                "StartTime": str(slot["start_time"]),
                "EndTime": str(slot["end_time"]),
                "CourtCost": self.money_text(slot["court_cost"]),
                "LightingCost": self.money_text(slot["lighting_cost"]),
                "MembershipCost": "0",
                "MembersPrice": "0",
                "GuestsPrice": "0",
                "MembersCost": "0",
                "GuestsCost": "0",
                "Token": booking_token,
                "Format": "None",
                "Source": "",
                "UseCredits": "False",
                "TotalCost": self.money_text(slot["total_cost"]),
                "Contacts[0].VenueContactID": current_user["venue_contact_id"],
                "Contacts[0].VenueContactName": "",
                "Contacts[0].IsPrimary": "true",
                "Contacts[0].IsMember": "False",
                "Contacts[0].FirstName": current_user["first_name"],
                "Contacts[0].LastName": current_user["last_name"],
            },
            headers={
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
                    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
                ),
                "content-type": "application/x-www-form-urlencoded",
                "referer": referer_url or page.url,
            },
            timeout=self.request_timeout_ms,
            fail_on_status_code=False,
            max_redirects=0,
        )
        location = response.headers.get("location", "")
        if response.status in (301, 302, 303, 307, 308) and location:
            target = urljoin(self.api_base_url, location)
            if "BookingUnsuccessful" in target:
                log.error(f"Direct confirm redirected to BookingUnsuccessful: {target}")
                try:
                    await page.goto(target, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
                except Exception:
                    pass
                return False
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
            except Exception as exc:
                log.error(f"Direct confirm redirect could not be opened: {exc}")
                return False
            return await self.wait_for_booking_confirmation(page)

        if "BookingUnsuccessful" in response.url:
            log.error(f"Direct confirm returned BookingUnsuccessful: {response.url}")
            return False

        log.error(f"Direct confirm returned unexpected status {response.status} at {response.url}")
        return False

    async def submit_payment_via_direct_api(
        self,
        page: Page,
        slot: dict,
        current_user: dict,
        form_token: Optional[str] = None,
        runtime: Optional[dict] = None,
        payment_method_id_override: Optional[str] = None,
        runtime_wait_seconds: float = 8.0,
    ) -> Optional[bool]:
        form_token = form_token or await self.request_verification_token(page)
        if not form_token:
            log.error("Direct submit unavailable: missing booking verification token")
            return None

        referer_url = self.direct_booking_url(slot)
        payment_method_id = payment_method_id_override
        if payment_method_id:
            log.info("Using precreated Stripe payment method")
        else:
            payment_method_id = await self.create_stripe_payment_method_direct(
                page,
                current_user,
                runtime=runtime,
                runtime_wait_seconds=runtime_wait_seconds,
            )
        if not payment_method_id:
            return None

        booking_token = await self.create_booking_payment_direct(
            page,
            slot,
            current_user,
            payment_method_id,
            referer_url=referer_url,
        )
        if not booking_token:
            return None

        log.info("Attempting direct booking confirmation")
        return await self.confirm_booking_direct(
            page,
            slot,
            current_user,
            form_token,
            booking_token,
            referer_url=referer_url,
        )

    async def pay(
        self,
        page: Page,
        dry_run: bool,
        slot: Optional[dict] = None,
        current_user: Optional[dict] = None,
        form_token_override: Optional[str] = None,
        runtime_override: Optional[dict] = None,
        precreated_payment_method: Optional[dict] = None,
        precreated_booking_payment: Optional[dict] = None,
    ) -> bool:
        if dry_run:
            log.info("[DRY-RUN] Stopping before direct payment and booking submission")
            return True

        if not slot or not current_user:
            log.error("Direct submit requires slot and current user details")
            return False

        log.info("Attempting direct payment and booking submission without opening payment session")
        form_token = form_token_override
        runtime = runtime_override
        if not form_token or not runtime:
            page_form_token, page_runtime = await self.wait_for_direct_submit_prerequisites(page, timeout_seconds=0.0)
            form_token = form_token or page_form_token
            runtime = runtime or page_runtime
        if not form_token or not runtime:
            log.info("Direct submit prerequisites not ready immediately; retrying briefly")
            page_form_token, page_runtime = await self.wait_for_direct_submit_prerequisites(page, timeout_seconds=0.35)
            form_token = form_token or page_form_token
            runtime = runtime or page_runtime

        rejection_reason = self.booking_unsuccessful_reason(page.url)
        if rejection_reason:
            log.error(f"Direct booking page rejected by ClubSpark: reason={rejection_reason} url={page.url}")
            return False
        if not form_token:
            log.error("Direct submit unavailable after direct booking page load: missing booking verification token")
            return False

        precreated_booking_token = self.usable_precreated_booking_payment(slot, precreated_booking_payment)
        if precreated_booking_token:
            log.info("Using precreated ClubSpark payment token")
            log.info("Attempting direct booking confirmation")
            if await self.confirm_booking_direct(
                page,
                slot,
                current_user,
                form_token,
                precreated_booking_token,
                referer_url=self.direct_booking_url(slot),
            ):
                return True
            log.warning("Precreated ClubSpark payment token path failed; retrying with a fresh ClubSpark payment token")

        if not runtime:
            log.error("Direct submit unavailable after direct booking page load: missing Stripe runtime metadata")
            await self.log_stripe_runtime_diagnostics(page)
            return False

        payment_method_id_override = self.usable_precreated_stripe_payment_method(precreated_payment_method)
        direct_result = await self.submit_payment_via_direct_api(
            page,
            slot,
            current_user,
            form_token=form_token,
            runtime=runtime,
            payment_method_id_override=payment_method_id_override,
            runtime_wait_seconds=0.0,
        )
        if direct_result is None and payment_method_id_override:
            log.warning("Precreated Stripe payment method path failed; retrying with a fresh Stripe payment method")
            direct_result = await self.submit_payment_via_direct_api(
                page,
                slot,
                current_user,
                form_token=form_token,
                runtime=runtime,
                payment_method_id_override=None,
                runtime_wait_seconds=0.0,
            )
        if direct_result is True:
            return True
        if direct_result is False:
            log.error("Direct submit failed after direct booking page load")
            return False

        log.error("Direct submit path unavailable after direct booking page load")
        return False

    async def wait_until_release(self, timing: TimingHelper, release_time: datetime, options: RunOptions) -> None:
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

    async def wait_until_precreate_window(
        self,
        timing: TimingHelper,
        release_time: datetime,
        lead_seconds: float,
        label: str,
        options: RunOptions,
    ) -> bool:
        if options.skip_wait or options.debug:
            log.info(f"Precreating {label} immediately for test mode")
            return True

        seconds_until_precreate = timing.secs_until(release_time) - max(0.0, lead_seconds)
        if seconds_until_precreate > 0:
            log.info(f"Waiting {seconds_until_precreate:.1f}s before {label} precreation")
            await asyncio.sleep(seconds_until_precreate)
        return True

    async def finish_precreate_before_release(
        self,
        timing: TimingHelper,
        release_time: datetime,
        label: str,
        precreate_coro,
        options: RunOptions,
    ):
        if options.skip_wait or options.debug:
            return await precreate_coro

        remaining = max(0.0, timing.secs_until(release_time) - 0.1)
        if remaining <= 0:
            log.warning(f"{label} precreation window expired; continuing without it")
            return None
        try:
            return await asyncio.wait_for(precreate_coro, timeout=remaining)
        except asyncio.TimeoutError:
            log.warning(f"{label} precreation did not finish before release; continuing without it")
            return None

    async def maybe_precreate_payment_method(
        self,
        page: Page,
        prefetched_slot: Optional[dict],
        current_user: Optional[dict],
        timing: TimingHelper,
        release_time: datetime,
        options: RunOptions,
    ) -> Optional[dict]:
        if prefetched_slot is None or current_user is None:
            return None

        await self.wait_until_precreate_window(
            timing,
            release_time,
            self.precreate_stripe_pm_lead_seconds,
            "Stripe payment method",
            options,
        )
        return await self.finish_precreate_before_release(
            timing,
            release_time,
            "Stripe payment method",
            self.precreate_stripe_payment_method(page, prefetched_slot, current_user),
            options,
        )

    async def maybe_precreate_booking_payment(
        self,
        page: Page,
        prefetched_slot: Optional[dict],
        current_user: Optional[dict],
        precreated_payment_method: Optional[dict],
        timing: TimingHelper,
        release_time: datetime,
        options: RunOptions,
    ) -> Optional[dict]:
        if prefetched_slot is None or current_user is None:
            return None

        await self.wait_until_precreate_window(
            timing,
            release_time,
            self.precreate_booking_payment_lead_seconds,
            "ClubSpark payment token",
            options,
        )
        return await self.finish_precreate_before_release(
            timing,
            release_time,
            "ClubSpark payment token",
            self.precreate_booking_payment(
                page,
                prefetched_slot,
                current_user,
                precreated_payment_method,
            ),
            options,
        )

    async def resolve_slot_and_bootstrap(
        self,
        page: Page,
        booking_time: str,
        date: str,
        prefetched_slot: Optional[dict],
    ) -> tuple[Optional[dict], Optional[dict]]:
        slot_refresh_task = asyncio.create_task(self.wait_for_slot_via_api(page, booking_time, date))
        slot = prefetched_slot
        booking_bootstrap = None

        if slot:
            booking_bootstrap = await self.prepare_direct_booking(page, slot)
            if booking_bootstrap is None:
                log.warning("Prefetched slot path failed; waiting for live API slot")
                slot = None

        if slot is None:
            try:
                slot = await slot_refresh_task
            finally:
                slot_refresh_task = None
            if not slot:
                log.error("API booking path did not find a matching slot")
                return None, None
            booking_bootstrap = await self.prepare_direct_booking(page, slot)
            if booking_bootstrap is None:
                return None, None
        elif slot_refresh_task is not None:
            slot_refresh_task.cancel()
            try:
                await slot_refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        return slot, booking_bootstrap

    async def ensure_current_user_for_slot(
        self,
        page: Page,
        slot: dict,
        user: Optional[dict],
        current_user: Optional[dict],
    ) -> Optional[dict]:
        if current_user is not None:
            return current_user
        try:
            if user is None:
                user = await self.get_current_user(page)
            return self.venue_contact_for_user(user, slot["resource_group_id"])
        except Exception:
            return None

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
        run_date_stamp = timing.now_true().strftime("%Y%m%d")
        self.configure_artifact_paths(run_date_stamp)

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
                await self.attach_network_response_logger(page, net_entries, capture_bodies=options.debug)
                self._network_entries = net_entries

            try:
                user = None
                if not await self.login(page, date):
                    return

                try:
                    user = await self.get_current_user(page)
                    current_user = self.venue_contact_for_user(user, "")
                except Exception as exc:
                    log.warning(f"Could not load current ClubSpark user: {exc}")
                    current_user = None

                for pattern in [
                    "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                    "**/google-analytics.com/**",
                    "**/googletagmanager.com/**",
                    "**/region1.google-analytics.com/**",
                ]:
                    await page.route(pattern, lambda route: route.abort())

                await self.goto_booking_page(page, date)
                prefetched_slot = await self.prefetch_slot_candidate(page, booking_time, date)
                if current_user is None and user is not None and prefetched_slot is not None:
                    current_user = self.venue_contact_for_user(user, prefetched_slot["resource_group_id"])

                precreated_payment_method = await self.maybe_precreate_payment_method(
                    page,
                    prefetched_slot,
                    current_user,
                    timing,
                    release_time,
                    options,
                )
                precreated_booking_payment = await self.maybe_precreate_booking_payment(
                    page,
                    prefetched_slot,
                    current_user,
                    precreated_payment_method,
                    timing,
                    release_time,
                    options,
                )

                await self.wait_until_release(timing, release_time, options)

                slot, booking_bootstrap = await self.resolve_slot_and_bootstrap(
                    page,
                    booking_time,
                    date,
                    prefetched_slot,
                )
                if slot is None or booking_bootstrap is None:
                    return

                current_user = await self.ensure_current_user_for_slot(page, slot, user, current_user)

                await self.pay(
                    page,
                    dry_run=dry_run,
                    slot=slot,
                    current_user=current_user,
                    form_token_override=(booking_bootstrap or {}).get("verification_token"),
                    runtime_override=(booking_bootstrap or {}).get("stripe_runtime"),
                    precreated_payment_method=precreated_payment_method,
                    precreated_booking_payment=precreated_booking_payment,
                )
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
