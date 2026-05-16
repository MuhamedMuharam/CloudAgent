# Cost Optimization Worker — Service Guide

## Overview

The cost optimization worker is a Python script (`cost_optimization_worker.py`) that performs **vertical cost optimization** — it analyzes your EC2 fleet for rightsizing opportunities (moving over-provisioned instances to smaller, cheaper types or under-provisioned instances to larger ones) and optionally applies the changes automatically. This is distinct from horizontal scaling: the worker does not add or remove instances; it optimizes the instance type of each individual instance.

Rather than running as a long-lived process, it is designed as a **one-shot task**: it runs, does its analysis, writes results, and exits. The infrastructure around it is what makes it repeatable and hands-off.

### How it works end-to-end

```
Your code (cost_optimization_worker.py)
        │
        ▼
Docker image  ──►  pushed to ECR (Elastic Container Registry)
        │
        ▼
ECS Task Definition  (references the ECR image + env file from S3)
        │
        ▼
EventBridge Scheduler  ──►  triggers ECS to run the task every week
        │
        ▼
ECS Fargate  (spins up a container, runs the worker, then shuts down)
        │
        ▼
CloudWatch Logs  (/ecs/cost-optimization-worker)
```

**Why Fargate?** The worker only needs to run for a few minutes per week. Fargate charges by the second — no idle EC2 cost, no server to maintain.

**Why ECR?** ECS needs to pull the image from somewhere. ECR is the natural private registry for AWS workloads and integrates directly with the ECS task execution role.

**Why S3 for the env file?** The worker's configuration (`cost-optimization.worker.env`) is stored in a private S3 bucket and injected into the container at startup via ECS `environmentFiles`. This keeps secrets and settings out of the image itself, so you can change thresholds without rebuilding the Docker image.

### What the worker does

1. Pulls CloudWatch metrics for all in-scope EC2 instances (CPU, memory, disk, network) over the configured analysis window (default: last 7 days).
2. Evaluates each instance against idle/hot thresholds and safety gates (peak CPU cap, minimum savings, minimum data hours).
3. In `recommend_only` mode — logs recommendations to CloudWatch and `state/audit_log.jsonl`. No instances are touched.
4. In `take_action` mode — for each qualifying instance: optionally creates an AMI backup, stops the instance, resizes it to the recommended type, restarts it, and syncs the new instance type to the ASG launch template (if the instance belongs to an ASG).

---

## IAM Roles Required

Three IAM roles are involved. All three are created automatically by `scripts/deploy_cost_service.py` — you do not need to create them manually.

### 1. ECS Task Role (what the worker container can do)

This is the identity the running worker code uses to call AWS. It needs permission to:

- **Read EC2 metadata** — describe instances, check their state, get instance types
- **Read CloudWatch metrics** — fetch CPU, memory, disk, and network data
- **Read Compute Optimizer recommendations** — get AWS's own rightsizing suggestions
- **Write CloudWatch metrics and logs** — publish worker activity and findings
- **Stop, start, and modify EC2 instances** — only used in `take_action` mode to apply resizes
- **Create AMI snapshots** — only used in `take_action` mode when `COST_OPTIMIZATION_CREATE_BACKUP=true`
- **Read and write the S3 bucket** — store analysis results and read the env file
- **Update ASG launch templates** — sync the new instance type after a resize

### 2. ECS Task Execution Role (what ECS needs to start the task)

This is the identity ECS itself uses before the container starts. It needs permission to:

- **Pull the Docker image from ECR** — so ECS can fetch the image you pushed
- **Read the env file from S3** — so ECS can inject configuration into the container at startup
- **Write to CloudWatch Logs** — so container stdout/stderr appears in the log group

### 3. EventBridge Scheduler Role (what the scheduler needs to fire the task)

This is the identity EventBridge uses to trigger the ECS task. It needs permission to:

- **Call ECS RunTask** — to launch the Fargate task on schedule
- **Pass the Task Role and Execution Role to ECS** — AWS requires explicit permission to hand roles to ECS

---

## Environment Variables

The worker reads its configuration from `config/cost_optimization/cost-optimization.worker.env`. Edit this file before deploying. The most important variables:

### Mode and frequency

| Variable | Default | Description |
|---|---|---|
| `COST_OPTIMIZATION_MODE` | `recommend_only` | `recommend_only` = analysis only; `take_action` = apply resizes |
| `COST_OPTIMIZATION_INTERVAL_WEEKS` | `1` | How often the worker runs (set to `2` to skip every other week) |
| `COST_OPTIMIZATION_LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG` for troubleshooting) |

### Analysis window and thresholds

| Variable | Default | Description |
|---|---|---|
| `COST_OPTIMIZATION_ANALYSIS_MINUTES` | `10080` | Lookback window (10080 = 7 days) |
| `COST_OPTIMIZATION_CPU_IDLE_THRESHOLD_PERCENT` | `15.0` | Avg CPU below this → instance is over-provisioned |
| `COST_OPTIMIZATION_CPU_HOT_THRESHOLD_PERCENT` | `70.0` | Avg CPU above this → instance is under-provisioned |
| `COST_OPTIMIZATION_CPU_PEAK_CAP_PERCENT` | `50.0` | Never downsize if peak CPU ever exceeded this % |
| `COST_OPTIMIZATION_NETWORK_IDLE_THRESHOLD_BYTES_PER_SECOND` | `5000.0` | Network idle threshold in bytes/sec |
| `COST_OPTIMIZATION_INCLUDE_MEMORY_DISK_SIGNALS` | `true` | Also use memory, disk, and swap when evaluating |
| `COST_OPTIMIZATION_MEMORY_PRESSURE_THRESHOLD_PERCENT` | `75.0` | Memory above this → do not downsize |
| `COST_OPTIMIZATION_DISK_PRESSURE_THRESHOLD_PERCENT` | `80.0` | Disk above this → do not downsize |

### Scope and safety gates

| Variable | Default | Description |
|---|---|---|
| `COST_OPTIMIZATION_MAX_INSTANCES` | `100` | Maximum number of instances to analyze per run |
| `COST_OPTIMIZATION_ALLOWED_INSTANCE_IDS` | *(blank = all)* | Comma-separated allowlist to limit scope |
| `COST_OPTIMIZATION_ALLOWED_FAMILIES` | *(blank = all)* | Comma-separated instance families to consider (e.g. `t3,m5`) |
| `COST_OPTIMIZATION_MIN_MONTHLY_SAVINGS_USD` | `5.0` | Minimum projected monthly savings before proposing a downsize |
| `COST_OPTIMIZATION_MIN_DATA_HOURS` | `3.0` | Minimum hours of metric data required for a decision |
| `COST_OPTIMIZATION_MAX_ACTIONS_PER_RUN` | `2` | Hard cap on resize actions per weekly run |
| `COST_OPTIMIZATION_SKIP_CROSS_FAMILY_OPTIMIZATION` | `true` | Stay within the same instance family when downsizing |
| `COST_OPTIMIZATION_REQUIRE_DOWNSIZE_SIGNAL` | `true` | At least one metric must signal underutilization |
| `COST_OPTIMIZATION_REQUIRE_NO_EXTENDED_FINDINGS` | `true` | Do not downsize if memory/disk pressure is detected |

### Take-action execution settings

| Variable | Default | Description |
|---|---|---|
| `COST_OPTIMIZATION_CREATE_BACKUP` | `true` | Create an AMI snapshot before resizing |
| `COST_OPTIMIZATION_NO_REBOOT_BACKUP` | `true` | Create snapshot without rebooting the instance |
| `COST_OPTIMIZATION_ENSURE_SERVICE_CONTINUITY` | `true` | Verify services are healthy after resize before marking as done |
| `COST_OPTIMIZATION_SERVICE_RECOVERY_TIMEOUT_SECONDS` | `420` | How long to wait for services to recover after resize |

---

## Deployment (Automated)

All infrastructure — ECR repository, ECS cluster, task definition, CloudWatch log group, S3 bucket, IAM roles, and EventBridge schedule — is provisioned by a single script:

```bash
python scripts/deploy_cost_service.py
```

After it completes, build and push the Docker image:

```bash
# Edit ACCOUNT_ID and REGION at the top of the script first
python scripts/update_cost_worker.py
```

This builds the image locally, pushes it to ECR, and registers a new ECS task definition revision pointing to the new image.

---

## Updating the Worker

### Code changes (new release)

When you modify `cost_optimization_worker.py` or its dependencies, increment `IMAGE_TAG` in `update_cost_worker.py` and re-run it:

```bash
python scripts/update_cost_worker.py
```

ECS always resolves the task definition family to its latest active revision, so the next scheduled or manual run will automatically use the new image — no changes to the EventBridge schedule are needed.

### Configuration-only changes (no new image needed)

If you only change variable values in `cost-optimization.worker.env`, upload the updated file to the same S3 path and run one manual task to verify:

```bash
aws s3 cp config/cost_optimization/cost-optimization.worker.env \
  s3://YOUR_BUCKET/ecs/env/cost-optimization.env \
  --region us-east-1 --sse AES256
```

---

## Triggering a Manual Run

**Via the AWS Console (recommended):**

- **Option A — ECS:** **ECS → Clusters → cost-opt-worker-cluster → Tasks → Run new task**. Set launch type to `FARGATE`, select the `cost-opt-worker` task definition (latest revision), choose a subnet and security group, and click **Create**.
- **Option B — EventBridge:** **EventBridge → Schedules → cost-opt-worker-weekly → Edit**. Temporarily change the schedule expression to a time one or two minutes in the future (e.g. `at(2025-01-10T14:05:00)`), save, wait for the task to run, then restore the original `rate(7 days)` expression.

**Via the CLI:**

```bash
aws ecs run-task \
  --cluster cost-opt-worker-cluster \
  --task-definition cost-opt-worker \
  --launch-type FARGATE \
  --region us-east-1 \
  --network-configuration 'awsvpcConfiguration={subnets=[<subnet-id>],securityGroups=[<sg-id>],assignPublicIp=ENABLED}'
```

Watch the run in **CloudWatch → Log management → Log groups → /ecs/cost-optimization-worker**.

---

## Moving to `take_action` Mode Safely

1. Run at least one full cycle in `recommend_only` mode and review the recommendations in CloudWatch logs and `state/audit_log.jsonl`.
2. Confirm the thresholds match your workload expectations (adjust if needed and re-upload the env file).
3. Set `COST_OPTIMIZATION_MODE=take_action` in the env file and upload it to S3.
4. Trigger one manual run and watch the logs to confirm the resizes were applied correctly.
5. Re-enable the weekly schedule.

> Keep `COST_OPTIMIZATION_MAX_ACTIONS_PER_RUN` low (2–3) when first enabling `take_action` mode, and increase it only after validating several successful cycles.

---

## Disabling or Changing the Schedule

- **Pause:** **EventBridge → Schedules → cost-opt-worker-weekly → Disable**
- **Change frequency:** Edit the schedule expression (e.g. `rate(14 days)` for bi-weekly)
- **Re-enable:** Enable the schedule again from the same console page
