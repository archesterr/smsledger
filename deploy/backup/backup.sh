#!/bin/sh
# pg_dump -> restic: encrypted, deduplicated, offsite. Keeps 7 daily, 4 weekly, 12 monthly.
#   loop   (default) back up now, then every day at BACKUP_HOUR:30
#   now    one backup
#   list   show snapshots
#   dump   write the latest dump to stdout (for restores, see README)
set -eu
set -o pipefail

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY in backup.env}"
: "${RESTIC_PASSWORD:?set RESTIC_PASSWORD in backup.env}"
export PGHOST="${PGHOST:-db}" PGUSER="${PGUSER:-smsledger}" PGDATABASE="${PGDATABASE:-smsledger}"

log() { echo "$(date -Iseconds) backup: $*"; }

backup() {
  restic cat config >/dev/null 2>&1 || restic init
  pg_dump --format=custom --no-owner --no-privileges \
    | restic backup --stdin --stdin-filename smsledger.dump --tag smsledger --host smsledger --quiet
  restic forget --tag smsledger --host smsledger --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune --quiet
  restic check --read-data-subset=10% --quiet   # spot-check that stored data is readable
  touch /tmp/last-success
  log "ok ($(restic snapshots --tag smsledger --json | grep -o '"short_id"' | wc -l) snapshots)"
}

seconds_until_next_run() {
  now=$(date +%s)
  next=$(date -d "$(date +%Y-%m-%d) $(printf '%02d' "${BACKUP_HOUR:-3}"):30:00" +%s)
  [ "$next" -gt "$now" ] || next=$((next + 86400))
  echo $((next - now))
}

case "${1:-loop}" in
  now) backup ;;
  list) restic snapshots --tag smsledger ;;
  dump) restic dump --tag smsledger latest smsledger.dump ;;
  loop)
    backup || log "FAILED"
    while :; do
      sleep "$(seconds_until_next_run)"
      backup || log "FAILED"
    done
    ;;
  *) exec "$@" ;;
esac
