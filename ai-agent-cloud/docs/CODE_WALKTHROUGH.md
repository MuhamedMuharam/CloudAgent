# Code Walkthrough

This document is a code-aligned walkthrough of the project architecture, intended as a technical reference for understanding implementation decisions and system design.

## 1. Project Purpose

This repository implements an autonomous cloud operations agent that:

1. Accepts natural-language goals.
2. Uses an LLM to plan and decide which cloud operations to execute.
3. Calls AWS capabilities through MCP tools (Model Context Protocol).
4. Applies policy checks before execution.
5. Persists state and an audit trail for traceability.
6. Supports observability-driven operations with CloudWatch, X-Ray, and SSM.
7. Continuously monitors infrastructure via an event-driven alarm worker.
8. Performs weekly cost optimization analysis via a containerized ECS Fargate worker.

## 2. Repository Structure

High-level modules and responsibilities:

| Module | Responsibility |
|--------|---------------|
| `main.py` | Interactive entry point for goal-driven agent execution |
| `alarm_worker.py` | Long-running SQS polling loop; triggers agent triage on CloudWatch alarms |
| `cost_optimization_worker.py` | One-shot weekly **vertical** rightsizing analysis (instance type, not fleet size); runs as ECS Fargate task |
| `agent/` | Core orchestration: LLM loop, MCP client, policy engine, state manager, observability helper, LLM provider abstraction |
| `mcp_servers/aws_server.py` | FastMCP server exposing 71 boto3-backed tools over stdio |
| `cloud_providers/aws/` | Manager classes wrapping boto3 APIs (EC2, VPC, CloudWatch, SSM, X-Ray, ASG, mapping) |
| `scripts/` | Bootstrap and deployment scripts (IAM, ECS, Lambda, EC2 setup) |
| `config/` | CloudWatch agent config, cost worker environment file |
| `state/` | Persistent snapshot (`state.json`) and append-only action log (`audit_log.jsonl`) |
| `policies/aws_policies.yaml` | YAML policy constraints enforced before every tool execution |
| `docs/` | Operational and architecture documentation |

## 3. LLM Provider Abstraction (`agent/llm_provider.py`)

The agent supports multiple LLM backends through a provider abstraction layer. The active provider is selected at startup via the `LLM_PROVIDER` environment variable.

```
LLM_PROVIDER=openai    → OpenAIProvider  (needs OPENAI_API_KEY)
LLM_PROVIDER=anthropic → AnthropicProvider (needs ANTHROPIC_API_KEY)
```

Key design decisions:

1. All providers expose the same `chat()` interface, returning a normalized `LLMResponse` with `content`, `tool_calls`, and `_raw` (provider-native object).
2. Tool schemas are defined once in OpenAI format; `AnthropicProvider._openai_tools_to_anthropic()` converts them transparently at call time.
3. Message history is stored in OpenAI-format dicts; `_to_anthropic_messages()` converts per-call without modifying stored history.
4. Adding a new provider requires only implementing `LLMProvider` and registering it in `create_provider()`.

The main controller model and observability helper model are independently configurable (`MAIN_CONTROLLER_MODEL`, `OBSERVABILITY_HELPER_MODEL`).

## 4. Python Package Initialization (`__init__.py`)

### `cloud_providers/aws/__init__.py`

Exports current AWS managers:

1. `EC2Manager`
2. `VPCManager`
3. `SecurityGroupManager`
4. `CloudWatchManager`
5. `SSMManager`
6. `XRayManager`
7. `ASGManager`
8. `map_generic_to_instance_type`

### `agent/__init__.py`

Exports key agent entry symbols:

1. `run_agent`
2. `StateManager`

## 5. MCP Architecture

The implementation uses two complementary libraries:

1. `mcp` (official SDK) on the client side in `agent/mcp_client.py`.
2. `fastmcp` on the server side in `mcp_servers/aws_server.py`.

Communication pattern:

1. Agent process spawns the AWS MCP server as a subprocess.
2. Client and server communicate over stdio using JSON-RPC (MCP protocol).
3. Agent discovers tools at startup; the tool list is dynamic.
4. The LLM chooses tool calls; the client routes them to the owning MCP server.

The planner (`agent/core.py`) is fully decoupled from boto3 implementation details. MCP resources and prompts are no longer used — all context is injected via goal-aware system prompt instruction packs.

## 6. Execution Flow A: Interactive Goal (`python main.py`)

### Step-by-step sequence

1. `main.py` defines a goal and calls `run_agent_sync(goal)`.
2. `agent/core.py` initializes:
   - LLM provider (via `create_provider(LLM_PROVIDER, MAIN_CONTROLLER_MODEL)`)
   - `MCPClientManager`
   - `StateManager`
   - `PolicyEngine`
3. Core connects to MCP servers (spawns `aws_server.py` as a child process over stdio).
4. MCP client discovers the 71 available tools.
5. System prompt is built via `build_system_prompt(goal)`:
   - Base rules are always included.
   - Instruction packs are selected based on keywords detected in the goal (e.g. `asg_scaling` pack injected if goal mentions "auto scaling", "scale out").
   - Policy domain hints are appended; full YAML sections are injected only when a relevant tool is about to be called.
6. `delegate_observability_analysis` is registered as a synthetic tool alongside the MCP tools.
7. Agent loop starts:
   - LLM returns either tool calls or a final response.
   - Mutating tools are checked against `MUTATING_TOOLS` frozenset.
   - Before executing each tool: `PolicyEngine.validate_action(tool_name, args, relevant_domains)`.
   - Tool executes through the MCP server.
   - Tool result is appended to conversation and logged via `StateManager`.
8. If `delegate_observability_analysis` is called, `run_observability_helper(analysis_request)` is invoked — a separate LLM instance with only CloudWatch/X-Ray/SSM tools, preventing telemetry payloads from bloating the main context.
9. Loop exits on final assistant response or `max_iterations`.
10. Core closes MCP sessions/subprocesses and writes the final goal execution log.

### Goal-aware instruction packs

`INSTRUCTION_PACKS` in `agent/core.py` contains focused guidance for each operational domain. Packs are injected into the system prompt only when their keywords appear in the goal, keeping token usage low for simple goals.

| Pack key | Injected when goal contains |
|---|---|
| `security_groups` | "security group", "ingress", "cidr", "port" |
| `vpc_deletion` | "vpc", "delete", "remove" |
| `cloudwatch_alarms` | "alarm", "cloudwatch", "metrics", "logs" |
| `ssm_execution` | "ssm", "command", "service", "systemctl" |
| `xray_tracing` | "xray", "trace", "service graph" |
| `cost_optimization` | "cost", "rightsize", "resize", "savings" |
| `asg_scaling` | "asg", "auto scaling", "scale out", "launch template" |

## 7. Execution Flow B: Alarm Worker (`python alarm_worker.py`)

`alarm_worker.py` is a continuously running triage loop:

1. Polls the per-instance SQS queue for CloudWatch alarm notifications via `CloudWatchManager.poll_alarm_notifications`.
2. Optionally filters to ALARM-state only (controlled by `ALARM_WORKER_PROCESS_ONLY_ALARM`).
3. Classifies the alarm into a family: `cpu_pressure`, `disk_pressure`, `memory_pressure`, `ec2_system_status`, `ec2_instance_status`, `application_error`, `generic_alarm`.
4. Builds a targeted triage goal via `_build_goal()`, including alarm context, execution mode, OS hint, and allowed service restarts.
5. Delegates heavy telemetry analysis to the observability helper via a base seed in `analysis_request`.
6. Calls `run_agent_sync(goal)` for automated diagnosis and (if configured) mitigation.
7. Acknowledges/deletes the SQS message on successful processing; leaves it unacked on failure so it reappears after the visibility timeout.

See `docs/AlarmWorkerGuide.md` for the full environment variable reference and log event dictionary.

## 8. Execution Flow C: Cost Optimization Worker (`python cost_optimization_worker.py`)

`cost_optimization_worker.py` is a one-shot **vertical cost optimization** process triggered weekly by EventBridge Scheduler on ECS Fargate. It optimizes the instance type of each EC2 instance (rightsizing) rather than changing fleet size:

1. Reads configuration from `config/cost_optimization/cost-optimization.worker.env`.
2. Queries CloudWatch metrics for all in-scope EC2 instances (CPU, memory, disk, network) over the configured analysis window.
3. Evaluates each instance against idle/hot thresholds and safety gates (peak CPU cap, minimum savings, minimum data hours).
4. In `recommend_only` mode: logs recommendations to CloudWatch (`/ecs/cost-optimization-worker`) and S3.
5. In `take_action` mode: for each qualifying instance, optionally creates an AMI backup, stops the instance, resizes it, restarts it, and calls `aws_sync_asg_launch_template_after_resize` to propagate the new instance type to the ASG launch template.

See `docs/CostOptimizationServiceGuide.md` for deployment details.

## 9. AWS MCP Tool Surface (71 tools)

The AWS MCP server currently exposes 71 tools across 7 groups. There are no MCP resources or prompts — all context is delivered via the system prompt.

### EC2 Lifecycle and Status (9)

| Tool | Action |
|------|--------|
| `aws_list_ec2_instances` | List instances with optional tag filter |
| `aws_create_ec2_instance` | Launch a new instance |
| `aws_delete_ec2_instance` | Terminate an instance |
| `aws_get_ec2_instance_status` | Get instance state and health |
| `aws_start_ec2_instance` | Start a stopped instance |
| `aws_stop_ec2_instance` | Stop a running instance |
| `aws_reboot_ec2_instance` | Reboot an instance |
| `aws_get_ec2_instance_ssm_status` | Check SSM agent reachability |
| `aws_collect_ec2_health_snapshot` | Collect combined status/metrics/alarm snapshot |

### SSM Remote Execution and Service Control (9)

| Tool | Action |
|------|--------|
| `aws_ssm_run_command` | Execute a shell command on an instance |
| `aws_ssm_get_command_output` | Fetch output from a previously submitted command |
| `aws_ssm_collect_host_diagnostics` | Run a suite of host diagnostics (disk, memory, processes, logs) |
| `aws_ssm_safe_disk_cleanup` | Identify and optionally remove large/tmp files |
| `aws_ssm_start_service` | Start a systemd service |
| `aws_ssm_stop_service` | Stop a systemd service |
| `aws_ssm_restart_service` | Restart a systemd service |
| `aws_ssm_get_service_status` | Get systemd service status |
| `aws_ssm_list_running_services` | List all active systemd services |

### Cost Optimization and Rightsizing (6)

| Tool | Action |
|------|--------|
| `aws_analyze_ec2_cost_optimization` | Analyze a single instance for rightsizing |
| `aws_analyze_ec2_fleet_cost_optimization` | Analyze the entire fleet |
| `aws_get_compute_optimizer_recommendations` | Fetch AWS Compute Optimizer recommendations |
| `aws_resize_ec2_instance` | Validate or perform a type change (dry_run supported) |
| `aws_apply_ec2_rightsizing` | Execute a validated resize with backup and continuity checks |
| `aws_detect_idle_cost_leaks` | Identify stopped/idle resources wasting spend |

### ASG and Launch Template (12)

| Tool | Action |
|------|--------|
| `aws_list_launch_templates` | List launch templates |
| `aws_describe_launch_template` | Get LT details and selected versions |
| `aws_create_launch_template_version` | Create a new LT version overriding instance type |
| `aws_set_launch_template_default_version` | Change the default version |
| `aws_list_asgs` | List Auto Scaling Groups |
| `aws_describe_asg` | Get full ASG details including instances |
| `aws_get_instance_asg` | Check if an instance belongs to an ASG |
| `aws_update_asg_launch_template` | Update ASG's launch template reference |
| `aws_sync_asg_launch_template_after_resize` | Compound call: create new LT version + update ASG after a vertical resize |
| `aws_put_asg_scaling_policy` | Create or update a scaling policy (Target Tracking, Step, Simple) |
| `aws_describe_asg_scaling_policies` | List scaling policies for an ASG |
| `aws_delete_asg_scaling_policy` | Remove a scaling policy |

### X-Ray Tracing (3)

| Tool | Action |
|------|--------|
| `aws_get_xray_trace_summaries` | Fetch trace summaries for a time window |
| `aws_get_xray_trace_details` | Fetch full segment data for specific trace IDs |
| `aws_get_xray_service_graph` | Get the service dependency graph |

### CloudWatch Observability (12)

| Tool | Action |
|------|--------|
| `aws_get_ec2_metrics` | Fetch EC2 metrics (CPU, network, disk) |
| `aws_get_ec2_metrics_scoped` | Fetch metrics with adaptive period resolution |
| `aws_list_log_groups` | List log groups with optional prefix filter |
| `aws_list_log_streams` | List streams within a log group |
| `aws_get_log_events` | Get raw log events from a stream |
| `aws_filter_logs` | Filter-pattern search across a log group |
| `aws_list_alarms` | List all CloudWatch alarms |
| `aws_list_ec2_alarms` | List alarms for a specific instance |
| `aws_poll_alarm_notifications` | Pull alarm notifications from an SQS queue |
| `aws_delete_alarm_notification` | Delete (acknowledge) an SQS alarm message |
| `aws_create_metric_alarm` | Create or update a CloudWatch alarm |
| `aws_get_dashboard` | Get a CloudWatch dashboard |

### VPC Networking (14)

| Tool | Action |
|------|--------|
| `aws_create_vpc` / `aws_delete_vpc` | Create or delete a VPC (force-deletes dependents) |
| `aws_list_vpcs` / `aws_get_vpc_details` | List or inspect VPCs |
| `aws_create_subnet` / `aws_delete_subnet` | Create or delete a subnet |
| `aws_create_internet_gateway` / `aws_delete_internet_gateway` | Create or delete an IGW |
| `aws_create_nat_gateway` / `aws_delete_nat_gateway` | Create or delete a NAT gateway |
| `aws_create_route_table` / `aws_delete_route_table` | Create or delete a route table |
| `aws_associate_route_table` | Associate a route table with a subnet |
| `aws_list_route_tables` | List route tables for a VPC |

### Security Groups (6)

| Tool | Action |
|------|--------|
| `aws_create_security_group` | Create a new security group |
| `aws_add_security_group_rule` | Add an ingress or egress rule |
| `aws_edit_security_group_rule` | Modify an existing rule |
| `aws_remove_security_group_rule` | Remove a rule |
| `aws_list_security_groups` | List security groups with optional filters |
| `aws_delete_security_group` | Delete a security group |

## 10. Policy Enforcement Model

`agent/policy_engine.py` validates selected actions before execution. Policy definitions live in `policies/aws_policies.yaml`.

Policy sections:

| Section | What it enforces |
|---------|-----------------|
| `ec2` | Instance creation constraints (CPU/RAM limits, allowed families) |
| `vpc` | CIDR block restrictions and prefix length validation |
| `security_groups` | Rule creation and CIDR validation |
| `ssm` | Blocked commands (rm -rf, shutdown, mkfs, etc.) and OS-safe command sets |
| `nat_gateway` | Creation awareness (cost warning) |
| `general` | Cross-cutting limits (max instances, tagging requirements) |
| `cost_optimization` | Resize guardrails (minimum savings, backup requirements) |

Policy injection is lazy: the system prompt contains only a discovery hint on startup. Full YAML sections are injected into the conversation only when the goal or an imminent tool call touches the relevant domain.

## 11. State and Audit Persistence

`agent/state_manager.py` uses a two-tier persistence model:

1. **Snapshot state** (`state/state.json`)
   - Current infrastructure representation
   - Hierarchical organization: VPCs → Subnets → Instances + Security Groups
   - Statistics counters: goals executed, resources created/deleted, cost savings
2. **Append-only audit log** (`state/audit_log.jsonl`)
   - One JSON object per line; never modified, only appended
   - Records every tool invocation, goal completion, and cost action
   - Survives crashes (partial writes don't corrupt previous lines)

Utilities:

- `sync_aws_state.py` — pulls live AWS resources and rewrites the state snapshot.
- `view_state.py` — prints statistics, infrastructure tree, and recent audit log entries.

## 12. Observability and Log-Group Semantics

CloudWatch log group mapping (from `config/observability/amazon-cloudwatch-agent.json`):

| Local path | CloudWatch log group | Contents |
|---|---|---|
| `/var/log/ai-agent/agent.log` | `/ai-agent/agent` | Alarm worker polling, agent orchestration, decision logs |
| `/var/log/messages` | `/ai-agent/system` | Host, systemd, OS-level events |
| *(ECS task stdout)* | `/ecs/cost-optimization-worker` | Cost worker analysis and action logs |
| *(Lambda stdout)* | `/aws/lambda/ai-agent-instance-alarm-cleanup` | Per-instance cleanup events on EC2 termination |

The observability helper's instruction pack and the alarm worker's `_build_goal()` both reference these log group names explicitly so the agent searches the right groups during incident triage.

## 13. Environment Variables (Key Runtime Controls)

### Global agent and AWS

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `openai` (default) or `anthropic` |
| `MAIN_CONTROLLER_MODEL` | Model for the main agent loop |
| `OBSERVABILITY_HELPER_MODEL` | Model for the observability sub-agent |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Credentials for the chosen provider |
| `AWS_REGION` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | AWS credentials (or `AWS_PROFILE`) |

### Alarm worker

| Variable | Purpose |
|---|---|
| `ALARM_SQS_QUEUE_URL` | SQS queue to poll (auto-written by `instance_alarm_setup.py`) |
| `ALARM_WORKER_AUTO_MITIGATE` | `true` = execute safe mitigations; `false` = recommend only |
| `ALARM_WORKER_PROCESS_ONLY_ALARM` | Skip OK/INSUFFICIENT_DATA messages |
| `ALARM_WORKER_REQUIRE_SUCCESS_FOR_ACK` | Only delete SQS message on agent success |
| `ALARM_WORKER_ALLOW_REBOOT_ON_STATUS_CHECK_FAILURE` | Permit autonomous reboots |
| `ALARM_WORKER_RESTART_SERVICES` | Comma-separated service names allowed for auto-restart |

Full reference: `docs/AlarmWorkerGuide.md`.

### Cost optimization worker

| Variable | Purpose |
|---|---|
| `COST_OPTIMIZATION_MODE` | `recommend_only` or `take_action` |
| `COST_OPTIMIZATION_CPU_IDLE_THRESHOLD_PERCENT` | CPU % below which an instance is over-provisioned |
| `COST_OPTIMIZATION_MAX_ACTIONS_PER_RUN` | Hard cap on resize actions per weekly run |
| `COST_OPTIMIZATION_CREATE_BACKUP` | Create AMI before resizing in take_action mode |

Full reference: `config/cost_optimization/cost-optimization.worker.env`.

## 14. Key Design Decisions

1. **MCP subprocess model** — tools are discovered dynamically at runtime over stdio; the planner is decoupled from all boto3 details.
2. **LLM provider abstraction** — the same agent loop works with any provider; tool schemas and message history use a provider-neutral format.
3. **Goal-aware instruction packs** — system prompt grows only with relevant domain guidance, keeping token overhead proportional to task complexity.
4. **Lazy policy injection** — full YAML policy content is only inserted into the conversation when a relevant tool is about to be called.
5. **MUTATING_TOOLS frozenset** — every state-changing tool is enumerated in one place; HITL approval gates and audit logs key off this set.
6. **Observability delegation** — telemetry-heavy analysis is offloaded to a separate LLM instance with a reduced tool set, preventing CloudWatch/X-Ray payloads from bloating the main planning context.
7. **Two-tier state persistence** — `state.json` is a fast-lookup snapshot; `audit_log.jsonl` is the authoritative, crash-safe history.
8. **Per-instance resource isolation** — each EC2 instance owns its own SQS queue, SNS topic, and CloudWatch alarms; a cleanup Lambda removes them atomically on termination.
9. **ASG launch template sync** — after any vertical resize, the cost worker propagates the new instance type to the ASG launch template so scale-out events inherit the optimized size.
