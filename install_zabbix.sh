#!/bin/bash
# Installs the LM Zabbix spoke (monitoring module). Zabbix server/proxy
# packages are installed by the deploy roles, not by this script.

_deployed_branch() {
  local b
  b=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  case "$b" in ""|HEAD) echo main ;; *) echo "$b" ;; esac
}

set -e

HUB_URL="auto"
SPOKE_ID="${SPOKE_ID:-zabbix-$(hostname -s)}"
SPOKE_SECRET="lm-secret"

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub) HUB_URL="$2"; shift ;;
        --id|--name) SPOKE_ID="$2"; shift ;;
        --secret) SPOKE_SECRET="$2"; shift ;;
        --hub-secret) HUB_SECRET="$2"; shift ;;
        --all-prereqs) ;;  # accepted for LM hub compat
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -n "${HUB_URL:-}" ] && [ "$HUB_URL" != "auto" ]; then
    case "$HUB_URL" in
        ws://*|wss://*) : ;;
        *:[0-9]*)       HUB_URL="wss://${HUB_URL}" ;;
        *)              HUB_URL="wss://${HUB_URL}:443" ;;
    esac
fi

if [ -z "$SPOKE_SECRET" ] || [ "$SPOKE_SECRET" == "lm-secret" ]; then
    SPOKE_SECRET=""
    echo "No pre-shared secret - spoke will connect unauthenticated and await admin approval in the LM WebUI."
fi

echo "Installing Zabbix module (native)..."

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root."
    exit 1
fi

apt-get update
apt-get install -y python3-pip python3-venv git curl

INSTALL_DIR="/opt/lm"
SERVICE_NAME="lm-zabbix"
mkdir -p "$INSTALL_DIR" /var/log/lm
cd "$INSTALL_DIR"

if [ -d "zabbix" ]; then
    SPOKE_PATH="$INSTALL_DIR/zabbix"
    cd "$SPOKE_PATH"
    BR=$(_deployed_branch) && git fetch origin -q "$BR" && git reset --hard "origin/$BR"
    cd "$INSTALL_DIR"
else
    git clone --branch "${ZABBIX_BRANCH:-main}" https://github.com/lbockenstedt/zabbix.git
    SPOKE_PATH="$INSTALL_DIR/zabbix"
fi

chown -R svc_lm:svc_lm "$SPOKE_PATH" 2>/dev/null || true
runuser -u svc_lm -- git config --global --add safe.directory "$SPOKE_PATH" 2>/dev/null || true

cd "$SPOKE_PATH"
rm -rf venv
python3 -m venv venv
./venv/bin/python3 -m pip install --upgrade pip -q
./venv/bin/python3 -m pip install -r requirements.txt -q

# Preserve the install UUID (hub fingerprint) across re-installs.
INSTALL_UUID_LINE=""
if [ -f .env ] && grep -q "^INSTALL_UUID=" .env; then
    EXISTING_UUID=$(grep "^INSTALL_UUID=" .env | cut -d= -f2-)
    [ -n "$EXISTING_UUID" ] && INSTALL_UUID_LINE="INSTALL_UUID=$EXISTING_UUID"
fi
cat <<EOF > .env
HUB_URL=$HUB_URL
SPOKE_ID=$SPOKE_ID
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=$HUB_SECRET
${INSTALL_UUID_LINE}
EOF

cat <<EOF > /etc/systemd/system/${SERVICE_NAME}.service
[Unit]
Description=Lab Manager Spoke - Zabbix
After=network.target

[Service]
Type=simple
User=svc_lm
WorkingDirectory=$SPOKE_PATH
EnvironmentFile=$SPOKE_PATH/.env
Environment="PYTHONPATH=$INSTALL_DIR:$INSTALL_DIR/core/src:$SPOKE_PATH/src"
ExecStart=$SPOKE_PATH/venv/bin/python3 -m src.control_plane --id=\${SPOKE_ID} --secret=\${SPOKE_SECRET} --hub=\${HUB_URL} --hub-secret=\${HUB_SECRET}
StandardOutput=append:/var/log/lm/${SERVICE_NAME}.log
StandardError=append:/var/log/lm/${SERVICE_NAME}.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "Zabbix module installed. Hub: $HUB_URL  Spoke ID: $SPOKE_ID  Version: $(cat VERSION 2>/dev/null || echo unknown)"
