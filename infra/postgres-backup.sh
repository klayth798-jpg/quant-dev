#!/bin/sh
set -eu

interval="${BACKUP_INTERVAL_SECONDS:-86400}"
retention="${BACKUP_RETENTION_DAYS:-14}"

while true; do
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  target="/backups/quantdev-${timestamp}.dump"
  pg_dump --format=custom --no-owner --file="$target"
  find /backups -type f -name 'quantdev-*.dump' -mtime "+$retention" -delete
  sleep "$interval"
done
