# Club Spark

This plugin targets Tanner St Park on ClubSpark. It uses a safer hybrid flow: pre-login, pre-load the target date, use the API to detect when the exact slot is available, then use the UI only for the final click, duration selection, continue-booking step, and payment.

## Run Commands

```bash
python main.py --site club_spark --debug --now --date 2026-03-24 --time 10:00
python main.py --site club_spark --debug --now --pay --date 2026-03-24 --time 10:00
python main.py --site club_spark --account a --pay --date 2026-03-27 --time 10:00
python main.py --site club_spark --account b --pay --date 2026-03-27 --time 11:00
python main.py --site club_spark
bash schedule.sh --site club_spark --account a --time 10:00
bash schedule.sh --site club_spark --account b --time 11:00
```

## Booking Rules

- New day release time: `20:00`
- Booking window: `7 days in advance`
- Default duration: `60 minutes`
- Court priority comes from `preferred_courts` in `sites/club_spark/config.json`
- Account `a` is the implicit default, and `--account` selects `CLUB_SPARK_<ACCOUNT>_BOOKING_USERNAME` / `...PASSWORD`
- `--time HH:MM` can override `booking_time` for a scheduled or manual run

Without `--date`, the plugin targets `today + 7 days`, which matches the site's release rule. For example, if today is Tuesday, it targets next Tuesday.

## Debug Notes

- Login uses the ClubSpark sign-in link, then the LTA login provider (`value="LTA2"`).
- Slot discovery uses `GetVenueSessions`, then the UI clicks the exact configured court/time and expands the booking to 1 hour by selecting the end time in the duration dropdown.
- Debug mode writes `network_log.club_spark.json` and `debug_cookies.club_spark.json` in the repository root.

## API Note

This plugin does not use a full direct-booking API path yet. It uses the API only to detect slot availability, then relies on the normal booking UI for the booking confirmation step. Debug network logging is left enabled so a fuller ClubSpark booking API contract can be investigated later if needed.
