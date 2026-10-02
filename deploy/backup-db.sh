#!/bin/sh
# Ежедневный дамп PostgreSQL с хранением 14 дней.
#   cron: 15 3 * * * /root/logist-agent/deploy/backup-db.sh >> /root/backups/backup.log 2>&1
# Пароль берётся из DATABASE_URL в .env (postgresql+asyncpg://user:pass@host/db).
set -eu

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/root/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

URL="$(grep '^DATABASE_URL=' "$APP_DIR/.env" | head -n1 | cut -d= -f2-)"
case "$URL" in
  postgresql*) ;;
  *) echo "DATABASE_URL не PostgreSQL, бэкап пропущен"; exit 0 ;;
esac

PG_URL="$(echo "$URL" | sed 's#^postgresql+asyncpg://#postgresql://#')"
mkdir -p "$BACKUP_DIR"
FILE="$BACKUP_DIR/logist-$(date +%Y%m%d-%H%M%S).sql.gz"

pg_dump --no-owner --dbname="$PG_URL" | gzip -9 > "$FILE.tmp"
mv "$FILE.tmp" "$FILE"
chmod 600 "$FILE"
find "$BACKUP_DIR" -name 'logist-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) бэкап готов: $FILE ($(du -h "$FILE" | cut -f1))"
