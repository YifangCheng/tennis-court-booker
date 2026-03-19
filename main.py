#!/usr/bin/env python3

import argparse
import asyncio

from shared.runtime import RunOptions, ensure_runtime_dirs
from sites import build_registry


def parse_args() -> argparse.Namespace:
    registry = build_registry()
    parser = argparse.ArgumentParser(description="Multi-site tennis court booker")
    parser.add_argument(
        "--site",
        required=True,
        choices=sorted(registry.keys()),
        help="Booking site plugin to run.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable screenshots and network logging. Stops before payment unless --pay is also set.",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Skip the midnight wait — run the booking flow immediately.",
    )
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Override target date. Default: site-specific booking window logic.",
    )
    parser.add_argument(
        "--time",
        default=None,
        metavar="HH:MM",
        help="Override booking time. Default: value from the selected site's config.",
    )
    parser.add_argument(
        "--pay",
        action="store_true",
        help="Actually submit payment, even in --debug mode.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_runtime_dirs()
    registry = build_registry()
    site = registry[args.site]()
    options = RunOptions(
        debug=args.debug,
        skip_wait=args.now,
        force_pay=args.pay,
        date_override=args.date,
        time_override=args.time,
    )
    asyncio.run(site.run(options))


if __name__ == "__main__":
    main()
