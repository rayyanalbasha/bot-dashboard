#!/usr/bin/env bash
# Runs the Discord bot and the FastAPI dashboard together, in the same
# process group, on the same filesystem, so they share config.db.
# Set this as the Render "Start Command".
set -e

python bot.py &
BOT_PID=$!

uvicorn dashboard:app --host 0.0.0.0 --port "${PORT:-8000}" &
WEB_PID=$!

# If either process dies, kill the other and exit so Render restarts cleanly.
wait -n "$BOT_PID" "$WEB_PID"
kill "$BOT_PID" "$WEB_PID" 2>/dev/null || true