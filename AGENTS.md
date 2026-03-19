# Repository Guidelines

## Project Structure & Module Organization
This repository is intentionally small. `main.py` is the shared entrypoint, and each site plugin owns its own runtime defaults under `sites/<site>/config.json`. Shared helpers live under `shared/`, and site-specific notes belong in the README inside each site folder. `setup.sh` bootstraps the local environment, and `schedule.sh` installs or removes the macOS scheduled run. Runtime artifacts such as `screenshots/`, `logs/`, and debug network captures are generated locally and should not be committed.

## Build, Test, and Development Commands
Run `bash setup.sh` once to create `.venv/`, install Python packages, and install Playwright Chromium. Activate the environment with `source .venv/bin/activate`. Use `python main.py --site raynes_park --debug --now --date 2026-03-04 --time 09:00` for a dry-run, or swap `raynes_park` for another site plugin. Use `python main.py --site club_spark --debug --now --pay --date 2026-03-26 --time 10:00` to exercise a live payment path. Use `bash schedule.sh --site SITE_NAME` to install the macOS wake-and-run job and `bash schedule.sh --uninstall` to remove it.

## Coding Style & Naming Conventions
Follow existing Python style: 4-space indentation, descriptive snake_case names, uppercase module constants, and short helper functions for side-effectful steps. Preserve the current stdlib-first import layout and logging style. Shell scripts should remain POSIX-oriented Bash with defensive flags like `set -e`. There is no formatter configured; keep edits consistent with surrounding code and avoid broad reformatting.

## Testing Guidelines
There is no automated test suite yet. Validate changes with a dry-run before touching payment behavior, then inspect `screenshots/`, `logs/`, and relevant debug captures. For safe syntax verification, run `python -m py_compile main.py shared/runtime.py sites/*/site.py`. If you change selectors, login flow, scheduling, or config parsing, document the manual scenario you exercised in the PR.

## Commit & Pull Request Guidelines
Git history is minimal and inconsistent (`Initial Check in`, `high performance`). Prefer short, imperative commit subjects such as `Tighten midnight retry logging` or `Add config validation`. Keep commits focused. PRs should include the user-visible behavior change, commands run for verification, config or secret-handling impact, and screenshots when UI selectors or booking flow changed.

## Security & Configuration Tips
Never commit `.env`, real card data, cookies, or debug network captures. Use `.env.example` as the template, keep site config files free of secrets, and scrub logs/screenshots before sharing them externally.
