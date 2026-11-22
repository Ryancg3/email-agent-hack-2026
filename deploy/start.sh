#!/usr/bin/env bash
# Container entrypoint: migrate, then run the web server and the scheduler side by
# side. If either one exits, stop the container so the host restarts both.
set -euo pipefail

python manage.py migrate --noinput

python manage.py run_scheduler &
gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers 1 --threads 4 \
    --access-logfile - &

wait -n || true
echo "A process exited; stopping so the host restarts the container." >&2
exit 1
