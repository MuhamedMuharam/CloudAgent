#!/bin/bash
set -euo pipefail

APP_DIR="/opt/ai-observability"
APP_FILE="$APP_DIR/demo_service.py"
LOG_DIR="/var/log/ai-agent"
SERVICE_NAME="ai-observability-demo"

if ! command -v dnf >/dev/null 2>&1; then
  echo "This installer expects Amazon Linux 2023 (dnf)."
  exit 1
fi

sudo dnf update -y
sudo dnf install -y python3 python3-pip

sudo mkdir -p "$APP_DIR"
sudo mkdir -p "$LOG_DIR"
sudo touch "$LOG_DIR/app.log"
sudo chown -R ec2-user:ec2-user "$APP_DIR" "$LOG_DIR"

#sudo pip3 install --upgrade pip
sudo pip3 install flask

cat <<'PYEOF' | sudo tee "$APP_FILE" >/dev/null
"""
Demo Observability Service for EC2
Provides controllable workload and fault endpoints for CloudWatch testing.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request


app = Flask(__name__)

LOG_PATH = os.getenv("APP_LOG_PATH", "/var/log/ai-agent/app.log")
APP_NAME = os.getenv("APP_NAME", "ai-observability-demo")

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logger = logging.getLogger("ai-observability-demo")
logger.setLevel(logging.INFO)
handler = logging.FileHandler(LOG_PATH)
handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(handler)


def emit(level: str, event: str, **extra):
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "app": APP_NAME,
        "event": event,
    }
    payload.update(extra)
    line = json.dumps(payload)
    if level == "ERROR":
        logger.error(line)
    else:
        logger.info(line)


_cpu_fault_running = False
_cpu_fault_thread = None


def _cpu_burn_loop():
    while _cpu_fault_running:
        _ = sum(i * i for i in range(10000))


@app.get("/health")
def health():
    emit("INFO", "health_check", path="/health")
    return jsonify({"status": "ok", "service": APP_NAME})


@app.get("/load")
def load():
    work_ms = int(request.args.get("ms", "250"))
    start = time.time()
    target = time.time() + (work_ms / 1000.0)
    while time.time() < target:
        _ = sum(i * i for i in range(2000))

    elapsed_ms = int((time.time() - start) * 1000)
    emit("INFO", "load_request", path="/load", requested_ms=work_ms, elapsed_ms=elapsed_ms)
    return jsonify({"ok": True, "elapsed_ms": elapsed_ms})


@app.get("/fault/error")
def fault_error():
    emit("ERROR", "intentional_error", path="/fault/error", message="Injected test fault")
    return jsonify({"ok": False, "fault": "error", "message": "Intentional error for alarm testing"}), 500


@app.post("/fault/cpu/start")
def fault_cpu_start():
    global _cpu_fault_running, _cpu_fault_thread
    if _cpu_fault_running:
        return jsonify({"ok": True, "message": "CPU fault already running"})

    _cpu_fault_running = True
    _cpu_fault_thread = threading.Thread(target=_cpu_burn_loop, daemon=True)
    _cpu_fault_thread.start()
    emit("ERROR", "cpu_fault_started")
    return jsonify({"ok": True, "message": "CPU fault started"})


@app.post("/fault/cpu/stop")
def fault_cpu_stop():
    global _cpu_fault_running
    _cpu_fault_running = False
    emit("INFO", "cpu_fault_stopped")
    return jsonify({"ok": True, "message": "CPU fault stopped"})


if __name__ == "__main__":
    emit("INFO", "service_start")
    app.run(host="0.0.0.0", port=8080)
PYEOF

cat <<'SVCEOF' | sudo tee /etc/systemd/system/ai-observability-demo.service >/dev/null
[Unit]
Description=AI Observability Demo Service
After=network.target

[Service]
Type=simple
User=ec2-user
Environment=APP_LOG_PATH=/var/log/ai-agent/app.log
Environment=APP_NAME=ai-observability-demo
ExecStart=/usr/bin/python3 /opt/ai-observability/demo_service.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable ai-observability-demo
sudo systemctl restart ai-observability-demo
sudo systemctl status ai-observability-demo --no-pager

echo
echo "Service installed. Endpoints:"
echo "  GET  /health"
echo "  GET  /load?ms=500"
echo "  GET  /fault/error"
echo "  POST /fault/cpu/start"
echo "  POST /fault/cpu/stop"
