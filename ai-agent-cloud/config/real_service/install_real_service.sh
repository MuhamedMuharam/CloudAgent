#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/real-service"
SRC_DIR="$APP_DIR/src"
VENV_DIR="$APP_DIR/venv"
LOG_DIR="/var/log/ai-agent"
OTEL_RPM_URL="https://aws-otel-collector.s3.amazonaws.com/amazon_linux/amd64/latest/aws-otel-collector.rpm"
OTEL_BIN="/opt/aws/aws-otel-collector/bin/aws-otel-collector"

echo "[1/8] Installing OS dependencies..."
sudo dnf update -y
sudo dnf install -y python3 python3-pip redis6
if ! command -v curl >/dev/null 2>&1; then
  sudo dnf install -y curl-minimal || sudo dnf install -y curl
fi

echo "[2/8] Enabling Redis broker..."
sudo systemctl enable redis6
sudo systemctl restart redis6

echo "[3/8] Preparing app directories..."
sudo mkdir -p "$SRC_DIR" "$LOG_DIR"
sudo touch "$LOG_DIR/app.log" "$LOG_DIR/worker.log" "$LOG_DIR/otel-collector.log"
sudo cp -r "$SCRIPT_DIR/src/." "$SRC_DIR/"
sudo cp "$SCRIPT_DIR/otel-collector-config.yaml" "$APP_DIR/otel-collector-config.yaml"
sudo chown -R ec2-user:ec2-user "$APP_DIR" "$LOG_DIR"

echo "[4/8] Creating Python environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo "[5/8] Installing AWS OTel Collector binary (if missing)..."
if [ ! -x "$OTEL_BIN" ]; then
  curl -fsSL "$OTEL_RPM_URL" -o /tmp/aws-otel-collector.rpm
  sudo dnf install -y /tmp/aws-otel-collector.rpm
fi

echo "[6/8] Writing systemd units..."
cat <<'EOF' | sudo tee /etc/systemd/system/otel-collector.service >/dev/null
[Unit]
Description=Real Service OpenTelemetry Collector
After=network.target

[Service]
Type=simple
ExecStart=/opt/aws/aws-otel-collector/bin/aws-otel-collector --config /opt/real-service/otel-collector-config.yaml
Restart=always
RestartSec=5
StandardOutput=append:/var/log/ai-agent/otel-collector.log
StandardError=append:/var/log/ai-agent/otel-collector.log

[Install]
WantedBy=multi-user.target
EOF

cat <<'EOF' | sudo tee /etc/systemd/system/real-api.service >/dev/null
[Unit]
Description=Real Workload FastAPI Service
After=network.target redis6.service otel-collector.service
Wants=redis6.service otel-collector.service

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/opt/real-service/src
Environment=PYTHONPATH=/opt/real-service/src
Environment=APP_ENV=dev
Environment=OTEL_SERVICE_NAME=real-api
Environment=OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
Environment=OTEL_FASTAPI_EXCLUDED_URLS=/health,/healthz
Environment=CELERY_BROKER_URL=redis://127.0.0.1:6379/0
Environment=CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1
ExecStart=/opt/real-service/venv/bin/uvicorn api:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
StandardOutput=append:/var/log/ai-agent/app.log
StandardError=append:/var/log/ai-agent/app.log

[Install]
WantedBy=multi-user.target
EOF

cat <<'EOF' | sudo tee /etc/systemd/system/real-worker.service >/dev/null
[Unit]
Description=Real Workload Celery Worker Service
After=network.target redis6.service otel-collector.service
Wants=redis6.service otel-collector.service

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/opt/real-service/src
Environment=PYTHONPATH=/opt/real-service/src
Environment=APP_ENV=dev
Environment=OTEL_SERVICE_NAME=real-worker
Environment=OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
Environment=CELERY_BROKER_URL=redis://127.0.0.1:6379/0
Environment=CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1
ExecStart=/opt/real-service/venv/bin/celery -A celery_app.celery_app worker --loglevel=INFO --concurrency=2
Restart=always
RestartSec=5
StandardOutput=append:/var/log/ai-agent/worker.log
StandardError=append:/var/log/ai-agent/worker.log

[Install]
WantedBy=multi-user.target
EOF

echo "[7/8] Disabling old demo workload service (if exists)..."
if systemctl list-unit-files | grep -q '^ai-observability-demo.service'; then
  sudo systemctl stop ai-observability-demo.service || true
  sudo systemctl disable ai-observability-demo.service || true
fi

echo "[8/8] Enabling and starting new services..."
sudo systemctl daemon-reload
sudo systemctl enable otel-collector.service real-api.service real-worker.service
sudo systemctl restart otel-collector.service real-api.service real-worker.service

echo "Deployment complete."
echo "Check status with: sudo systemctl status real-api.service real-worker.service otel-collector.service"
