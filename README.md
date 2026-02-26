# Tennis Court Booker

Automatically books a Raynes Park tennis court (4–9) at 17:00, firing at exactly midnight when the 13-day booking window opens.

---

## First-time setup

```bash
bash setup.sh          # creates venv, installs dependencies, copies .env.example → .env
```

Then edit `.env` with your credentials:

```
BOOKING_USERNAME=your_email@example.com
BOOKING_PASSWORD=your_password
CARD_NUMBER=1234567890123456
CARD_EXPIRY=12/27
CARD_CVV=123
CARD_NAME=Your Full Name
```

---

## Debug run (start here — no payment made)

Tests the full flow on a specific date/time you choose. Fills in card details but **does not click Pay**.

```bash
source venv/bin/activate
python booker.py --debug --now --date 2026-03-04 --time 09:00
```

- `--debug` — fills card details but stops before clicking Pay
- `--now` — skips the midnight wait, runs immediately
- `--date` — override the target date (default: auto-calculated as tonight + 13 days)
- `--time` — override the booking time (default: 17:00 from config.json)

Check `screenshots/` after each run to verify each step worked correctly:

| Screenshot | What to check |
|---|---|
| `01_login_page.png` | Login form loaded |
| `02_after_login.png` | Logged in successfully |
| `03_booking_page.png` | Correct date shown, court grid visible |
| `04_clicked_slot.png` | 17:00 slot highlighted/clicked |
| `05_popup.png` | "Continue booking" popup appeared |
| `06_booking_page.png` | Navigated to /Booking/Book page |
| `07_confirm_and_pay_page.png` | "Confirm and pay" button visible |
| `08_card_popup.png` | Stripe card form visible |
| `09_card_filled.png` | All three card fields filled in |

---

## Normal run (real booking)

Waits until exactly midnight, then books the first available court (4–9) at 17:00.

```bash
source venv/bin/activate
python booker.py
```

The script will:
1. Sleep until 2 minutes before midnight
2. Log in and pre-load the booking page
3. Wait for exactly midnight
4. Reload the page and click the 17:00 slot
5. Confirm and complete payment

Logs are written to `booker.log`.

---

## Scheduled run (sleep through it)

Makes your Mac wake from sleep at 23:57 and run the script automatically at 23:58.

**Requirements:** MacBook must be plugged in to power (lid can be closed).

```bash
bash schedule.sh
```

To cancel:

```bash
bash schedule.sh --uninstall
```

---

## Configuration

Edit `config.json` to change defaults:

```json
{
  "booking_time":      "17:00",
  "preferred_courts":  [4, 5, 6, 7, 8, 9],
  "timezone":          "Europe/London",
  "headless":          true,
  "pre_login_seconds": 120
}
```
