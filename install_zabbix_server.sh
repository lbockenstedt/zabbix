#!/bin/bash
# Deploys the Zabbix 7.0 server + web frontend (nginx) on Debian. Used by the LM
# "zabbix-server" deploy role. Without --db-host a local PostgreSQL is installed
# (scratch/lab); with --db-host the external database (Azure Flexible Server) is used.
set -e
DB_HOST=""; DB_PORT=5432; DB_NAME=zabbix; DB_USER=zabbix; DB_PASS=""; SERVER_NAME="_"
ADMIN_USER=""; ADMIN_PASS=""; TIMESCALE=0; SELF_SIGNED=0
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --infra-only|--all-prereqs) ;;
    --db-host) DB_HOST="$2"; shift ;;
    --db-port) DB_PORT="$2"; shift ;;
    --db-name) DB_NAME="$2"; shift ;;
    --db-user) DB_USER="$2"; shift ;;
    --db-pass) DB_PASS="$2"; shift ;;
    --server-name) SERVER_NAME="$2"; shift ;;
    --admin-user) ADMIN_USER="$2"; shift ;;   # external DB admin: creates the role/db/extension
    --admin-pass) ADMIN_PASS="$2"; shift ;;
    --timescale) TIMESCALE=1 ;;
    --self-signed) SELF_SIGNED=1 ;;           # HTTPS on 443 with a self-signed cert (until LE)
    *) echo "Unknown parameter: $1"; exit 1 ;;
  esac
  shift
done
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
log() { echo "[zabbix-server] $*"; logger -t lm-zabbix-server "$*" 2>/dev/null || true; }
export DEBIAN_FRONTEND=noninteractive

if ! dpkg -s zabbix-release >/dev/null 2>&1; then
  . /etc/os-release
  log "adding Zabbix 7.0 repo (debian${VERSION_ID})"
  curl -fsSL -o /tmp/zbx-release.deb \
    "https://repo.zabbix.com/zabbix/7.0/debian/pool/main/z/zabbix-release/zabbix-release_latest_7.0+debian${VERSION_ID}_all.deb"
  dpkg -i /tmp/zbx-release.deb
fi
apt-get update
apt-get install -y zabbix-server-pgsql zabbix-frontend-php zabbix-nginx-conf zabbix-sql-scripts zabbix-agent2 postgresql-client curl

if [ -z "$DB_HOST" ]; then
  log "no --db-host: installing local PostgreSQL"
  apt-get install -y postgresql
  DB_HOST=localhost
  [ -n "$DB_PASS" ] || DB_PASS=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  runuser -u postgres -- psql -tc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 \
    || runuser -u postgres -- psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS'"
  runuser -u postgres -- psql -tc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
    || runuser -u postgres -- createdb -O "$DB_USER" "$DB_NAME"
fi

if [ -n "$ADMIN_USER" ]; then
  log "preparing external DB as $ADMIN_USER"
  PGPASSWORD="$ADMIN_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$ADMIN_USER" -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$DB_USER') THEN CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS'; END IF; END \$\$;
GRANT $DB_USER TO $ADMIN_USER;
SQL
  PGPASSWORD="$ADMIN_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$ADMIN_USER" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
    || PGPASSWORD="$ADMIN_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$ADMIN_USER" -d postgres -c "CREATE DATABASE $DB_NAME OWNER $DB_USER"
  if [ "$TIMESCALE" = 1 ]; then
    PGPASSWORD="$ADMIN_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$ADMIN_USER" -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"
  fi
fi
export PGPASSWORD="$DB_PASS"
if ! psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tc "SELECT 1 FROM pg_tables WHERE tablename='users'" | grep -q 1; then
  log "importing initial schema"
  zcat /usr/share/zabbix-sql-scripts/postgresql/server.sql.gz | psql -q -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME"
fi

if [ "$TIMESCALE" = 1 ] && ! psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tc "SELECT 1 FROM pg_extension WHERE extname='timescaledb'" | grep -q 1; then
  log "timescaledb extension not enabled in $DB_NAME"; exit 1
fi
if [ "$TIMESCALE" = 1 ] && ! psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tc "SELECT 1 FROM timescaledb_information.hypertables LIMIT 1" | grep -q 1; then
  log "converting history/trends tables to hypertables"
  TS=/usr/share/zabbix-sql-scripts/postgresql/timescaledb/schema.sql
  [ -f "$TS" ] || TS=$(ls /usr/share/zabbix-sql-scripts/postgresql/timescaledb/*schema* | head -1)
  case "$TS" in *.gz) zcat "$TS" ;; *) cat "$TS" ;; esac | psql -q -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME"
fi

CONF=/etc/zabbix/zabbix_server.conf
sed -i -e "s|^#\? *DBHost=.*|DBHost=$DB_HOST|" -e "s|^#\? *DBName=.*|DBName=$DB_NAME|" \
       -e "s|^#\? *DBUser=.*|DBUser=$DB_USER|" -e "s|^#\? *DBPassword=.*|DBPassword=$DB_PASS|" \
       -e "s|^#\? *DBPort=.*|DBPort=$DB_PORT|" "$CONF"
chmod 640 "$CONF"

sed -i -e "s|^#\? *listen .*80;|        listen 80;|" -e "s|^#\? *server_name .*|        server_name $SERVER_NAME;|" \
  /etc/zabbix/nginx.conf
rm -f /etc/nginx/sites-enabled/default
if [ "$SELF_SIGNED" = 1 ]; then
  mkdir -p /etc/zabbix/tls
  [ -f /etc/zabbix/tls/web.crt ] || openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
    -keyout /etc/zabbix/tls/web.key -out /etc/zabbix/tls/web.crt -subj "/CN=$SERVER_NAME" 2>/dev/null
  chmod 640 /etc/zabbix/tls/web.key; chown root:www-data /etc/zabbix/tls/web.key
  sed -i -e "s|^ *listen .*80;|        listen 443 ssl;\n        ssl_certificate /etc/zabbix/tls/web.crt;\n        ssl_certificate_key /etc/zabbix/tls/web.key;|" /etc/zabbix/nginx.conf
fi

systemctl enable zabbix-server zabbix-agent2 nginx php8.4-fpm 2>/dev/null || systemctl enable zabbix-server zabbix-agent2 nginx
systemctl restart zabbix-server zabbix-agent2 nginx
systemctl restart "$(systemctl list-unit-files 'php*-fpm.service' --no-legend | awk 'NR==1{print $1}')" 2>/dev/null || true
sleep 3
systemctl is-active zabbix-server && log "zabbix-server installed and active (db=$DB_HOST)"
