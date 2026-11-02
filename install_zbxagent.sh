#!/bin/bash
# Deploys zabbix-agent2 in ACTIVE mode, reporting to the tenant's local Zabbix proxy.
# Used by the LM "zbxagent" deploy role; the hub fills in --proxy and --hostname.
set -e
PROXY=""; HOSTNAME_ARG="$(hostname -s)"; TLS_CA=""; TLS_CERT=""; TLS_KEY=""
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --infra-only|--all-prereqs) ;;
    --proxy) PROXY="$2"; shift ;;
    --hostname) HOSTNAME_ARG="$2"; shift ;;
    --tls-ca) TLS_CA="$2"; shift ;;
    --tls-cert) TLS_CERT="$2"; shift ;;
    --tls-key) TLS_KEY="$2"; shift ;;
    *) echo "Unknown parameter: $1"; exit 1 ;;
  esac
  shift
done
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ -n "$PROXY" ] || { echo "--proxy <host[:port]> required"; exit 1; }
log() { echo "[zbxagent] $*"; logger -t lm-zbxagent "$*" 2>/dev/null || true; }
export DEBIAN_FRONTEND=noninteractive

if ! dpkg -s zabbix-release >/dev/null 2>&1; then
  . /etc/os-release
  case "$ID" in debian|ubuntu|raspbian) DIST="$ID" ;; *) DIST=$(echo "$ID_LIKE" | awk '{print $1}') ;; esac
  case "$DIST" in debian|ubuntu) ;; *) log "unsupported distro: ID=$ID ID_LIKE=$ID_LIKE (need Debian/Ubuntu)"; exit 1 ;; esac
  URL="https://repo.zabbix.com/zabbix/7.0/${DIST}/pool/main/z/zabbix-release/zabbix-release_latest_7.0+${DIST}${VERSION_ID}_all.deb"
  log "fetching $URL"
  curl -fsSL -o /tmp/zbx-release.deb "$URL" || { log "download failed: $URL"; exit 1; }
  dpkg -i /tmp/zbx-release.deb
fi
apt-get update
apt-get install -y zabbix-agent2

# LM-authored drop-in; the packaged zabbix_agent2.conf is left untouched.
mkdir -p /etc/zabbix/zabbix_agent2.d
case "$PROXY" in *:*) ADDR="$PROXY" ;; *) ADDR="$PROXY:10051" ;; esac
cat > /etc/zabbix/zabbix_agent2.d/lm-agent.conf <<EOF
Server=${ADDR%%:*}
ServerActive=$ADDR
Hostname=$HOSTNAME_ARG
EOF
if [ -n "$TLS_CERT" ]; then
  chgrp zabbix "$TLS_CA" "$TLS_CERT" "$TLS_KEY"; chmod 640 "$TLS_KEY"
  cat >> /etc/zabbix/zabbix_agent2.d/lm-agent.conf <<EOF
TLSConnect=cert
TLSAccept=cert
TLSCAFile=$TLS_CA
TLSCertFile=$TLS_CERT
TLSKeyFile=$TLS_KEY
EOF
fi
sed -i 's|^Server=127.0.0.1|# Server=127.0.0.1|; s|^ServerActive=127.0.0.1|# ServerActive=127.0.0.1|; s|^Hostname=Zabbix server|# Hostname=Zabbix server|' /etc/zabbix/zabbix_agent2.conf
systemctl enable zabbix-agent2
systemctl restart zabbix-agent2
sleep 3
systemctl is-active zabbix-agent2 && log "agent '$HOSTNAME_ARG' active, reporting to $ADDR"
