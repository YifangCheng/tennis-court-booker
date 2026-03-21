import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import ntplib
import pytz
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_root_env() -> None:
    load_dotenv(ROOT / ".env")


def ensure_runtime_dirs() -> None:
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    (ROOT / "screenshots").mkdir(parents=True, exist_ok=True)


def get_logger() -> logging.Logger:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s  %(levelname)-7s  %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)],
        )
    return logging.getLogger("tennis_court_booker")


def load_json(path: Path) -> dict:
    with open(path) as handle:
        return json.load(handle)


@dataclass(frozen=True)
class RunOptions:
    debug: bool
    skip_wait: bool
    force_pay: bool
    date_override: Optional[str]
    time_override: Optional[str]
    account_override: Optional[str]


class TimingHelper:
    def __init__(self, tz_name: str, log: logging.Logger):
        self.tz_name = tz_name
        self.log = log
        self._ntp_offset = 0.0

    def tz(self) -> pytz.BaseTzInfo:
        return pytz.timezone(self.tz_name)

    def sync_ntp(self, server: str = "pool.ntp.org") -> None:
        try:
            resp = ntplib.NTPClient().request(server, version=3)
            self._ntp_offset = resp.offset
            self.log.info(f"NTP sync OK  offset={self._ntp_offset:+.3f}s  (server={server})")
        except Exception as exc:
            self.log.warning(f"NTP sync failed — using local clock: {exc}")

    def now_true(self) -> datetime:
        return datetime.now(self.tz()) + timedelta(seconds=self._ntp_offset)

    def midnight_tonight(self) -> datetime:
        tomorrow = self.now_true().date() + timedelta(days=1)
        return self.tz().localize(datetime(tomorrow.year, tomorrow.month, tomorrow.day))

    def target_date(self) -> str:
        tomorrow = self.now_true().date() + timedelta(days=1)
        return (tomorrow + timedelta(days=13)).strftime("%Y-%m-%d")

    def secs_until(self, dt: datetime) -> float:
        return (dt - self.now_true()).total_seconds()
