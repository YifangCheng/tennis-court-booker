# Club Spark

This plugin targets ClubSpark venues such as Tanner St Park. The current fast path logs in early, preloads the target date, prefetched the likely slot before release, then uses direct ClubSpark and Stripe requests for payment and confirmation instead of the old click-through UI flow.

## Run Commands

```bash
python main.py --site club_spark --venue TannerStPark --court 4 --debug --now --date 2026-03-24 --time 10:00
python main.py --site club_spark --venue TannerStPark --court 4 --debug --now --pay --date 2026-03-24 --time 10:00
python main.py --site club_spark --venue TannerStPark --court 4 --account a --pay --date 2026-03-27 --time 10:00
python main.py --site club_spark --venue TannerStPark --court 4 --account b --pay --date 2026-03-27 --time 11:00
bash schedule.sh --site club_spark --venue TannerStPark --court 4 --account a --time 10:00
bash schedule.sh --site club_spark --venue TannerStPark --court 4 --account b --time 11:00
```

## Booking Rules

- New day release time: `20:00`
- Booking window: `7 days in advance`
- Default duration: `60 minutes`
- `--venue` is required, for example `TannerStPark` or `GeraldineMaryHarmsworth`
- `--court` is required, for example `--court 4`
- Account `a` is the implicit default, and `--account` selects `CLUB_SPARK_<ACCOUNT>_BOOKING_USERNAME` / `...PASSWORD`
- `--time HH:MM` is required for ClubSpark booking runs
- `booking_open_time` in `sites/club_spark/config.json` controls when new bookings open

Without `--date`, the plugin targets `today + 7 days`, which matches the site's release rule. For example, if today is Tuesday, it targets next Tuesday.

## Debug Notes

- Login uses the ClubSpark sign-in link, then the LTA login provider (`value="LTA2"`).
- Slot discovery uses `GetVenueSessions`, and the booking page bootstrap races a raw request against browser navigation.
- The default hot path is direct Stripe `payment_methods` -> ClubSpark `CreatePayment` -> ClubSpark `ConfirmBooking`.
- Debug mode writes `network_log.club_spark.json` and `debug_cookies.club_spark.json` in the repository root.

## API Note

This plugin now uses a direct submit path after release. The remaining browser dependency is the booking-page bootstrap, which is used to obtain the verification token and Stripe runtime data when needed.

## Speed Tuning

- Stripe `payment_method` precreation is on by default.
- If the reused precreated payment method fails before confirmation, the bot retries once with a fresh Stripe payment method.
- `CLUB_SPARK_PRECREATE_STRIPE_PM_LEAD_SECONDS` controls how many seconds before release the precreation attempt runs.
- `CLUB_SPARK_PRECREATED_STRIPE_PM_MAX_AGE_SECONDS` controls how old a cached Stripe payment method is allowed to be before the bot creates a fresh one.
- `CLUB_SPARK_REQUEST_TIMEOUT_SECONDS` controls the timeout for the raw booking bootstrap request, Stripe `payment_methods`, `CreatePayment`, and `ConfirmBooking`.
- `CLUB_SPARK_PAGE_TIMEOUT_SECONDS` controls the timeout for direct booking-page and confirmation-page navigations.
