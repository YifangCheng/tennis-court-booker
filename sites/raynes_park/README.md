# Raynes Park

Raynes Park is the first site plugin in this repository. It automates booking at the AELTC Community Tennis Centre and owns the selectors, login flow, court mapping, and payment steps for that site.

## Run Commands

Use the shared entrypoint with the Raynes Park plugin:

```bash
python main.py --site raynes_park --debug --now --date 2026-03-04 --time 09:00
python main.py --site raynes_park --debug --now --pay --date 2026-03-04 --time 09:00
python main.py --site raynes_park
```

Common flags:

- `--debug`: enable screenshots and network logging
- `--now`: skip the midnight wait
- `--pay`: allow payment submission in debug mode
- `--date YYYY-MM-DD`: override the target date
- `--time HH:MM`: override the booking time

## Debug Workflow

Start with a dry-run. It takes screenshots, fills card details, and stops before payment unless `--pay` is set.

After each run, inspect `screenshots/`:

| Screenshot | Check |
|---|---|
| `01_login_page.png` | Login form loaded |
| `02_after_login.png` | Logged in successfully |
| `03_booking_page.png` | Correct date and court grid visible |
| `04_clicked_slot.png` | Correct court and time selected |
| `05_popup.png` | Continue booking popup appeared |
| `06_booking_page.png` | Navigated to the booking confirmation page |
| `09_card_filled.png` | Card fields filled successfully |

Debug runs also write `network_log.json` and `debug_cookies.json` in the repository root.

## Midnight Validation

To test the midnight flow without waiting for a full booking cycle:

1. Check which date opens tonight.
2. Dry-run that date with an available time.
3. Run the real scheduled or manual midnight flow.

Example:

```bash
python -c "from datetime import date, timedelta; print((date.today() + timedelta(days=14)).isoformat())"
python main.py --site raynes_park --debug --now --date <that-date> --time 17:00
python main.py --site raynes_park
```

## User Setup Notes

User-facing configuration lives in the main repository README. Raynes Park login credentials live in the root `.env` file using the `RAYNES_PARK_` prefix, while payment details are shared in the same file.
