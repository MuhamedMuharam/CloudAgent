# Phases 5 and 6 Console Runbook

## Goal

Deploy a real workload (FastAPI + Celery + Redis) and enable OpenTelemetry traces exported to AWS X-Ray.

## 1. On your EC2 instance (from repo root)

```bash
cd /home/ec2-user/CloudAgent/ai-agent-cloud
chmod +x config/real_service/install_real_service.sh
./config/real_service/install_real_service.sh
```

## 1b. Apply code updates after future git pulls (without full reinstall)

If services are already installed, pulling the repo is not enough because
systemd runs code from `/opt/real-service/src`.

```bash
cd /home/ec2-user/CloudAgent/ai-agent-cloud
git pull

sudo rsync -av --delete config/real_service/src/ /opt/real-service/src/
sudo chown -R ec2-user:ec2-user /opt/real-service/src
sudo cp config/real_service/otel-collector-config.yaml /opt/real-service/otel-collector-config.yaml

sudo /opt/real-service/venv/bin/pip install -r config/real_service/requirements.txt
sudo systemctl restart otel-collector.service real-api.service real-worker.service

# Optional: restart alarm worker if agent prompts/instructions changed
sudo systemctl restart ai-agent.service
```

Quick verification that the running code is updated:

```bash
grep -n '"service": "real-api"' /opt/real-service/src/api.py
grep -n '@app.get("/orders/stats")' /opt/real-service/src/api.py
```

## 2. Restart CloudWatch agent to pick up new log files

```bash
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config \
  -m ec2 \
  -c file:/home/ec2-user/CloudAgent/ai-agent-cloud/config/observability/amazon-cloudwatch-agent.json \
  -s
```

## 3. Verify systemd services

```bash
sudo systemctl status redis6.service --no-pager
sudo systemctl status otel-collector.service --no-pager
sudo systemctl status real-api.service --no-pager
sudo systemctl status real-worker.service --no-pager
```

## 4. Functional tests

```bash
curl -X GET "http://your-ec2-ip:8080/health"

curl -X POST "http://your-ec2-ip:8080/orders" ^
  -H "Content-Type: application/json" ^
  -d "{\"customer_id\":\"cust-1\",\"item_count\":3,\"simulate_failure\":false}"

curl -X GET "http://your-ec2-ip:8080/orders?limit=20"

curl -X GET "http://your-ec2-ip:8080/orders/stats?limit=200"

# Copy order_id manually from POST /orders response
curl -X GET "http://your-ec2-ip:8080/orders/<order_id>"

# Request cancellation (use ?terminate=true to force-kill running worker process)
curl -X POST "http://your-ec2-ip:8080/orders/<order_id>/cancel"

# Copy task_id manually from POST /orders response
curl -X GET "http://your-ec2-ip:8080/tasks/<task_id>"

# Validate unknown task behavior (expected HTTP 404)
curl -i -X GET "http://your-ec2-ip:8080/tasks/does-not-exist"
```

Use your EC2 public IP in place of `your-ec2-ip`, then copy `order_id` and `task_id` manually from the create-order response.

## 5. Log checks

```bash
tail -n 50 /var/log/ai-agent/app.log
tail -n 50 /var/log/ai-agent/worker.log
tail -n 50 /var/log/ai-agent/otel-collector.log
```

## 6. AWS-side trace validation

1. Open AWS X-Ray Trace Map for your region.
2. Generate a few `/orders` requests.
3. Confirm traces appear with API and worker spans.

## 7. Agent-side trace checks (MCP tools)

Use these tools from your agent goals:

- `aws_get_xray_trace_summaries`
- `aws_get_xray_trace_details`
- `aws_get_xray_service_graph`

Example intent:
"Get X-Ray trace summaries for last 15 minutes and identify error traces for real-api"
