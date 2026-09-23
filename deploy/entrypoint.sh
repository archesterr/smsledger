#!/bin/sh
# web: migrate, re-parse unparsed SMS with the parsers in this image, then serve.
# anything else: run it (e.g. `docker compose exec app python manage.py invite --note Ali`).
set -eu

if [ "${1:-web}" = "web" ]; then
  python manage.py migrate --noinput
  python manage.py createcachetable
  python manage.py reparse
  exec gunicorn config.wsgi \
    --bind 0.0.0.0:8000 \
    --workers "${WEB_CONCURRENCY:-3}" --threads 2 --timeout 120 \
    --worker-tmp-dir /dev/shm --no-control-socket \
    --access-logfile - \
    --access-logformat '%({x-forwarded-for}i)s "%(m)s %(U)s" %(s)s %(b)s %(M)sms'
    # %(U)s is the path without the query string: search terms never reach the logs
fi
exec "$@"
