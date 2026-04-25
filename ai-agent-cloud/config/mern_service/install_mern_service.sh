#!/bin/bash
# UniVeranstaltungen MERN Stack - EC2 Deployment Script (Amazon Linux 2023)
# Usage: Set the env vars below in your shell, then run: bash install_mern_service.sh
#
# Required env vars before running:
#   MONGO_URI        - MongoDB Atlas connection string
#   MONGO_DB         - Database name
#   JWT_SECRET       - Random 32+ char string (generate: openssl rand -hex 32)
#   MERN_APP_HOST    - EC2 public IP or domain (e.g. 54.123.45.67)
#
# Optional (features degrade gracefully without them):
#   SENDGRID_API_KEY, FROM_EMAIL, STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, DEEPAI_API_KEY
#
# Optional deployment behavior:
#   MERN_SKIP_CLIENT_TYPECHECK=true  # Build client with Vite only (skip `tsc &&` in npm run build)

set -euo pipefail

# ── Validation ────────────────────────────────────────────────────────────────
: "${MONGO_URI:?MONGO_URI is required}"
: "${MONGO_DB:?MONGO_DB is required}"
: "${JWT_SECRET:?JWT_SECRET is required}"
: "${MERN_APP_HOST:?MERN_APP_HOST is required (EC2 public IP or domain)}"

REPO_URL="${MERN_REPO_URL:-https://github.com/Advanced-Computer-Lab-2025/UniVeranstaltungen.git}"
APP_DIR="/opt/mern-app"
LOG_DIR="/var/log/mern-app"
BACKEND_PORT="${PORT:-4000}"
SENDGRID_API_KEY="${SENDGRID_API_KEY:-}"
FROM_EMAIL="${FROM_EMAIL:-noreply@example.com}"
STRIPE_SECRET_KEY="${STRIPE_SECRET_KEY:-}"
STRIPE_PUBLISHABLE_KEY="${STRIPE_PUBLISHABLE_KEY:-}"
DEEPAI_API_KEY="${DEEPAI_API_KEY:-}"
MERN_SKIP_CLIENT_TYPECHECK="${MERN_SKIP_CLIENT_TYPECHECK:-false}"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   UniVeranstaltungen MERN Stack - Deployment             ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo "Host:     $MERN_APP_HOST"
echo "App dir:  $APP_DIR"
echo "Backend:  port $BACKEND_PORT"

# ── Step 1: OS dependencies ───────────────────────────────────────────────────
echo ""
echo "[1/9] Installing OS dependencies..."
sudo dnf update -y --quiet
sudo dnf install -y git nginx --quiet

# Install Node.js 20 LTS via NodeSource
if ! command -v node >/dev/null 2>&1 || [[ "$(node --version)" != v20* ]]; then
  curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
  sudo dnf install -y nodejs --quiet
fi

node --version
npm --version

# ── Step 2: Directories and log files ─────────────────────────────────────────
echo ""
echo "[2/9] Preparing directories..."
sudo mkdir -p "$APP_DIR" "$LOG_DIR"
sudo touch "$LOG_DIR/api.log"
sudo chown -R ec2-user:ec2-user "$APP_DIR" "$LOG_DIR"

# ── Step 3: Clone repository ──────────────────────────────────────────────────
echo ""
echo "[3/9] Cloning repository..."
if [ -d "$APP_DIR/server" ]; then
  echo "  Repo already present — pulling latest..."
  git -C "$APP_DIR" pull --ff-only
else
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi

# ── Step 4: Backend .env ──────────────────────────────────────────────────────
echo ""
echo "[4/9] Writing backend .env..."
cat > "$APP_DIR/server/.env" <<EOF
NODE_ENV=production
PORT=$BACKEND_PORT
MONGO_URI=$MONGO_URI
MONGO_DB=$MONGO_DB
JWT_SECRET=$JWT_SECRET
FRONTEND_URL=http://$MERN_APP_HOST
SENDGRID_API_KEY=$SENDGRID_API_KEY
FROM_EMAIL=$FROM_EMAIL
STRIPE_SECRET_KEY=$STRIPE_SECRET_KEY
STRIPE_PUBLISHABLE_KEY=$STRIPE_PUBLISHABLE_KEY
DEEPAI_API_KEY=$DEEPAI_API_KEY
EOF
chmod 600 "$APP_DIR/server/.env"

# ── Step 5: Build backend (TypeScript → dist/) ────────────────────────────────
echo ""
echo "[5/9] Building backend..."
cd "$APP_DIR/server"
npm install --prefer-offline 2>&1 | tail -5
npm run build
echo "  Backend build complete → server/dist/"

# ── Step 6: Frontend .env + build (Vite → dist/) ─────────────────────────────
echo ""
echo "[6/9] Building frontend..."
# VITE_API_URL uses a relative path so it works regardless of IP/domain changes
cat > "$APP_DIR/client/.env" <<EOF
VITE_API_URL=/api
EOF

cd "$APP_DIR/client"
npm install --prefer-offline 2>&1 | tail -5
if [[ "$MERN_SKIP_CLIENT_TYPECHECK" == "true" ]]; then
  echo "  MERN_SKIP_CLIENT_TYPECHECK=true -> running Vite build without TypeScript type-check"
  npx vite build
else
  npm run build
fi
echo "  Frontend build complete → client/dist/"

# Correct ownership after npm runs as ec2-user
sudo chown -R ec2-user:ec2-user "$APP_DIR"

# ── Step 7: Systemd unit ──────────────────────────────────────────────────────
echo ""
echo "[7/9] Writing systemd unit..."
cat <<EOF | sudo tee /etc/systemd/system/mern-api.service >/dev/null
[Unit]
Description=UniVeranstaltungen MERN API (Express/TypeScript)
After=network.target
Documentation=https://github.com/Advanced-Computer-Lab-2025/UniVeranstaltungen

[Service]
Type=simple
User=ec2-user
WorkingDirectory=$APP_DIR/server
EnvironmentFile=$APP_DIR/server/.env
ExecStart=/usr/bin/node dist/index.js
Restart=always
RestartSec=5
StandardOutput=append:$LOG_DIR/api.log
StandardError=append:$LOG_DIR/api.log

[Install]
WantedBy=multi-user.target
EOF

# ── Step 8: Nginx config ──────────────────────────────────────────────────────
echo ""
echo "[8/9] Configuring Nginx..."

# Disable default nginx site if present
sudo rm -f /etc/nginx/conf.d/default.conf

cat <<EOF | sudo tee /etc/nginx/conf.d/mern-app.conf >/dev/null
server {
    listen 80;
    server_name _;

    root $APP_DIR/client/dist;
    index index.html;

    # Cache static Vite assets (hashed filenames — safe to cache forever)
    location /assets/ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # Proxy all /api/* requests to Express backend
    location /api/ {
        proxy_pass         http://127.0.0.1:$BACKEND_PORT;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }

    # React SPA fallback — all non-file routes serve index.html
    location / {
        try_files \$uri \$uri/ /index.html;
    }
}
EOF

sudo nginx -t  # validate config before reloading

# ── Step 9: Enable and start services ─────────────────────────────────────────
echo ""
echo "[9/9] Starting services..."
sudo systemctl daemon-reload
sudo systemctl enable mern-api.service nginx
sudo systemctl restart mern-api.service nginx

sleep 3
echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Deployment complete!"
echo ""
echo " Frontend:  http://$MERN_APP_HOST"
echo " API:       http://$MERN_APP_HOST/api"
echo " Logs:      $LOG_DIR/api.log"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo " Service status:"
sudo systemctl is-active mern-api.service nginx

echo ""
echo " Quick health check:"
sleep 2
curl -sf "http://127.0.0.1:$BACKEND_PORT/api/health" && echo " Backend: OK" || echo " Backend: not responding yet (check logs)"
