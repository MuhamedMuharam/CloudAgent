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
    # Intentional busy loop for CPU alarm testing.
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

    # Simulate app work for deterministic latency and CPU pressure.
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
