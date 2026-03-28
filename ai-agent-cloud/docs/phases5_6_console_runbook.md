# Phases 5 and 6 Console Runbook

## Goal

Deploy a real workload (FastAPI + Celery + Redis) and enable OpenTelemetry traces exported to AWS X-Ray.

## 1. On your EC2 instance (from repo root)

```bash
cd /home/ec2-user/CloudAgent/ai-agent-cloud
chmod +x config/real_service/install_real_service.sh
./config/real_service/install_real_service.sh
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
curl -s http://127.0.0.1:8080/health

curl -s -X POST http://127.0.0.1:8080/orders \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":"cust-1","item_count":3,"simulate_failure":false}'
```

Take `task_id` from response and check state:

```bash
curl -s http://127.0.0.1:8080/tasks/<task_id>
```

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
