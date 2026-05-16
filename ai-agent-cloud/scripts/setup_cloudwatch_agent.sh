#!/usr/bin/env bash
# Installs the Amazon CloudWatch Agent and applies the project configuration.
#
# Config source: config/observability/amazon-cloudwatch-agent.json
# Collected logs  : /var/log/ai-agent/agent.log, /var/log/messages, /var/log/mern-app/api.log
# Collected metrics: disk, diskio, mem, swap (60 s interval)
#
# Usage: sudo bash scripts/setup_cloudwatch_agent.sh

set -euo pipefail

# Require root
if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: Run this script with sudo." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CW_CONFIG="${REPO_DIR}/config/observability/amazon-cloudwatch-agent.json"
CW_CTL="/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl"

if [[ ! -f "${CW_CONFIG}" ]]; then
  echo "ERROR: Config not found at ${CW_CONFIG}" >&2
  exit 1
fi

# Create log directories referenced in the agent config before starting the agent
echo "==> Creating log directories"
mkdir -p /var/log/ai-agent /var/log/mern-app
chmod 755 /var/log/ai-agent /var/log/mern-app

# Install the agent if the binary is not already present
if [[ ! -f "${CW_CTL}" ]]; then
  echo "==> Installing Amazon CloudWatch Agent"
  if command -v yum &>/dev/null; then
    yum install -y amazon-cloudwatch-agent
  elif command -v apt-get &>/dev/null; then
    apt-get install -y amazon-cloudwatch-agent
  else
    echo "ERROR: Could not detect a supported package manager (yum / apt-get)."
    echo "       Install the agent manually and re-run this script:"
    echo "       https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/install-CloudWatch-Agent-on-EC2-Instance.html"
    exit 1
  fi
fi

# fetch-config applies the config, -s starts (or restarts) the agent immediately
echo "==> Applying configuration from ${CW_CONFIG}"
"${CW_CTL}" -a fetch-config -m ec2 -s -c "file:${CW_CONFIG}"

echo ""
echo "Done. Amazon CloudWatch Agent is running."
echo "  Status : sudo systemctl status amazon-cloudwatch-agent"
echo "  Logs   : sudo tail -f /opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log"
