# CloudAgent — Autonomous AI Cloud Infrastructure Manager

An AI agent that autonomously manages cloud infrastructure using natural-language goals and the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). The current implementation targets **AWS**, with the architecture designed to extend to Azure and GCP.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [Part 1 — AWS Infrastructure Bootstrap (Local Machine)](#part-1--aws-infrastructure-bootstrap-local-machine)
4. [Part 2 — EC2 Instance Setup (Per Instance)](#part-2--ec2-instance-setup-per-instance)
5. [Environment Variables Reference](#environment-variables-reference)
6. [Verifying the Deployment](#verifying-the-deployment)
7. [Running the Agent](#running-the-agent)
8. [Part 3 — Auto Scaling Groups (Optional)](#part-3--auto-scaling-groups-optional)

---

## Architecture Overview

```
main.py  ──►  agent/core.py (async planning loop)
                  │
                  ├─ agent/mcp_client.py     spawns MCP servers as subprocesses
                  │      └─ mcp_servers/aws_server.py  (50+ boto3-backed tools)
                  │
                  ├─ agent/policy_engine.py  validates every tool call before execution
                  ├─ agent/state_manager.py  persists state/ directory
                  └─ agent/observability_helper.py  sub-agent for telemetry analysis

alarm_worker.py   ──►    long-running SQS polling loop → auto-mitigates CloudWatch alarms
cost_optimization_worker.py ──►   weekly vertical rightsizing analysis — optimizes instance types, not fleet size (runs as ECS Fargate task)
```

### IAM Identity Split

| Component                       | Runs on        | AWS Identity          | Why                                                                   |
| ------------------------------- | -------------- | --------------------- | --------------------------------------------------------------------- |
| `alarm_worker.py`               | EC2 (ec2-user) | `ai-agent` IAM user   | Needs SSM, EC2 Describe, CloudWatch, SQS                              |
| `instance_alarm_setup.py`       | EC2            | EC2 instance role     | Needs SQS/SNS/CloudWatch create                                       |
| CloudWatch agent / X-Ray daemon | EC2            | EC2 instance role     | Covered by `CloudWatchAgentServerPolicy` / `AWSXRayDaemonWriteAccess` |
| `cost_optimization_worker.py`   | ECS Fargate    | ECS task role         | Reads metrics, optionally resizes instances                           |
| Cleanup Lambda function         | AWS Lambda     | Lambda execution role | Deletes per-instance SQS/SNS/alarms on termination                    |

---

## Prerequisites

### Local machine

| Tool           | Minimum version | Notes                                            |
| -------------- | --------------- | ------------------------------------------------ |
| Python         | 3.11            | Required for scripts and the agent itself        |
| AWS CLI v2     | latest          | `aws --version` to check                         |
| Docker Desktop | latest          | Required to build and push the cost worker image |
| Git            | any             |                                                  |

### AWS account

- Administrator or root-level IAM access to run the bootstrap scripts
- AWS CLI profile configured for that admin account (SSO or long-term credentials)

---

## Part 1 — AWS Infrastructure Bootstrap (Local Machine)

Run these steps **once** from your local workstation with admin/root AWS credentials.  
All scripts are in `ai-agent-cloud/scripts/`.

### 1.1 — Clone the repository

```bash
git clone https://github.com/MuhamedMuharam/CloudAgent.git
cd CloudAgent/ai-agent-cloud
```

### 1.2 — Set up a local Python environment for the scripts

```bash
python -m venv venv

# Windows — Command Prompt
venv\Scripts\activate.bat

# Windows — PowerShell
venv\Scripts\Activate.ps1

# Install the only dependency the admin scripts need
pip install boto3

# Required if your admin profile uses AWS SSO (Identity Center)
pip install "botocore[crt]"
```

### 1.3 — Configure AWS CLI and set your admin profile

#### Log in (SSO example)

```bash
aws sso login --profile root-user
```

#### Set the profile for the current terminal session

**Command Prompt (Windows):**

```cmd
set AWS_PROFILE=root-user
```

**PowerShell (Windows):**

```powershell
$env:AWS_PROFILE = "root-user"
```

**Verify you are authenticated as the admin:**

```bash
aws sts get-caller-identity
```

> If your SSO session expires later, re-run `aws sso login --profile root-user` and set `AWS_PROFILE` again.

### 1.4 — Create IAM user and EC2 instance role

```bash
python scripts/setup_iam.py
```

**What it creates:**

- IAM user `ai-agent` with policies for EC2, CloudWatch, SSM, ECR, and Compute Optimizer
- IAM access key for `ai-agent` — **copy the key ID and secret now, they are shown only once**
- EC2 role `EC2AgentRole` with SSM, X-Ray, and CloudWatch Agent policies
- Instance profile `EC2AgentRole` linked to the role

**Save the output — you will need it in the next steps:**

```
AWS_ACCESS_KEY_ID     = AKIA...
AWS_SECRET_ACCESS_KEY = ...
IAM user ARN          = arn:aws:iam::ACCOUNT_ID:user/ai-agent
```

### 1.5 — Prepare the `.env` file

```bash
copy .env.example .env   # Windows CMD
# or
cp .env.example .env     # bash
```

Open `.env` and fill in at minimum:

```ini
# LLM
OPENAI_API_KEY=sk-proj-...

# AWS — use the ai-agent IAM user key created in step 1.4
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1

# IAM user ARN — used by instance_alarm_setup.py to grant the alarm_worker SQS access
AGENT_IAM_USER_ARN=arn:aws:iam::ACCOUNT_ID:user/ai-agent
```

All other variables have safe defaults. See [Environment Variables Reference](#environment-variables-reference) for details.

### 1.6 — Configure the cost optimization worker environment

Before deploying the ECS infrastructure, open `config/cost_optimization/cost-optimization.worker.env` and adjust the settings for your fleet. The file ships with safe defaults, but the variables below are the most important to review:

| Variable                                       | Default          | Description                                                                 |
| ---------------------------------------------- | ---------------- | --------------------------------------------------------------------------- |
| `AWS_REGION`                                   | `us-east-1`      | Must match the region where your EC2 instances run                          |
| `COST_OPTIMIZATION_MODE`                       | `recommend_only` | Set to `take_action` to allow the worker to actually resize instances       |
| `COST_OPTIMIZATION_CPU_IDLE_THRESHOLD_PERCENT` | `15.0`           | Instances averaging below this CPU % are flagged as over-provisioned        |
| `COST_OPTIMIZATION_CPU_HOT_THRESHOLD_PERCENT`  | `70.0`           | Instances averaging above this CPU % are flagged as under-provisioned       |
| `COST_OPTIMIZATION_CPU_PEAK_CAP_PERCENT`       | `50.0`           | Never downsize if the peak CPU ever exceeded this % (bursty workload guard) |
| `COST_OPTIMIZATION_MIN_MONTHLY_SAVINGS_USD`    | `5.0`            | Minimum projected monthly savings required before a downsize is proposed    |
| `COST_OPTIMIZATION_MAX_ACTIONS_PER_RUN`        | `5`              | Hard cap on the number of resize actions per weekly run                     |
| `COST_OPTIMIZATION_ALLOWED_INSTANCE_IDS`       | _(blank = all)_  | Comma-separated list of instance IDs to limit the scope of analysis         |
| `COST_OPTIMIZATION_CREATE_BACKUP`              | `true`           | Creates an AMI snapshot before resizing when `take_action` mode is active   |

> **Tip:** Keep `COST_OPTIMIZATION_MODE=recommend_only` for the first few weeks. Review the logs, then switch to `take_action` once you are confident the thresholds suit your workload.

### 1.7 — Deploy the ECS Fargate cost optimization worker infrastructure

This script provisions everything the cost worker needs: IAM roles, S3 bucket, ECR repository, ECS cluster, CloudWatch log group, task definition, and an EventBridge weekly schedule.

```bash
python scripts/deploy_cost_service.py
```

At the end it prints the exact values to use in the next step — copy them.

### 1.8 — Build and push the worker Docker image

Open `scripts/update_cost_worker.py` and verify the variables at the top match what `deploy_cost_service.py` printed:

```python
ACCOUNT_ID  = "YOUR_ACCOUNT_ID"
REGION      = "us-east-1"
ECR_REPO    = "cost-opt-worker"   # must match deploy_cost_service.py
TASK_FAMILY = "cost-opt-worker"   # must match deploy_cost_service.py
IMAGE_TAG   = "v1"
LOCAL_IMAGE = "cost-opt-worker:local"
```

Make sure Docker Desktop is running, then:

```bash
python scripts/update_cost_worker.py
```

This builds the Docker image, pushes it to ECR, and registers a new ECS task definition revision.

> **Reuse for future updates:** any time you modify `cost_optimization_worker.py` or its dependencies, re-run `update_cost_worker.py` (incrementing `IMAGE_TAG`) to build and push a new image. ECS always resolves the task definition family to its latest active revision, so the next scheduled or manual run will automatically use the new image — no changes to the EventBridge schedule are needed.

### 1.9 — Deploy the instance cleanup Lambda

This creates a Lambda function that automatically deletes the per-instance SQS queue, SNS topic, and CloudWatch alarms when any EC2 instance is terminated.

```bash
python scripts/deploy_cleanup_lambda.py
```

---

## Part 2 — EC2 Instance Setup (Per Instance)

Repeat these steps on **every EC2 instance** that should run the alarm worker.

### 2.1 — Launch the instance

From the AWS console, launch an **Amazon Linux 2023** instance and:

- Attach the IAM instance profile: **EC2AgentRole** (created in step 1.4)  
  EC2 console → Instance → Actions → Security → Modify IAM role
- Open port 22 (SSH) in the security group for your IP

### 2.2 — SSH into the instance

```bash
ssh -i your-key.pem ec2-user@<PUBLIC_IP>
```

### 2.3 — Update the system and install Git

```bash
sudo dnf update -y
sudo dnf install -y git
```

### 2.4 — Clone the repository

```bash
git clone https://github.com/MuhamedMuharam/CloudAgent.git
cd CloudAgent/ai-agent-cloud
```

### 2.5 — Install Python 3.11 and set up the virtual environment

Amazon Linux 2023 ships with Python 3.9 by default; the project requires 3.11+.

```bash
# Install Python 3.11
sudo dnf install -y python3.11

# Verify
python3.11 --version

# Create virtual environment with Python 3.11
python3.11 -m venv venv

# Upgrade pip and install dependencies
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

### 2.6 — Add the `.env` file

The application reads credentials and configuration from a `.env` file at the project root.  
Create it on the instance — the easiest way is to copy the contents from your local `.env`:

```bash
nano .env
# paste your local .env content, save with Ctrl+O then Ctrl+X
```

**Minimum required values on EC2:**

```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-proj-...

# ai-agent IAM user credentials (from step 1.4)
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1

# IAM user ARN — lets instance_alarm_setup.py grant SQS access to the alarm worker
AGENT_IAM_USER_ARN=arn:aws:iam::ACCOUNT_ID:user/ai-agent
```

> `SQS_QUEUE_URL` / `ALARM_SQS_QUEUE_URL` will be automatically written into `.env` by `instance_alarm_setup.py` on the first boot — you do not need to set it manually.

### 2.7 — Make the setup scripts executable

```bash
sudo chmod +x scripts/setup_alarm_worker.sh \
              scripts/setup_cloudwatch_agent.sh \
              scripts/setup_xray.sh
```

### 2.8 — Install the X-Ray daemon

```bash
sudo bash scripts/setup_xray.sh
```

### 2.9 — Install and configure the CloudWatch agent

```bash
sudo bash scripts/setup_cloudwatch_agent.sh
```

### 2.10 — Install the alarm worker service

```bash
sudo bash scripts/setup_alarm_worker.sh
```

This script:

1. Creates a Python virtual environment if one does not exist
2. Prompts for the `ai-agent` IAM user credentials and writes them to `/home/ec2-user/.aws/credentials`
3. Writes two systemd services:
   - `ai-agent-setup.service` — oneshot, runs `scripts/instance_alarm_setup.py` on every boot to create the per-instance SQS queue, SNS topic, and CloudWatch alarms, then patches `.env` with the queue URL so that the alarm_worker polls from the right SQS
   - `ai-agent.service` — long-running `alarm_worker.py` SQS polling loop
4. Enables and starts both services

Once the script completes, `ai-agent.service` should be `active (running)` and `ai-agent-setup.service` `inactive (dead)` (oneshot, exits after success). See [Verifying the Deployment](#verifying-the-deployment) for expected output and console verification steps.

### 2.11 — Add custom CloudWatch alarms (optional)

`instance_alarm_setup.py` creates a dedicated SNS topic named `ai-agent-alarms-<instance-id-hex>` and wires it to the SQS queue that `alarm_worker.py` polls. The default alarms cover CPU, disk, memory, instance reachability, and system failures. If your workload needs additional signals you can attach any CloudWatch alarm(Through AWS console or CLI) to the same topic.

## Environment Variables Reference

Full reference is in `.env.example`. The most important variables for a fresh deployment:

| Variable                 | Where set                                 | Description                                                 |
| ------------------------ | ----------------------------------------- | ----------------------------------------------------------- |
| `OPENAI_API_KEY`         | `.env`                                    | OpenAI API key for the agent's GPT model                    |
| `AWS_ACCESS_KEY_ID`      | `.env`                                    | ai-agent IAM user key (from `setup_iam.py`)                 |
| `AWS_SECRET_ACCESS_KEY`  | `.env`                                    | ai-agent IAM user secret                                    |
| `AWS_DEFAULT_REGION`     | `.env`                                    | AWS region (default: `us-east-1`)                           |
| `AGENT_IAM_USER_ARN`     | `.env`                                    | ARN of the ai-agent IAM user — grants it SQS access         |
| `ALARM_SQS_QUEUE_URL`    | auto-patched by `instance_alarm_setup.py` | SQS queue URL for the alarm worker                          |
| `COST_OPTIMIZATION_MODE` | `.env`                                    | `recommend_only` (safe default) or `take_action`            |
| `HITL_ENABLED`           | `.env`                                    | `true` to require human approval before destructive actions |

---

## Verifying the Deployment

### On the EC2 instance

```bash
# Alarm worker is polling SQS
sudo systemctl status ai-agent.service

# Per-instance setup completed (SQS/SNS/alarms created)
sudo systemctl status ai-agent-setup.service

sudo tail -f /var/log/ai-agent/agent.log
```

Expected results:
- `ai-agent.service` shows `Active: active (running)`.
- `ai-agent-setup.service` shows `Active: inactive (dead)` with `status=0/SUCCESS`.
- The log tail prints new entries as the worker runs, without repeated errors.

If `ai-agent-setup.service` exited with a non-zero status, check `sudo journalctl -u ai-agent-setup.service` for the error.

### Via the AWS Console (if you have console access)

You can also verify the deployment visually without SSH:

| What to check                      | Where to look                                                                                                                                                                                                                                                                                               |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Alarm worker is running            | **EC2 → Instances** — instance state should be `running`; check **Systems Manager → Session Manager** to open a shell without SSH                                                                                                                                                                           |
| Per-instance SQS queue was created | **SQS** → search for `ai-agent-alarms-<instance-id-hex>`                                                                                                                                                                                                                                                    |
| Per-instance SNS topic was created | **SNS → Topics** → search for `ai-agent-alarms-<instance-id-hex>`                                                                                                                                                                                                                                           |
| CloudWatch alarms are active       | **CloudWatch → Alarms** → filter by `CPUUtilizationAlarm-<instance-id>`, `DiskPressureAlarm-<instance-id>`, etc.                                                                                                                                                                                            |
| Log groups created                 | **CloudWatch → Log management → Log groups** — a full deployment creates 4 groups: `/ai-agent/agent`, `/ai-agent/system`, `/aws/lambda/ai-agent-instance-alarm-cleanup`, `/ecs/cost-optimization-worker`. All four present means every component (alarm worker, Lambda, ECS task) has logged at least once. |
| Cleanup Lambda is deployed         | **Lambda** → search for `ec2-alarm-cleanup`                                                                                                                                                                                                                                                                 |
| Cost worker schedule is active     | **EventBridge → Schedules** → `cost-opt-worker-weekly`, state should be `ENABLED`                                                                                                                                                                                                                           |
| Cost worker ECS cluster exists     | **ECS → Clusters** → `cost-opt-worker-cluster`                                                                                                                                                                                                                                                              |

### Trigger a manual cost worker run (optional)

**Via the AWS Console (recommended):**

- **Option A — ECS:** **ECS → Clusters → cost-opt-worker-cluster → Tasks → Run new task**. Set launch type to `FARGATE`, select the `cost-opt-worker` task definition (latest revision), choose a subnet and security group, and click **Create**.
- **Option B — EventBridge:** **EventBridge → Schedules → cost-opt-worker-weekly → Edit**. Temporarily change the schedule expression to a time one or two minutes in the future , save, wait for the task to run, then restore the original `rate(7 days)` expression.

**or Via the CLI:**

```bash
aws ecs run-task \
  --cluster cost-opt-worker-cluster \
  --task-definition cost-opt-worker \
  --launch-type FARGATE \
  --region us-east-1 \
  --network-configuration 'awsvpcConfiguration={subnets=[<subnet-id>],securityGroups=[<sg-id>],assignPublicIp=ENABLED}'
```

After triggering, watch the run in **CloudWatch → Log management → Log groups → /ecs/cost-optimization-worker**.

---

## Running the Agent

Once the infrastructure is in place, interact with the agent directly from your local machine:

```bash
# activate local venv
venv\Scripts\activate   # Windows

# edit the `goal` variable in main.py, then run
python main.py

# inspect current tracked state
python view_state.py

# sync live AWS state into state/state.json
python sync_aws_state.py

# tail the audit log
python view_state.py --log
```

### Example goals

```python
# in main.py — edit the goal variable
goal = "List all running EC2 instances and their CPU utilization"
goal = "Ensure I have 2 running t3.micro instances tagged ManagedBy:AIAgent"
goal = "Analyze CloudWatch metrics for instance i-0abc123 and recommend any rightsizing changes"
```

### Switching the LLM provider

The agent has a built-in LLM abstraction layer — you can swap the underlying model without touching any agent code. The `LLM_PROVIDER` variable in `.env` selects the backend, and `MAIN_CONTROLLER_MODEL` / `OBSERVABILITY_HELPER_MODEL` name the models for the main planning loop and the observability sub-agent respectively.

**Default (OpenAI):**

```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-proj-...
MAIN_CONTROLLER_MODEL=gpt-5.4
OBSERVABILITY_HELPER_MODEL=gpt-4.1-mini
```

**Switch to Anthropic Claude:**

```ini
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
MAIN_CONTROLLER_MODEL=claude-opus-4-7
OBSERVABILITY_HELPER_MODEL=claude-haiku-4-5
```

No other changes are needed — restart the agent or the alarm worker after editing `.env` and the new provider takes effect immediately.

---

## Part 3 — Auto Scaling Groups (Optional)

If you want the framework to manage a fleet of identical instances — with automatic scale-out and scale-in — you can back the alarm worker with an ASG. Once set up, new instances join the monitoring system automatically on first boot, terminated instances are cleaned up by the Lambda, and the cost optimization worker keeps the launch template in sync whenever it resizes an instance.

### 3.1 — Create an AMI from a fully configured instance

Complete Part 2 on one instance and confirm both systemd services are healthy. Then create an AMI from it.

**Via the AWS Console (recommended):**

1. **EC2 → Instances** → select your configured instance
2. **Actions → Image and templates → Create image**
3. Set **Image name** (e.g. `ai-agent-base-20250110`) and an optional **Image description**
4. Check **No reboot** to avoid downtime (see note below)
5. Click **Create image** — copy the `ami-xxxxxxxxxxxxxxxxx` ID from the confirmation banner
6. Track progress in **EC2 → AMIs** until status shows `available`

**or Via the CLI:**

```bash
aws ec2 create-image \
  --instance-id <INSTANCE_ID> \
  --name "ai-agent-base-$(date +%Y%m%d)" \
  --description "Base image with alarm worker, CloudWatch agent, X-Ray daemon" \
  --no-reboot \
  --region us-east-1
# Returns: { "ImageId": "ami-xxxxxxxxxxxxxxxxx" }
```

> **No reboot** avoids downtime but may capture in-flight writes. For a fully consistent snapshot, leave the option unchecked — the instance will reboot briefly.

### 3.2 — Create a Launch Template from the AMI

**Via the AWS Console (recommended):**

1. **EC2 → Launch Templates → Create launch template**
2. **Launch template name**: `ai-agent-worker` | **Version description**: `v1 — base image`
3. **Application and OS Images → My AMIs** → select the AMI from step 3.1
4. **Instance type**: `t3.medium` (or your preferred size)
5. **Key pair**: select your existing key pair
6. **Network settings → Security groups**: select your security group
7. **Advanced details → IAM instance profile**: select **EC2AgentRole** ← required; without it `instance_alarm_setup.py` cannot create SQS/SNS/alarms on first boot
8. **Resource tags**: add `ManagedBy = AIAgent`, resource type `Instances`
9. Click **Create launch template**

**or Via the CLI:**

```bash
aws ec2 create-launch-template \
  --launch-template-name ai-agent-worker \
  --version-description "v1 — base image" \
  --launch-template-data "{
    \"ImageId\": \"<AMI_ID>\",
    \"InstanceType\": \"t3.medium\",
    \"IamInstanceProfile\": {\"Name\": \"EC2AgentRole\"},
    \"KeyName\": \"<YOUR_KEY_PAIR>\",
    \"SecurityGroupIds\": [\"<YOUR_SG_ID>\"],
    \"TagSpecifications\": [{
      \"ResourceType\": \"instance\",
      \"Tags\": [{\"Key\": \"ManagedBy\", \"Value\": \"AIAgent\"}]
    }]
  }" \
  --region us-east-1
```

### 3.3 — Create the Auto Scaling Group

**Via the AWS Console (recommended):**

1. **EC2 → Auto Scaling Groups → Create Auto Scaling group**
2. **Name**: `ai-agent-fleet` → select **Launch template**: `ai-agent-worker` (default version) → Next
3. Select your **VPC** and at least two **subnets** (for multi-AZ resilience) → Next
4. Skip load balancer configuration unless required (you can add a load balancer — for example an Application Load Balancer (ALB) — to route traffic if the deployed app needs it) → Next
5. **Desired capacity**: `2` | **Min**: `1` | **Max**: `5` → Next
6. Add tag `ManagedBy = AIAgent`, check **Tag new instances** → Next → **Create Auto Scaling group**

**or Via the CLI:**

```bash
aws autoscaling create-auto-scaling-group \
  --auto-scaling-group-name ai-agent-fleet \
  --launch-template "LaunchTemplateName=ai-agent-worker,Version=\$Default" \
  --min-size 1 \
  --max-size 5 \
  --desired-capacity 2 \
  --vpc-zone-identifier "<SUBNET_ID_1>,<SUBNET_ID_2>" \
  --tags "Key=ManagedBy,Value=AIAgent,PropagateAtLaunch=true" \
  --region us-east-1
```

Once the ASG launches its first instances, verify each one registered its own SQS queue and alarms in **SQS** and **CloudWatch → Alarms** before proceeding.

### 3.4 — How the framework handles each lifecycle event

| Event                               | What happens automatically                                                                                                                                                                                                                                                                                                                                             |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Instance launches**               | `ai-agent-setup.service` runs on first boot (baked into the AMI), creates the per-instance SQS queue, SNS topic, and CloudWatch alarms, and patches `.env` with the queue URL. `ai-agent.service` starts and begins polling.                                                                                                                                           |
| **Alarm fires**                     | The alarm publishes to the instance's SNS topic → SQS queue → `alarm_worker.py` picks it up and invokes the agent to diagnose and mitigate.                                                                                                                                                                                                                            |
| **Cost worker resizes an instance** | After a successful resize the worker calls `aws_sync_asg_launch_template_after_resize`, which creates a new launch template version with the updated instance type and sets it as the ASG default (note: this behavior applies when the instance is a member of an Auto Scaling Group (ASG)). Future scale-out events automatically use the right-sized instance type. |
| **Instance terminates**             | The cleanup Lambda fires on the EC2 state-change event and deletes the per-instance SQS queue, SNS topic, and CloudWatch alarms.                                                                                                                                                                                                                                       |

### 3.5 — Add or tune scaling policies (optional)

**Via the AWS Console (recommended):** **EC2 → Auto Scaling Groups → ai-agent-fleet → Automatic scaling → Create dynamic scaling policy**. Choose **Target tracking**, metric `Average CPU utilization`, target value `60%`, and click **Create**.

**or Via the CLI:**

```bash
aws autoscaling put-scaling-policy \
  --auto-scaling-group-name ai-agent-fleet \
  --policy-name cpu-target-tracking \
  --policy-type TargetTrackingScaling \
  --target-tracking-configuration "{
    \"PredefinedMetricSpecification\": {\"PredefinedMetricType\": \"ASGAverageCPUUtilization\"},
    \"TargetValue\": 60.0
  }" \
  --region us-east-1
```

You can also ask the agent to configure or inspect scaling policies for you using natural language goals:

```python
goal = "List all Auto Scaling Groups and their current capacity"
goal = "Ensure the ai-agent-fleet ASG has a minimum of 2 instances"
goal = "Add a CPU target-tracking scaling policy to ai-agent-fleet with a 60% target"
```

---

## Further Reading

- `ai-agent-cloud/docs/CODE_WALKTHROUGH.md` — step-by-step explanation of the agent loop
- `ai-agent-cloud/docs/AlarmWorkerGuide.md` — alarm worker internals, default alarms, and all env variable reference
- `ai-agent-cloud/docs/CostOptimizationServiceGuide.md` — ECS Fargate + EventBridge deployment details
- `ai-agent-cloud/docs/HorizontalScalingGuide.md` — ASG and Launch Template integration
- `ai-agent-cloud/docs/STATE_FILES_GUIDE.md` — state file schema and audit log format
- `ai-agent-cloud/policies/aws_policies.yaml` — policy rules enforced before every cloud API call

---

## Future Implementations

- 🚧 **Azure VM management** — parallel to the AWS implementation; MCP server stub already wired in
- 🚧 **GCP Compute Engine** — complete multi-cloud coverage alongside AWS and Azure
