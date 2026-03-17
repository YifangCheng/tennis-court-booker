# Repository Guidelines

## Project Structure & Module Organization
This repository is intentionally small. `booker.py` contains the main Playwright automation flow, CLI flags, config loading, and midnight booking logic. `config.json` stores runtime defaults such as `booking_time`, `preferred_courts`, and `headless`. `setup.sh` bootstraps the local environment, and `schedule.sh` installs or removes the macOS scheduled run. Reference material lives in [`README.md`](/Users/yifang/PycharmProjects/tennis-court-booker/README.md) and [`context.md`](/Users/yifang/PycharmProjects/tennis-court-booker/context.md). Runtime artifacts such as `screenshots/`, `booker.log`, `booker_error.log`, and `network_log.json` are generated locally and should not be committed.

## Build, Test, and Development Commands
Run `bash setup.sh` once to create `.venv/`, install Python packages, and install Playwright Chromium. Activate the environment with `source .venv/bin/activate`. Use `python booker.py --debug --now --date 2026-03-04 --time 09:00` for a dry-run with screenshots and no payment. Use `python booker.py --debug --now --pay --date 2026-03-04 --time 09:00` to exercise the live payment path. Use `bash schedule.sh` to install the macOS wake-and-run job and `bash schedule.sh --uninstall` to remove it.

## Coding Style & Naming Conventions
Follow existing Python style: 4-space indentation, descriptive snake_case names, uppercase module constants, and short helper functions for side-effectful steps. Preserve the current stdlib-first import layout and logging style. Shell scripts should remain POSIX-oriented Bash with defensive flags like `set -e`. There is no formatter configured; keep edits consistent with surrounding code and avoid broad reformatting.

## Testing Guidelines
There is no automated test suite yet. Validate changes with a dry-run before touching payment behavior, then inspect `screenshots/` and relevant log output. For safe syntax verification, run `python -m py_compile booker.py`. If you change selectors, login flow, scheduling, or config parsing, document the manual scenario you exercised in the PR.

## Commit & Pull Request Guidelines
Git history is minimal and inconsistent (`Initial Check in`, `high performance`). Prefer short, imperative commit subjects such as `Tighten midnight retry logging` or `Add config validation`. Keep commits focused. PRs should include the user-visible behavior change, commands run for verification, config or secret-handling impact, and screenshots when UI selectors or booking flow changed.

## Security & Configuration Tips
Never commit `.env`, real card data, cookies, or debug network captures. Use `.env.example` as the template, keep `config.json` free of secrets, and scrub logs/screenshots before sharing them externally.
