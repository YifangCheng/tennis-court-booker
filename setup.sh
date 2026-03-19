#!/usr/bin/env bash
# One-time setup. Run this once after cloning/downloading the project.
# Usage: bash setup.sh

set -e
cd "$(dirname "$0")"

echo "=== Tennis Court Booker — Setup ==="

# Create virtual environment
echo ""
echo "1. Creating virtual environment …"
python3 -m venv .venv
source .venv/bin/activate

# Install Python packages
echo ""
echo "2. Installing Python packages …"
pip install --quiet --upgrade pip
pip install -r requirements.txt

# Install Playwright's Chromium browser
echo ""
echo "3. Installing Playwright Chromium …"
playwright install chromium

# Create .env from template
echo ""
echo "4. Creating .env …"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "   .env created. Fill in your login and payment details."
else
    echo "   .env already exists — skipping."
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env"
echo "  2. Test the script (no payment made):"
echo "     source .venv/bin/activate"
echo "     python main.py --site raynes_park --debug --now"
echo "  3. Check screenshots/ to verify selectors work"
echo "  4. When ready, schedule the midnight run:"
echo "     bash schedule.sh --site raynes_park"
