#!/bin/bash
# Deploys the Zabbix 7.0 server + web frontend (nginx) on Debian. Used by the LM
# "zabbix-server" deploy role. Without --db-host a local PostgreSQL is installed
# (scratch/lab); with --db-host the external database (Azure Flexible Server) is used.
set -e
DB_HOST=""; DB_PORT=5432; DB_NAME=zabbix; DB_USER=zabbix; DB_PASS=""; SERVER_NAME="_"
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --infra-only|--all-prereqs) ;;
    --db-host) DB_HOST="$2"; shift ;;
    --db-port) DB_PORT="$2"; shift ;;
    --db-name) DB_NAME="$2"; shift ;;
    --db-user) DB_USER="$2"; shift ;;
    --db-pass) DB_PASS="$2"; shift ;;
    --server-name) SERVER_NAME="$2"; shift ;;
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

export PGPASSWORD="$DB_PASS"
if ! psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tc "SELECT 1 FROM pg_tables WHERE tablename='users'" | grep -q 1; then
  log "importing initial schema"
  zcat /usr/share/zabbix-sql-scripts/postgresql/server.sql.gz | psql -q -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME"
fi

CONF=/etc/zabbix/zabbix_server.conf
sed -i -e "s|^#\? *DBHost=.*|DBHost=$DB_HOST|" -e "s|^#\? *DBName=.*|DBName=$DB_NAME|" \
       -e "s|^#\? *DBUser=.*|DBUser=$DB_USER|" -e "s|^#\? *DBPassword=.*|DBPassword=$DB_PASS|" \
       -e "s|^#\? *DBPort=.*|DBPort=$DB_PORT|" "$CONF"
chmod 640 "$CONF"

sed -i -e "s|^#\? *listen .*80;|        listen 80;|" -e "s|^#\? *server_name .*|        server_name $SERVER_NAME;|" \
  /etc/zabbix/nginx.conf
rm -f /etc/nginx/sites-enabled/default

systemctl enable zabbix-server zabbix-agent2 nginx php8.4-fpm 2>/dev/null || systemctl enable zabbix-server zabbix-agent2 nginx
systemctl restart zabbix-server zabbix-agent2 nginx
systemctl restart "$(systemctl list-unit-files 'php*-fpm.service' --no-legend | awk 'NR==1{print $1}')" 2>/dev/null || true
sleep 3
systemctl is-active zabbix-server && log "zabbix-server installed and active (db=$DB_HOST)"
