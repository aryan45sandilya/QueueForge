#!/usr/bin/env bash
# Run PostgreSQL and Redis from portable binaries when Docker isn't available.
# Expects the binaries in .devservices/ (gitignored):
#   .devservices/pgsql/bin/{initdb,pg_ctl,psql}   (EnterpriseDB "binaries" zip, or any PostgreSQL 14+)
#   .devservices/redis/redis-server               (Redis 5+; Windows builds: tporadowski/redis)
# Ports avoid clashing with system services: PostgreSQL 54329, Redis 63799.
#
#   scripts/devservices.sh start|stop|status
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/.devservices"
PG_BIN="$DIR/pgsql/bin"
PG_DATA="$DIR/pgdata"
PG_PORT=54329
REDIS_PORT=63799
EXE=""
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) EXE=".exe" ;; esac

start() {
  if [ ! -d "$PG_DATA" ]; then
    echo "queueforge" > "$DIR/pwfile"
    "$PG_BIN/initdb$EXE" -D "$PG_DATA" -U queueforge --pwfile="$DIR/pwfile" -A scram-sha-256 -E UTF8 --locale=C >/dev/null
    rm -f "$DIR/pwfile"
    created=1
  fi
  "$PG_BIN/pg_ctl$EXE" -D "$PG_DATA" -o "-p $PG_PORT -c listen_addresses=127.0.0.1" -l "$DIR/pg.log" -w start
  if [ "${created:-0}" = 1 ]; then
    PGPASSWORD=queueforge "$PG_BIN/psql$EXE" -h 127.0.0.1 -p $PG_PORT -U queueforge -d postgres \
      -c "CREATE DATABASE queueforge;" -c "CREATE DATABASE queueforge_test;"
  fi
  if ! "$DIR/redis/redis-cli$EXE" -p $REDIS_PORT ping >/dev/null 2>&1; then
    (nohup "$DIR/redis/redis-server$EXE" --port $REDIS_PORT --bind 127.0.0.1 --save "" --appendonly no \
      > "$DIR/redis.log" 2>&1 &)
  fi
  status
  cat <<EOF

Use these in backend/.env:
  QF_DATABASE_URL=postgresql+asyncpg://queueforge:queueforge@127.0.0.1:$PG_PORT/queueforge
  QF_REDIS_URL=redis://127.0.0.1:$REDIS_PORT/0
Tests:
  QF_TEST_DATABASE_URL=postgresql+asyncpg://queueforge:queueforge@127.0.0.1:$PG_PORT/queueforge_test
  QF_TEST_REDIS_URL=redis://127.0.0.1:$REDIS_PORT/15
EOF
}

stop() {
  "$PG_BIN/pg_ctl$EXE" -D "$PG_DATA" -m fast stop || true
  "$DIR/redis/redis-cli$EXE" -p $REDIS_PORT shutdown nosave 2>/dev/null || true
}

status() {
  "$PG_BIN/pg_ctl$EXE" -D "$PG_DATA" status >/dev/null 2>&1 && echo "postgres: running on $PG_PORT" || echo "postgres: stopped"
  "$DIR/redis/redis-cli$EXE" -p $REDIS_PORT ping >/dev/null 2>&1 && echo "redis:    running on $REDIS_PORT" || echo "redis:    stopped"
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
