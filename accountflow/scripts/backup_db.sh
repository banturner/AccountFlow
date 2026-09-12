#!/usr/bin/env bash
# Nightly logical backup of the AccountFlow Postgres volume on KVM2.
#
# The box's existing `backup` container knows nothing about
# accountflow_postgres_data, so until it does this is the only copy of pilot
# data. Install as root on KVM2 once /opt/accountflow is deployed:
#
#   install -m 750 /opt/accountflow/scripts/backup_db.sh /usr/local/bin/accountflow-backup
#   (crontab -l 2>/dev/null; echo '15 3 * * * /usr/local/bin/accountflow-backup >> /var/log/accountflow-backup.log 2>&1') | crontab -
#   /usr/local/bin/accountflow-backup      # run once by hand and check the output
#
# Restore:
#   gunzip -c /root/backups/accountflow/accountflow_YYYY-MM-DD.sql.gz \
#     | docker exec -i accountflow-db psql -U accountflow accountflow
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/root/backups/accountflow}"
# PDPA pack, Schedule B: deleted data ages out of backups within 35 days.
KEEP_DAYS="${KEEP_DAYS:-35}"
CONTAINER="${CONTAINER:-accountflow-db}"
DB_USER="${DB_USER:-accountflow}"
DB_NAME="${DB_NAME:-accountflow}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

target="$BACKUP_DIR/accountflow_$(date +%F).sql.gz"
tmp="$target.tmp"

docker exec "$CONTAINER" pg_dump -U "$DB_USER" --no-owner "$DB_NAME" | gzip > "$tmp"
mv -f "$tmp" "$target"
chmod 600 "$target"

find "$BACKUP_DIR" -name 'accountflow_*.sql.gz' -mtime +"$KEEP_DAYS" -delete

echo "$(date -Is) backup ok: $target ($(du -h "$target" | cut -f1))"
