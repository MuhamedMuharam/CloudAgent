# Alarm Worker Guide

## What It Does

`alarm_worker.py` is a long-running SQS polling loop that turns CloudWatch alarm notifications into autonomous AI-driven triage and mitigation. It runs as the `ai-agent.service` systemd unit on each managed EC2 instance.

**High-level flow:**

```
CloudWatch alarm fires
        │
        ▼
SNS topic  (ai-agent-alarms-<instance-id-hex>)
        │
        ▼
SQS queue  (ai-agent-alarms-<instance-id-hex>)
        │
        ▼
alarm_worker.py  polls every LOOP_SLEEP_SECONDS
        │
        ├─ filter: skip non-ALARM states if PROCESS_ONLY_ALARM=true
        │
        ├─ _build_goal()  ← constructs natural-language triage context
        │
        ├─ run_agent_sync(goal)  ← invokes the AI agent
        │        └─ agent optionally calls delegate_observability_analysis
        │               for telemetry-heavy alarms (CPU, disk, memory)
        │
        └─ delete SQS message  ← only if ACK policy allows
```

Both ALARM and OK state transitions are delivered to the SQS queue, so the worker knows when an issue resolves.

---

## Default Alarms

`scripts/instance_alarm_setup.py` creates five alarms per instance when the `ai-agent-setup.service` runs on first boot. All five publish both `AlarmActions` and `OKActions` to the per-instance SNS topic. `TreatMissingData` is set to `notBreaching` on every alarm.

| Alarm name | Metric | Namespace | Statistic | Threshold | Evaluation |
|---|---|---|---|---|---|
| `CPUUtilizationAlarm-{id}` | `CPUUtilization` | `AWS/EC2` | Average | > 70 % | 1 × 5 min |
| `DiskPressureAlarm-{id}` | `disk_used_percent` | `CWAgent` | Average | > 85 % | 2 × 5 min |
| `MemWarningAlarm-{id}` | `mem_used_percent` | `CWAgent` | Average | > 85 % | 5 × 1 min |
| `FailedInstanceAlarm-{id}` | `StatusCheckFailed_Instance` | `AWS/EC2` | Maximum | > 0 | 2 × 1 min |
| `SystemFailureAlarm-{id}` | `StatusCheckFailed_System` | `AWS/EC2` | Maximum | > 0 | 2 × 1 min |

`{id}` is the full EC2 instance ID (e.g. `i-0abc1234567890def`).

> `DiskPressureAlarm` requires the CloudWatch agent to be running and publishing `disk_used_percent`. The alarm dimensions (`path`, `device`, `fstype`) are set at the top of `instance_alarm_setup.py` and default to the root volume of Amazon Linux 2023. Adjust them if your instance uses a different mount point or device name.

You can attach additional alarms to the same SNS topic without modifying any worker code — see [Part 2, step 2.11 in README.md](../README.md#211--add-custom-cloudwatch-alarms-optional).

---

## Alarm Classification

The worker classifies each incoming alarm into a family before building the goal. This controls which diagnostic scope the observability sub-agent is given.

| Family | Trigger conditions | Default scope hint |
|---|---|---|
| `ec2_system_status` | `StatusCheckFailed_System` metric or `systemfailure` in name | Host health and status checks |
| `ec2_instance_status` | `StatusCheckFailed_Instance` metric or `failedinstance` in name | Host health and status checks |
| `disk_pressure` | `disk_used_percent` metric or `disk` in name | Disk metrics, logs, filesystem checks, inode pressure |
| `memory_pressure` | `mem_used_percent` metric or `memory`/`memwarning` in name | Memory metrics and app logs |
| `cpu_pressure` | `CPUUtilization` metric or `cpu` in name | CPU metrics then correlated logs |
| `application_error` | Application/MERN-specific error patterns | App logs first; service-outage path skips log scanning entirely |
| `generic_alarm` | Everything else | Balanced diagnostics across metrics, logs, traces |

---

## Environment Variables

All variables are read from `.env` at worker startup via `load_dotenv`. Defaults shown below match the values in `.env.example`.

### Polling

| Variable | Default | alarm_worker.py | Description |
|---|---|---|---|
| `ALARM_SQS_QUEUE_URL` | *(required)* | line 358 | URL of the SQS queue to poll. Written automatically by `instance_alarm_setup.py` on first boot. The worker refuses to start if this is empty. |
| `ALARM_WORKER_MAX_MESSAGES` | `1` | line 361 | Maximum number of SQS messages fetched per `receive_message` call. Keep at `1` so the agent handles one alarm at a time and visibility timeouts don't stack. |
| `ALARM_WORKER_WAIT_TIME_SECONDS` | `20` | line 362 | SQS long-poll wait time in seconds (0–20). Higher values reduce API calls; `20` is the SQS maximum. |
| `ALARM_WORKER_VISIBILITY_TIMEOUT` | `300` | line 363 | How long (seconds) a received message stays hidden from other consumers before reappearing. Should be longer than the expected agent run time. |
| `ALARM_WORKER_LOOP_SLEEP_SECONDS` | `2` | line 364 | Pause between loop iterations after a poll returns no messages. |

### Message filtering and acknowledgment

| Variable | Default | alarm_worker.py | Description |
|---|---|---|---|
| `ALARM_WORKER_PROCESS_ONLY_ALARM` | `true` | lines 365, 433 | When `true`, messages whose `new_state` is not `ALARM` (e.g. `OK`, `INSUFFICIENT_DATA`) are silently acknowledged and skipped. Set to `false` to have the agent also respond to recovery notifications. |
| `ALARM_WORKER_REQUIRE_SUCCESS_FOR_ACK` | `true` | lines 366, 537 | When `true`, the SQS message is deleted only if `run_agent_sync` returns `success=true`. On failure the message becomes visible again after the visibility timeout, allowing a retry. Set to `false` to always acknowledge regardless of outcome. |
| `ALARM_WORKER_AGENT_ATTEMPTS` | `1` | lines 375, 473 | Number of times the agent is invoked per message before the loop decides whether to acknowledge. Each failed attempt is logged. Increase to `2`–`3` for transient LLM-timeout resilience. |

### Mitigation behaviour

| Variable | Default | alarm_worker.py | Description |
|---|---|---|---|
| `ALARM_WORKER_AUTO_MITIGATE` | `true`* | lines 367, 274 | When `true`, the goal instructs the agent to `execute-safe-mitigations`; when `false`, the agent operates in `recommend-only` mode and reports without taking action. |
| `ALARM_WORKER_ALLOW_REBOOT_ON_STATUS_CHECK_FAILURE` | `false` | lines 368–371, 337–343 | When `true`, the goal explicitly permits the agent to call `aws_reboot_ec2_instance` for unresolved `StatusCheckFailed_Instance` alarms. When `false`, the agent recommends a reboot to the operator instead. |
| `ALARM_WORKER_ALLOW_DISK_CLEANUP_APPLY` | `false` | lines 372, 344 | When `true`, the agent may run `aws_ssm_safe_disk_cleanup` in apply mode (not just dry-run) during disk-pressure alarms. The worker always runs a dry-run first regardless of this flag. |
| `ALARM_WORKER_TARGET_OS` | `amazon-linux-2023` | lines 373, 334 | OS hint passed into the goal. Used by the agent to select SSM-safe commands for the right distribution (e.g. `dnf` vs `apt`). |
| `ALARM_WORKER_RESTART_SERVICES` | *(empty)* | lines 374, 273 | Comma-separated list of systemd service names the agent is allowed to restart autonomously (e.g. `real-api.service,nginx.service`). An empty list means no service restarts are permitted. |

> *`ALARM_WORKER_AUTO_MITIGATE` defaults to `true` in the worker code (`_parse_bool_env(..., True)`), but `.env.example` ships it as `false` for safety. The value in your `.env` file always wins.

### Logging

| Variable | Default | alarm_worker.py | Description |
|---|---|---|---|
| `ALARM_WORKER_LOG_LEVEL` | `INFO` | line 24 | Python logging level for the worker. Set to `DEBUG` to see every poll event, including empty-queue cycles. |

---

## Log Events

The worker emits structured JSON log lines (one per event) to stdout, captured by systemd. Key events:

| Event | When emitted |
|---|---|
| `worker_started` | Once at startup — dumps the effective configuration |
| `alarm_received` | Every message pulled from SQS |
| `alarm_skipped` | Non-ALARM message silently acknowledged |
| `trigger_agent` | Before each agent invocation attempt |
| `agent_attempt_incomplete` | Agent returned `success=false` |
| `agent_attempt_failed` | Agent raised an exception |
| `notification_processed` | Message successfully acknowledged — includes `notification_delay_seconds`, `ttm_seconds`, `e2e_seconds`, `trajectory_length` |
| `message_not_acknowledged` | Message left in queue (agent failed and `REQUIRE_SUCCESS_FOR_ACK=true`, or ACK call itself failed) |
| `sqs_poll_failed` | SQS receive call threw an exception; worker backs off and retries |

**Tail live logs on the instance:**
```bash
sudo tail -f /var/log/ai-agent/agent.log
# or via journald
sudo journalctl -u ai-agent.service -f
```

**Example `notification_processed` entry:**
```json
{
  "timestamp": "2025-01-10T12:05:42Z",
  "level": "INFO",
  "event": "notification_processed",
  "alarm_name": "CPUUtilizationAlarm-i-0abc1234567890def",
  "state": "ALARM",
  "action": "acknowledged",
  "agent_success": true,
  "notification_delay_seconds": 12.4,
  "ttm_seconds": 38.1,
  "e2e_seconds": 50.5,
  "trajectory_length": 7
}
```

`e2e_seconds` is the end-to-end time from when the CloudWatch alarm fired to when the agent finished. `ttm_seconds` is the time-to-mitigation if the agent executed a mutating action.

---

## Running the Worker Manually

```bash
# activate the venv
source venv/bin/activate          # Linux/Mac
venv\Scripts\activate             # Windows

# ensure ALARM_SQS_QUEUE_URL is set in .env
python alarm_worker.py
```

The worker runs until interrupted (`Ctrl+C`). On EC2 instances it is managed by `ai-agent.service` — use `sudo systemctl restart ai-agent.service` to apply `.env` changes without a reboot.
