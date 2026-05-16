#!/usr/bin/env bash
# Deploys the AI Agent alarm worker as a pair of systemd services.
#
#   ai-agent-setup.service  — oneshot, runs on every boot to provision
#                             per-instance SQS/SNS/CloudWatch alarms and
#                             patch .env with the queue URL
#   ai-agent.service        — long-running SQS polling worker (alarm_worker.py)
#
# Usage: sudo bash scripts/setup_alarm_worker.sh
# Run from any directory — paths are resolved from the script location.

set -euo pipefail

# Require root
if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: Run this script with sudo." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="/var/log/ai-agent"
SYSTEMD_DIR="/etc/systemd/system"

# Bootstrap venv if it doesn't exist yet
if [[ ! -f "${REPO_DIR}/venv/bin/python" && ! -f "${REPO_DIR}/venv/bin/python3" ]]; then
  echo "==> No virtualenv found — creating one"
  # fastmcp requires Python 3.10+; prefer python3.12, fall back down the chain
  PY_BIN="$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3 || command -v python || true)"
  if [[ -z "${PY_BIN}" ]]; then
    echo "ERROR: Python 3.10+ not found. Install it first:"
    echo "       sudo dnf install -y python3.12   # Amazon Linux 2023"
    exit 1
  fi
  PY_VER="$("${PY_BIN}" -c 'import sys; print(sys.version_info[:2])')"
  if [[ "${PY_VER}" < "(3, 10)" ]]; then
    echo "ERROR: Found ${PY_BIN} but it is Python ${PY_VER} — need 3.10+."
    echo "       sudo dnf install -y python3.12"
    exit 1
  fi
  "${PY_BIN}" -m venv "${REPO_DIR}/venv"
  "${REPO_DIR}/venv/bin/pip" install --quiet --upgrade pip
  "${REPO_DIR}/venv/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"
fi

# Resolve the venv python binary (venv always creates a python3 symlink, and
# usually a python one too — prefer the explicit python3 to be safe)
VENV_PYTHON="${REPO_DIR}/venv/bin/python3"
[[ -x "${VENV_PYTHON}" ]] || VENV_PYTHON="${REPO_DIR}/venv/bin/python"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "ERROR: venv exists but no python binary found inside it." >&2
  exit 1
fi

# Configure AWS credentials for ec2-user if not already present.
# boto3 checks ~/.aws/credentials before falling back to the EC2 instance role.
# The alarm worker needs the ai-agent IAM user's permissions (SSM, EC2, CloudWatch…)
# which the instance role does not have.
EC2_USER_HOME="/home/ec2-user"
AWS_CREDS_FILE="${EC2_USER_HOME}/.aws/credentials"

if [[ ! -f "${AWS_CREDS_FILE}" ]]; then
  echo ""
  echo "==> ~/.aws/credentials not found for ec2-user"
  echo "    The alarm worker uses the ai-agent IAM user to call SSM, EC2,"
  echo "    CloudWatch, and SQS — the EC2 instance role does not cover these."
  echo ""
  read -rp "    AWS_ACCESS_KEY_ID    : " _key_id
  read -rsp "    AWS_SECRET_ACCESS_KEY: " _secret_key
  echo ""
  mkdir -p "${EC2_USER_HOME}/.aws"
  cat > "${AWS_CREDS_FILE}" <<CREDS
[default]
aws_access_key_id = ${_key_id}
aws_secret_access_key = ${_secret_key}
CREDS
  chown -R ec2-user:ec2-user "${EC2_USER_HOME}/.aws"
  chmod 600 "${AWS_CREDS_FILE}"
  echo "==> Credentials written to ${AWS_CREDS_FILE}"
else
  echo "==> AWS credentials already present at ${AWS_CREDS_FILE} — skipping"
fi

echo "==> Creating log directory ${LOG_DIR}"
mkdir -p "${LOG_DIR}"
chmod 755 "${LOG_DIR}"

echo "==> Writing ${SYSTEMD_DIR}/ai-agent.service"
cat > "${SYSTEMD_DIR}/ai-agent.service" <<EOF
[Unit]
Description=AI Agent Alarm Worker
After=network.target

[Service]
User=ec2-user
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_PYTHON} alarm_worker.py
Restart=always
RestartSec=5
Environment=PYTHONPATH=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:${LOG_DIR}/agent.log
StandardError=append:${LOG_DIR}/agent.log

[Install]
WantedBy=multi-user.target
EOF

echo "==> Writing ${SYSTEMD_DIR}/ai-agent-setup.service"
cat > "${SYSTEMD_DIR}/ai-agent-setup.service" <<EOF
[Unit]
Description=AI Agent per-instance alarm setup
Before=ai-agent.service
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_PYTHON} ${REPO_DIR}/scripts/instance_alarm_setup.py
ExecStartPost=/bin/systemctl --no-block restart ai-agent.service
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "==> Reloading systemd and enabling services"
systemctl daemon-reload
systemctl enable ai-agent-setup.service ai-agent.service
systemctl start ai-agent-setup.service

echo ""
echo "Done. Both services are enabled and started."
echo "  Alarm worker status : sudo systemctl status ai-agent.service"
echo "  Setup service status : sudo systemctl status ai-agent-setup.service"
echo "  Live logs            : sudo tail -f ${LOG_DIR}/agent.log"
