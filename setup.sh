#!/bin/bash
# Umbra setup script
set -e

UMBRA_DIR="/home/pi/MaiconErick/Umbra"
echo "Setting up Umbra at $UMBRA_DIR..."

# Create venv
python3 -m venv "$UMBRA_DIR/venv"
source "$UMBRA_DIR/venv/bin/activate"

# Install deps
pip install --upgrade pip
pip install -r "$UMBRA_DIR/requirements.txt"

# Copy .env
if [ ! -f "$UMBRA_DIR/.env" ]; then
    cp "$UMBRA_DIR/.env.example" "$UMBRA_DIR/.env"
    echo ""
    echo ">>> Edit $UMBRA_DIR/.env and fill in your API keys before running."
fi

echo ""
echo "Setup complete."
echo "To run the bot:  source venv/bin/activate && python umbra.py"
echo "To run the CLI:  source venv/bin/activate && python cli.py"
