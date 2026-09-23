# restic is copied from its official, version-pinned image (no package repos needed at build).
FROM restic/restic:0.18.1 AS restic

# pg_dump must be the same Postgres major as the db service in docker-compose.yml
FROM postgres:17-alpine
COPY --from=restic /usr/bin/restic /usr/local/bin/restic
COPY backup.sh /usr/local/bin/backup
ENV RESTIC_CACHE_DIR=/tmp/restic-cache HOME=/tmp
USER postgres
# unhealthy if the last successful backup is older than 26 hours
HEALTHCHECK --interval=10m --timeout=10s --start-period=15m \
  CMD test -f /tmp/last-success && [ $(( $(date +%s) - $(stat -c %Y /tmp/last-success) )) -lt 93600 ]
ENTRYPOINT ["backup"]
CMD ["loop"]
