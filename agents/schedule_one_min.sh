#!/bin/bash
# Schedule sending a telegram message in 1 minute using at or sleep fallback
BASE="/home/pi/MaiconErick/Umbra"
PY="$BASE/agents/send_telegram.py"
TEXT="${1:-Teste: mensagem em 1 minuto}"

if command -v at >/dev/null 2>&1; then
    echo "python3 $PY '$TEXT'" | at now + 1 minute
    echo "Scheduled with at"
else
    (sleep 60; python3 "$PY" "$TEXT") &
    echo "Scheduled with sleep in background"
fi
