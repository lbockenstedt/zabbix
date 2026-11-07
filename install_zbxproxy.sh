#!/bin/bash
# Deploys a Zabbix 7.0 ACTIVE proxy (sqlite3) on Debian. Used by the LM "zbxproxy"
# deploy role. The proxy dials out to the server on 10051 (no inbound port needed).
set -e
SERVER=""; PROXY_NAME="$(hostname -s)"; TLS_CA=""; TLS_CERT=""; TLS_KEY=""
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --infra-only|--all-prereqs) ;;
    --server) SERVER="$2"; shift ;;
    --proxy-name) PROXY_NAME="$2"; shift ;;
    --tls-ca) TLS_CA="$2"; shift ;;
    --tls-cert) TLS_CERT="$2"; shift ;;
    --tls-key) TLS_KEY="$2"; shift ;;
    *) echo "Unknown parameter: $1"; exit 1 ;;
  esac
  shift
done
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ -n "$SERVER" ] || { echo "--server <host[:port]> required"; exit 1; }
log() { echo "[zbxproxy] $*"; logger -t lm-zbxproxy "$*" 2>/dev/null || true; }
export DEBIAN_FRONTEND=noninteractive

if ! dpkg -s zabbix-release >/dev/null 2>&1; then
  . /etc/os-release
  case "$ID" in debian|ubuntu|raspbian) DIST="$ID" ;; *) DIST=$(echo "$ID_LIKE" | awk '{print $1}') ;; esac
  case "$DIST" in debian|ubuntu) ;; *) log "unsupported distro: ID=$ID ID_LIKE=$ID_LIKE (need Debian/Ubuntu)"; exit 1 ;; esac
  URL="https://repo.zabbix.com/zabbix/7.0/${DIST}/pool/main/z/zabbix-release/zabbix-release_latest_7.0+${DIST}${VERSION_ID}_all.deb"
  log "fetching $URL"
  curl -fsSL -o /tmp/zbx-release.deb "$URL" || { log "download failed: $URL (no Zabbix 7.0 repo for ${DIST} ${VERSION_ID}, or no internet)"; exit 1; }
  dpkg -i /tmp/zbx-release.deb
fi
apt-get update
# Default packaged config can't start (no valid Server); block the postinst start, configure, then start.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d
apt-get install -y zabbix-proxy-sqlite3 zabbix-agent2 || { rm -f /usr/sbin/policy-rc.d; exit 1; }
rm -f /usr/sbin/policy-rc.d

CONF=/etc/zabbix/zabbix_proxy.conf
setkey() { grep -q "^$1=" "$CONF" && sed -i "s|^$1=.*|$1=$2|" "$CONF" || echo "$1=$2" >> "$CONF"; }
setkey ProxyMode 0
setkey Server "$SERVER"
setkey Hostname "$PROXY_NAME"
# The proxy DB lives in RAM (/run): the hourly housekeeper's SQLite deletes and
# fsyncs stalled slow VM storage for ~60s, freezing everything else on the host.
# It is only a buffer + config cache; the proxy re-creates it and re-syncs its
# config from the server after a reboot (unsent buffered values are lost).
DB_DIR=/run/zabbix-db
setkey DBName "$DB_DIR/zabbix_proxy.db"
mkdir -p /etc/systemd/system/zabbix-proxy.service.d
cat > /etc/systemd/system/zabbix-proxy.service.d/lm-ramdb.conf <<EOF
[Service]
ExecStartPre=+/usr/bin/install -d -o zabbix -g zabbix -m 750 $DB_DIR
EOF
systemctl daemon-reload
rm -f /var/lib/zabbix/zabbix_proxy.db
if [ -n "$TLS_CERT" ]; then
  setkey TLSConnect cert; setkey TLSAccept cert
  setkey TLSCAFile "$TLS_CA"; setkey TLSCertFile "$TLS_CERT"; setkey TLSKeyFile "$TLS_KEY"
  chgrp zabbix "$TLS_CA" "$TLS_CERT" "$TLS_KEY"; chmod 640 "$TLS_KEY"
  # The packaged agent on this host talks to the local proxy over loopback, so leave it unencrypted.
fi
chown root:zabbix "$CONF"; chmod 640 "$CONF"
mkdir -p /var/lib/zabbix && chown zabbix:zabbix /var/lib/zabbix

systemctl enable zabbix-proxy zabbix-agent2
systemctl restart zabbix-proxy
sleep 3
systemctl is-active zabbix-proxy && log "proxy '$PROXY_NAME' active, server=$SERVER"
