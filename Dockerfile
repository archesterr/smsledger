FROM python:3.12-alpine
WORKDIR /srv
COPY app/ app/
RUN mkdir /data && chown 65534:65534 /data
USER 65534:65534
ENV DB_PATH=/data/ledger.db LISTEN=0.0.0.0:8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["python", "-m", "app.server"]
