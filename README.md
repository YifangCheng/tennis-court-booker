# Tennis Court Booker

Automatically books an indoor tennis court at the AELTC Community Tennis Centre (Raynes Park), firing at exactly midnight when the 13-day booking window opens.

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

## Modes

| Command | Mode | What it does |
|---|---|---|
| `python booker.py --debug --now --date 2026-03-03 --time 12:00` | **Dry-run** | Runs now with screenshots, stops before payment |
| `python booker.py --debug --now --pay --date 2026-03-03 --time 12:00` | **Book now** | Runs now with screenshots, completes payment |
| `python booker.py` | **Production** | Waits for midnight, books & pays automatically |
| `python booker.py --now` | **Quick live** | Production logic but skips midnight wait |

### Flags

| Flag | Description |
|---|---|
| `--debug` | Enable screenshots + network logging. Stops before payment unless `--pay` is also set |
| `--now` | Skip the midnight wait — run the booking flow immediately |
| `--pay` | Submit payment even in debug mode |
| `--date YYYY-MM-DD` | Override target date (default: auto-calculated as tonight + 14 days) |
| `--time HH:MM` | Override booking time (default: from `config.json`) |

---

## Recommended workflow

### 1. Dry-run (start here — no payment made)

Tests the full flow on a specific date/time you choose. Fills in card details but **does not click Pay**.

```bash
source venv/bin/activate
python booker.py --debug --now --date 2026-03-04 --time 09:00
```

Check `screenshots/` after each run to verify each step worked correctly:

| Screenshot | What to check |
|---|---|
| `01_login_page.png` | Login form loaded |
| `02_after_login.png` | Logged in successfully |
| `03_booking_page.png` | Correct date shown, court grid visible |
| `04_clicked_slot.png` | Correct court and time selected |
| `05_popup.png` | "Continue booking" popup appeared |
| `06_booking_page.png` | Navigated to /Booking/Book page |
| `09_card_filled.png` | All three card fields filled in |

### 2. Test a real booking

Pick an available slot you can cancel afterwards:

```bash
python booker.py --debug --now --pay --date 2026-03-03 --time 12:00
```

### 3. Production run (real booking at midnight)

```bash
python booker.py
```

The script will:
1. Sleep until 2 minutes before midnight
2. Log in and pre-load the booking page
3. Wait for exactly midnight (NTP-synced)
4. Click the first available preferred court
5. Confirm and complete payment

### Testing the midnight flow

To verify the midnight wait logic works without waiting a full day:

1. Check what date opens tonight:
   ```bash
   python -c "from datetime import date, timedelta; print((date.today() + timedelta(days=14)).isoformat())"
   ```

2. Dry-run for that date to confirm a slot is available:
   ```bash
   python booker.py --debug --now --date <that-date> --time 17:00
   ```

3. Run the real midnight booking (start before midnight):
   ```bash
   python booker.py
   ```
   The script auto-calculates the target date and sleeps until 2 minutes before midnight.

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

Edit `config.json`:

```json
{
  "booking_date":      null,
  "booking_time":      "10:00",
  "preferred_courts":  [4, 5, 6, 7, 8, 9],
  "court_type":        "indoor",
  "timezone":          "Europe/London",
  "headless":          true,
  "pre_login_seconds": 120
}
```

| Field | Description | Default |
|---|---|---|
| `booking_date` | Target date (YYYY-MM-DD), or `null` to auto-calculate from midnight | `null` |
| `booking_time` | Time slot to book (HH:MM) | `"10:00"` |
| `preferred_courts` | Court numbers to try, in priority order | `[4, 5, 6, 7, 8, 9]` |
| `court_type` | `"indoor"`, `"outdoor"`, or `"grass"` | `"indoor"` |
| `timezone` | Timezone for midnight calculation | `"Europe/London"` |
| `headless` | Run browser without GUI | `true` |
| `pre_login_seconds` | Seconds before midnight to login | `120` |
