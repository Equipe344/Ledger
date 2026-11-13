#!/bin/sh
# Container startup for the Django backend.
#
# Runs at *runtime*, not build time, so no secret is ever baked into the image:
# the platform injects SECRET_KEY / DATABASE_URL / PAYSTACK_* as env vars.
set -e

echo "Applying database migrations..."
python manage.py migrate --noinput
# Shared rate-limit counters live in this table (idempotent).
python manage.py createcachetable

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Gunicorn on 0.0.0.0:8000..."
exec gunicorn core.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-2}" \
    --access-logfile - \
    --error-logfile -
