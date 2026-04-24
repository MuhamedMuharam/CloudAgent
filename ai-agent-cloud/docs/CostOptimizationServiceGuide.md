# Cost Optimization Worker on ECS Fargate

This guide deploys the EC2 rightsizing worker as an on-demand ECS Fargate task triggered weekly by EventBridge Scheduler.

Scope of this worker:

1. Analyze EC2 fleet rightsizing opportunities.
2. Generate recommendations in recommend_only mode.
3. Optionally apply qualified rightsizing changes in take_action mode.

Current scope intentionally excludes EBS optimization.

## 1) Environment Variables Used by the Worker

The worker reads the following runtime variables:

### AWS and Runtime

- AWS_REGION (default: us-east-1)
- AWS_ACCESS_KEY_ID (optional; avoid on ECS, prefer task role)
- AWS_SECRET_ACCESS_KEY (optional; avoid on ECS, prefer task role)
- AWS_PROFILE (optional; local use)
- COST_OPTIMIZATION_LOG_LEVEL (default: INFO)

### Mode and Frequency Logic

- COST_OPTIMIZATION_MODE (default: recommend_only)
- COST_OPTIMIZATION_INTERVAL_WEEKS (default: 1)

### Analysis and Thresholds

- COST_OPTIMIZATION_ANALYSIS_MINUTES (default: 10080)
- COST_OPTIMIZATION_ANALYSIS_PERIOD_SECONDS (default: auto)
- COST_OPTIMIZATION_ADAPTIVE_BASE_PERIOD_SECONDS (default: 900)
- COST_OPTIMIZATION_MAX_DATAPOINTS_PER_METRIC (default: 1200)
- COST_OPTIMIZATION_CPU_IDLE_THRESHOLD_PERCENT (default: 15.0)
- COST_OPTIMIZATION_CPU_HOT_THRESHOLD_PERCENT (default: 70.0)
- COST_OPTIMIZATION_NETWORK_IDLE_THRESHOLD_BYTES_PER_SECOND (default: 555.0)
- COST_OPTIMIZATION_INCLUDE_MEMORY_DISK_SIGNALS (default: true)
- COST_OPTIMIZATION_MEMORY_PRESSURE_THRESHOLD_PERCENT (default: 75.0)
- COST_OPTIMIZATION_DISK_PRESSURE_THRESHOLD_PERCENT (default: 80.0)
- COST_OPTIMIZATION_SWAP_PRESSURE_THRESHOLD_PERCENT (default: 50.0)

### Scope and Safety Gates

- COST_OPTIMIZATION_MAX_INSTANCES (default: 100)
- COST_OPTIMIZATION_ALLOWED_INSTANCE_IDS (default: empty CSV)
- COST_OPTIMIZATION_ALLOWED_FAMILIES (default: empty CSV)
- COST_OPTIMIZATION_MIN_MONTHLY_SAVINGS_USD (default: 10.0)
- COST_OPTIMIZATION_MIN_PRIMARY_DATAPOINTS (default: 24)
- COST_OPTIMIZATION_REQUIRE_DOWNSIZE_SIGNAL (default: true)
- COST_OPTIMIZATION_REQUIRE_NO_EXTENDED_FINDINGS (default: true)
- COST_OPTIMIZATION_MAX_ACTIONS_PER_RUN (default: 2)
- COST_OPTIMIZATION_MIN_CPU (default: empty)
- COST_OPTIMIZATION_MIN_RAM_GB (default: empty)

### Action and Continuity

- COST_OPTIMIZATION_CREATE_BACKUP (default: true)
- COST_OPTIMIZATION_BACKUP_NAME_PREFIX (default: ai-agent-cost-opt)
- COST_OPTIMIZATION_NO_REBOOT_BACKUP (default: true)
- COST_OPTIMIZATION_ENSURE_SERVICE_CONTINUITY (default: true)
- COST_OPTIMIZATION_STRICT_SERVICE_CONTINUITY (default: false)
- COST_OPTIMIZATION_SERVICE_RECOVERY_TIMEOUT_SECONDS (default: 420)

Notes:

- OPENAI_API_KEY is not required by cost_optimization_worker.py.
- Systemd-only variables such as COST_OPTIMIZATION_ON_CALENDAR, COST_OPTIMIZATION_RANDOMIZED_DELAY_SECONDS, COST_OPTIMIZATION_RUN_ON_INSTALL, and COST_OPTIMIZATION_SERVICE_USER are not used in ECS mode.

## 2) Prepare the Worker Env File

Use the provided template:

- config/cost_optimization/cost-optimization.worker.env

Edit values as needed, especially:

- COST_OPTIMIZATION_MODE
- AWS_REGION
- Any scope and safety threshold variables

Recommended starting mode:

```ini
COST_OPTIMIZATION_MODE=recommend_only
```

## 3) Upload the Env File to Private S3

```bash
aws s3 cp config/cost_optimization/cost-optimization.worker.env s3://YOUR_BUCKET/ecs/env/cost-optimization.env --region us-east-1 --sse AES256
```

Keep the bucket private and Block Public Access enabled.

## 4) Task Execution Role Permissions

If you use ECS environmentFiles from S3, the task execution role needs read access to the object:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadCostWorkerEnvFile",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": ["arn:aws:s3:::YOUR_BUCKET/ecs/env/cost-optimization.env"]
    }
  ]
}
```

## 5) Create ECS Task Definition (Fargate)

Use:

- Launch type: Fargate
- Network mode: awsvpc
- Container command: python cost_optimization_worker.py
- environmentFiles:

```json
[
  {
    "type": "s3",
    "value": "arn:aws:s3:::YOUR_BUCKET/ecs/env/cost-optimization.env"
  }
]
```

Set awslogs for container logs.

## 6) Configure Weekly EventBridge Scheduler

Create a schedule with cron or rate expression and target ECS RunTask.

Example schedule expression (weekly):

```text
cron(0 3 ? * SUN *)
```

Target details:

- ECS cluster ARN
- Task definition ARN
- Launch type: Fargate
- Subnets and security groups
- Optional dead-letter queue and retry policy

## 7) Verify Runtime

Check:

- EventBridge schedule invocation history
- ECS task status (Stopped/Running)
- CloudWatch container logs

A successful run writes cost optimization activity to:

- state/audit_log.jsonl
- state/cost_optimization_service_state.json

## 8) Move to take_action Safely

Start with at least one full cycle in recommendation mode:

```ini
COST_OPTIMIZATION_MODE=recommend_only
```

After reviewing recommendations, switch to:

```ini
COST_OPTIMIZATION_MODE=take_action
```

Upload the updated env file to the same S3 path and run one manual ECS task before relying on the schedule.

## 9) Change or Disable Schedule

- Change schedule: edit the EventBridge Scheduler expression.
- Disable temporarily: disable the schedule in EventBridge Scheduler.
- Re-enable later: enable the schedule again.

## 10) Update Worker Code (New Release)

When you change worker logic in code, use the deployment script `scripts/deploy_cost_worker.ps1` — it automates all manual steps (build, ECR login, tag, push, optional env upload, task definition re-registration, and optional Scheduler update) in a single command.

### Using the deploy script

**Code change only (no env update, no Scheduler update):**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy_cost_worker.ps1 `
  -AccountId YOUR_ACCOUNT_ID `
  -Region us-east-1 `
  -TaskDefinitionFamily cost-optimization-worker `
  -ImageTag v4
```

**Code change + upload updated env file to S3:**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy_cost_worker.ps1 `
  -AccountId YOUR_ACCOUNT_ID `
  -Region us-east-1 `
  -TaskDefinitionFamily cost-optimization-worker `
  -ImageTag v4 `
  -UploadEnv `
  -EnvS3Uri s3://YOUR_BUCKET/ecs/env/cost-optimization.worker.env
```

**Code change + env upload + update EventBridge Scheduler target to the new revision:**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy_cost_worker.ps1 `
  -AccountId YOUR_ACCOUNT_ID `
  -Region us-east-1 `
  -TaskDefinitionFamily cost-optimization-worker `
  -ImageTag v4 `
  -UploadEnv `
  -EnvS3Uri s3://YOUR_BUCKET/ecs/env/cost-optimization.worker.env `
  -UpdateScheduler `
  -ScheduleName YOUR_SCHEDULE_NAME
```

### Script parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `-AccountId` | yes | — | AWS account ID |
| `-Region` | no | `us-east-1` | AWS region |
| `-TaskDefinitionFamily` | yes | — | ECS task definition family name |
| `-ImageTag` | no | timestamp | ECR image tag (prefer date or git SHA) |
| `-UploadEnv` | no | off | Upload env file to S3 before registering task definition |
| `-EnvFilePath` | no | `config/cost_optimization/cost-optimization.worker.env` | Local env file path |
| `-EnvS3Uri` | required with `-UploadEnv` | — | S3 destination URI |
| `-UpdateScheduler` | no | off | Update EventBridge Scheduler target to the new task definition revision |
| `-ScheduleName` | required with `-UpdateScheduler` | — | EventBridge schedule name |
| `-ScheduleGroup` | no | `default` | EventBridge schedule group |

### After deployment

1. Run one manual ECS task for validation before relying on the schedule.
2. Check CloudWatch container logs to confirm the worker started and completed without errors.

Important notes:

- Prefer immutable tags (for example date or git SHA), not `latest`.
- If you only changed `.env` values and kept the same S3 object key, you can re-run the script with `-UploadEnv` and omit `-ImageTag` to reuse the existing image — but registering a new task definition revision is still required to pick up the updated env file reference if it changed.
- Validate first with `recommend_only` before enabling automatic actions in production.

## 11) Update .env Values Stored in S3

If you only change variable values, you usually do not need a new image.

### Case A: Same S3 object key (recommended)

1. Edit config/cost_optimization/cost-optimization.worker.env locally.
2. Upload to the same S3 key used by ECS environmentFiles.
3. Run one manual ECS task to verify logs and behavior.
4. Keep schedule enabled after validation.

```bash
aws s3 cp config/cost_optimization/cost-optimization.worker.env s3://YOUR_BUCKET/ecs/env/cost-optimization.env --region us-east-1 --sse AES256
```

In this case, task definition and schedule usually remain unchanged.

### Case B: New S3 object key/path

1. Upload env file to a new key.
2. Register a new ECS task definition revision with updated environmentFiles value.
3. Update EventBridge Scheduler target to use the new task definition revision.
4. Run one manual ECS task for validation.

### Safe rollout checklist for env changes

- Keep COST_OPTIMIZATION_MODE=recommend_only while validating.
- Confirm CloudWatch logs show expected values and no parsing errors.
- Verify state/audit_log.jsonl entries look correct after test run.
- Switch to take_action only after at least one clean recommendation cycle.
