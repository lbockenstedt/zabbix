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
  curl -fsSL -o /tmp/zbx-release.deb \
    "https://repo.zabbix.com/zabbix/7.0/debian/pool/main/z/zabbix-release/zabbix-release_latest_7.0+debian${VERSION_ID}_all.deb"
  dpkg -i /tmp/zbx-release.deb
fi
apt-get update
apt-get install -y zabbix-proxy-sqlite3 zabbix-agent2

CONF=/etc/zabbix/zabbix_proxy.conf
setkey() { grep -q "^$1=" "$CONF" && sed -i "s|^$1=.*|$1=$2|" "$CONF" || echo "$1=$2" >> "$CONF"; }
setkey ProxyMode 0
setkey Server "$SERVER"
setkey Hostname "$PROXY_NAME"
setkey DBName /var/lib/zabbix/zabbix_proxy.db
if [ -n "$TLS_CERT" ]; then
  setkey TLSConnect cert; setkey TLSAccept cert
  setkey TLSCAFile "$TLS_CA"; setkey TLSCertFile "$TLS_CERT"; setkey TLSKeyFile "$TLS_KEY"
fi
mkdir -p /var/lib/zabbix && chown zabbix:zabbix /var/lib/zabbix

systemctl enable zabbix-proxy zabbix-agent2
systemctl restart zabbix-proxy
sleep 3
systemctl is-active zabbix-proxy && log "proxy '$PROXY_NAME' active, server=$SERVER"
