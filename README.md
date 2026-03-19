# Tennis Court Booker

This repository automates tennis court booking across multiple websites using a plugin-style layout. The standard entrypoint is `main.py` with `--site`.

## Setup

```bash
bash setup.sh          # creates .venv, installs dependencies, copies .env.example -> .env
```

Then edit `.env`:

```
RAYNES_PARK_BOOKING_USERNAME=your_email@example.com
RAYNES_PARK_BOOKING_PASSWORD=your_password
CLUB_SPARK_BOOKING_USERNAME=your_clubspark_username
CLUB_SPARK_BOOKING_PASSWORD=your_clubspark_password
CARD_NUMBER=1234567890123456
CARD_EXPIRY=12/27
CARD_CVV=123
CARD_NAME=Your Full Name
```

## Configuration

Each site keeps its own runtime defaults. Edit the config file for the site you want to run, for example `sites/raynes_park/config.json` or `sites/club_spark/config.json`.

```json
{
  "booking_date": null,
  "booking_time": "10:00",
  "preferred_courts": [4, 5, 6, 7, 8, 9],
  "court_type": "indoor",
  "timezone": "Europe/London",
  "headless": true,
  "pre_login_seconds": 120
}
```

Important fields:
- `booking_time`: default slot time to book
- `preferred_courts`: priority order for slot selection
- `court_type`: `indoor`, `outdoor`, or `grass`
- `pre_login_seconds`: how early the script logs in before midnight

Login credentials are site-specific by variable name inside `.env`. Payment fields remain shared in the same file.

## Scheduled run (sleep through it)

Makes your Mac wake from sleep shortly before the selected site's release window and start the booking script at that site's pre-login time.

**Requirements:** MacBook must be plugged in to power (lid can be closed).

```bash
bash schedule.sh --site raynes_park
```

Pass the target plugin explicitly. For example, if you later add `clubspark_wimbledon`, schedule it with `bash schedule.sh --site clubspark_wimbledon`.

To cancel:

```bash
bash schedule.sh --uninstall
```

## Site Docs

Each plugin can keep its own operational and development notes.

- Club Spark: [`sites/club_spark/README.md`](/Users/yifang/PycharmProjects/tennis-court-booker/sites/club_spark/README.md)
- Raynes Park: [`sites/raynes_park/README.md`](/Users/yifang/PycharmProjects/tennis-court-booker/sites/raynes_park/README.md)
