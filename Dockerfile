# App image. On an Iran VPS Docker Hub / PyPI may be blocked: use a Docker registry mirror and
# PIP_INDEX_URL (see README, "Iran servers"). The FROM line stays literal so Dependabot can
# update it, and CI runs the tests on the same Python version.
FROM python:3.13-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.txt .
# every dependency is pinned with a sha256 hash (pip-compile --generate-hashes)
RUN pip install --require-hashes --index-url "${PIP_INDEX_URL}" -r requirements.txt

COPY manage.py ./
COPY config/ config/
COPY ledger/ ledger/
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod 0755 /usr/local/bin/entrypoint \
 && SECRET_KEY=build-time-collectstatic-only-not-a-real-secret-0123456789abcdef python manage.py collectstatic --noinput -v0

# nobody:nogroup; the root filesystem is mounted read-only in docker-compose.yml
USER 65534:65534
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
ENTRYPOINT ["entrypoint"]
CMD ["web"]
