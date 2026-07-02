#!/bin/bash
# Ежедневный бэкап БД kurotrack. Ретеншн 14 дней.
#
# На хосте нет системного pg_dump совместимой версии (только 10.23, сервер PG 16),
# поэтому используем клиентские бинарники postgresql16, распакованные без root
# в ~/pgtools (взяты из официального PGDG rpm, установка через rpm2cpio без sudo).
set -e

BACKUP_DIR="$HOME/backups/kurotrack"
PG_DUMP="$HOME/pgtools/usr/pgsql-16/bin/pg_dump"
PG_LIBDIR="$HOME/pgtools/usr/pgsql-16/lib"

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%F)
export PGPASSWORD=kuro
export LD_LIBRARY_PATH="$PG_LIBDIR"

"$PG_DUMP" -h 127.0.0.1 -p 5433 -U kuro -d kurotrack --no-owner --no-acl \
  | gzip > "$BACKUP_DIR/kurotrack-$STAMP.sql.gz.tmp"
mv "$BACKUP_DIR/kurotrack-$STAMP.sql.gz.tmp" "$BACKUP_DIR/kurotrack-$STAMP.sql.gz"

# Ретеншн: удаляем старше 14 дней
find "$BACKUP_DIR" -name "kurotrack-*.sql.gz" -mtime +14 -delete

# Контроль: размер сегодняшнего дампа
ls -lh "$BACKUP_DIR/kurotrack-$STAMP.sql.gz"
