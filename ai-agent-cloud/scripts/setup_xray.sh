#!/usr/bin/env bash
# Installs the AWS X-Ray daemon and registers it as a systemd service.
#
# The daemon is an infrastructure-level collector: it runs independently of
# any deployed application and forwards trace segments to AWS X-Ray so the
# AI agent can query distributed traces via its xray.py cloud provider module.
#
# Usage: sudo bash scripts/setup_xray.sh

set -euo pipefail

# Require root
if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: Run this script with sudo." >&2
  exit 1
fi

XRAY_BIN="/usr/bin/xray"
XRAY_LOG_DIR="/var/log/xray"
XRAY_CONFIG_DIR="/etc/amazon/xray"
SYSTEMD_DIR="/etc/systemd/system"

# X-Ray assets are hosted in us-east-1 regardless of the instance region
XRAY_RPM_URL="https://s3.us-east-1.amazonaws.com/aws-xray-assets.us-east-1/xray-daemon/aws-xray-daemon-3.x.rpm"
XRAY_DEB_URL="https://s3.us-east-1.amazonaws.com/aws-xray-assets.us-east-1/xray-daemon/aws-xray-daemon-amd64-3.x.deb"

if [[ ! -f "${XRAY_BIN}" ]]; then
  echo "==> Installing AWS X-Ray daemon"
  if command -v yum &>/dev/null; then
    yum install -y "${XRAY_RPM_URL}"
  elif command -v apt-get &>/dev/null; then
    curl -sSL "${XRAY_DEB_URL}" -o /tmp/xray.deb
    dpkg -i /tmp/xray.deb
    rm -f /tmp/xray.deb
  else
    echo "ERROR: Could not detect a supported package manager (yum / apt-get)."
    echo "       Install the daemon manually and re-run this script:"
    echo "       https://docs.aws.amazon.com/xray/latest/devguide/xray-daemon.html"
    exit 1
  fi
else
  echo "==> AWS X-Ray daemon already installed at ${XRAY_BIN}"
fi

echo "==> Ensuring xray user/group exist"
if ! id -u xray &>/dev/null; then
  useradd --system --no-create-home --shell /sbin/nologin xray
fi

echo "==> Creating log and config directories"
mkdir -p "${XRAY_LOG_DIR}" "${XRAY_CONFIG_DIR}"
chown xray:xray "${XRAY_LOG_DIR}"
chmod 755 "${XRAY_LOG_DIR}"

echo "==> Writing ${SYSTEMD_DIR}/xray.service"
cat > "${SYSTEMD_DIR}/xray.service" <<'EOF'
[Unit]
Description=AWS X-Ray Daemon
After=network-online.target

[Service]
Type=exec
WorkingDirectory=/usr/bin/
User=xray
Group=xray
ExecStart=/usr/bin/xray -f /var/log/xray/xray.log
Restart=always
LogsDirectory=xray
LogsDirectoryMode=0755
ConfigurationDirectory=amazon/xray
ConfigurationDirectoryMode=0755

[Install]
WantedBy=network-online.target
EOF

echo "==> Reloading systemd and enabling xray"
systemctl daemon-reload
systemctl enable xray
systemctl restart xray

echo ""
echo "Done. AWS X-Ray daemon is running."
echo "  Status : sudo systemctl status xray"
echo "  Logs   : sudo tail -f ${XRAY_LOG_DIR}/xray.log"
