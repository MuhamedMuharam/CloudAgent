# Complete Code Walkthrough (Current Architecture)

This document is a current, code-aligned walkthrough of the project architecture.
It is intended as a technical reference for implementation understanding and thesis writing.

## 1. Project Purpose

This repository implements an autonomous cloud operations agent that:

1. Accepts natural-language goals.
2. Uses an LLM to plan and decide which cloud operations to execute.
3. Calls AWS capabilities through MCP tools (Model Context Protocol).
4. Applies policy checks before execution.
5. Persists state and an audit trail for traceability.
6. Supports observability-driven operations with CloudWatch, X-Ray, and SSM.

In addition, the project includes a deployable FastAPI + Celery reference workload used to generate realistic logs, metrics, and traces for incident simulation and root-cause analysis.

## 2. Repository Structure

High-level modules and responsibilities:

1. `main.py`: Interactive entry point for goal-driven agent execution.
2. `agent/`: Core orchestration logic.
3. `mcp_servers/`: MCP server implementations (AWS fully implemented).
4. `cloud_providers/aws/`: AWS manager classes wrapping boto3 APIs.
5. `alarm_worker.py`: Event-driven worker that polls SQS alarm notifications and triggers agent triage.
6. `config/observability/`: CloudWatch agent config for log and metric collection.
8. `state/`: Persistent snapshot (`state.json`) and append-only action log (`audit_log.jsonl`).
9. `policies/`: YAML policy constraints enforced before tool execution.
10. `docs/`: Operational and architecture documentation.

## 3. Python Package Initialization (`__init__.py`)

`__init__.py` files define package boundaries and convenient exports.

### `cloud_providers/aws/__init__.py`

Exports current AWS managers and helpers:

1. `EC2Manager`
2. `VPCManager`
3. `SecurityGroupManager`
4. `CloudWatchManager`
5. `SSMManager`
6. `XRayManager`
7. `map_generic_to_instance_type`

### `agent/__init__.py`

Exports key agent entry symbols:

1. `run_agent`
2. `StateManager`

## 4. MCP Architecture (Current)

The implementation uses two complementary libraries:

1. `mcp` (official SDK) on the client side in `agent/mcp_client.py`.
2. `fastmcp` on the server side in `mcp_servers/aws_server.py`.

Communication pattern:

1. Agent process spawns AWS MCP server as a subprocess.
2. Client and server communicate over stdio using JSON-RPC (MCP protocol).
3. Agent discovers tools, resources, resource templates, and prompts.
4. LLM chooses tool calls; client routes calls to the owning MCP server.

Design consequence: the planner (`agent/core.py`) is decoupled from boto3 implementation details.

## 5. Execution Flow A: Interactive Goal (`python main.py`)

### Step-by-step sequence

1. `main.py` defines a goal and calls `run_agent_sync(goal)`.
2. `agent/core.py` initializes:
   - OpenAI client
   - MCPClientManager
   - StateManager
   - PolicyEngine
3. Core connects to MCP servers (default AWS server subprocess).
4. MCP client discovers capabilities:
   - tools
   - resources
   - resource templates
   - prompts
5. System prompt is built via goal-aware instruction packs.
6. Agent loop starts:
   - LLM returns either tool calls or final response
   - Before executing each tool: `PolicyEngine.validate_action(...)`
   - Tool executes through MCP server
   - Tool result is appended to conversation and logged
7. Loop exits on final assistant response or max iterations.
8. Core closes MCP sessions/subprocesses and writes final goal execution log.

### Important implementation notes

1. Tool routing is dynamic via `tool_server_mapping`.
2. Synthetic helper tools are added for MCP non-tool capabilities:
   - `read_mcp_resource`
   - `get_mcp_prompt`
3. Pre-router in `agent/core.py` can preload explicitly requested resources/prompts before first model turn.

## 6. Execution Flow B: Alarm-Driven Worker (`python alarm_worker.py`)

`alarm_worker.py` is a continuously running triage loop:

1. Polls SQS for alarm notifications via `CloudWatchManager.poll_alarm_notifications`.
2. Parses SNS-wrapped CloudWatch alarm payload.
3. Extracts alarm context (name, state, reason, InstanceId dimension if present).
4. Builds a targeted triage goal.
5. Calls `run_agent_sync(goal)` to perform automated diagnosis.
6. Acknowledges/deletes message on successful processing.
7. Leaves message unacked on failure (reappears after visibility timeout).

This turns CloudWatch alarms into event-triggered autonomous response actions.

## 7. Current AWS MCP Surface (Server Capabilities)

The AWS MCP server currently exposes:

1. 47 tools (`@mcp.tool()`)
2. 3 resources (`@mcp.resource(...)`)
3. 2 prompt templates (`@mcp.prompt()`)

### Tool groups

1. EC2 lifecycle and status
   - list/create/delete/get status/start/stop/reboot
   - SSM managed-instance status check
2. SSM remote execution and service control
   - run command, get output
   - start/stop/restart/status/list services
3. X-Ray tracing
   - trace summaries
   - trace details
   - service graph
4. CloudWatch observability
   - EC2 and agent metrics
   - log groups/streams/events/filter
   - alarms list and EC2-specific alarm listing
   - SQS alarm notification poll/delete
   - create metric alarm
   - get dashboard
5. VPC networking
   - create/list/get/delete VPC
   - create/delete subnet, IGW, NAT gateway, route table
   - associate route tables, list route tables
6. Security groups
   - create SG
   - add SG rule
   - list SGs
   - delete SG

### Resources

1. `aws://observability/snapshot`
2. `aws://observability/ec2/{instance_id}/snapshot`
3. `aws://observability/log-group/{log_group_name}/snapshot`

### Prompts

1. `aws_incident_triage_prompt`
2. `aws_observability_snapshot_interpreter_prompt`

## 8. Policy Enforcement Model

`agent/policy_engine.py` validates selected actions before execution. Policy definitions live in `policies/aws_policies.yaml`.

Current enforced validators include:

1. EC2 creation constraints (CPU/RAM limits).
2. VPC CIDR restrictions and prefix validation.
3. Security group creation/rule checks.
4. NAT gateway creation awareness.

Current policy sections in YAML:

1. `ec2`
2. `vpc`
3. `security_groups`
4. `nat_gateway`
5. `general`

## 9. State and Audit Persistence

`agent/state_manager.py` uses a two-tier persistence model:

1. Snapshot state (`state/state.json`)
   - current infrastructure representation
   - hierarchical AWS organization (VPCs, subnets, instances, SGs)
2. Append-only audit log (`state/audit_log.jsonl`)
   - every action/goal execution over time
   - supports traceability and post-hoc analysis

Utilities:

1. `sync_aws_state.py`: pull live AWS resources and rewrite state snapshot.
2. `view_state.py`: report statistics/logs and optional sync.

## 10. Observability and Log-Group Semantics

Current CloudWatch log mapping (from `config/observability/amazon-cloudwatch-agent.json`):

1. `/var/log/ai-agent/agent.log` -> `/ai-agent/agent`
2. `/var/log/messages` -> `/ai-agent/system`
3. `/var/log/mern-app/api.log` -> `/mern-app/api`

Operational interpretation:

1. `/ai-agent/agent`: alarm_worker polling, agent orchestration, and decision logs.
2. `/ai-agent/system`: host, systemd, OS-level events.
3. `/mern-app/api`: MERN application request and error logs (Express, MongoDB, Node.js).

`agent/core.py` and `agent/observability_helper.py` instruction packs include this log-group routing guidance for incident triage.

## 12. Deployment and Runtime Path Reality

A common operational pitfall is source location mismatch:

1. Git repository code lives under repo root.
2. Deployed systemd services run from `/opt/real-service/src`.

Therefore, after `git pull`, code changes are not active until synced to `/opt/real-service/src` and services are restarted.

This is captured in `docs/FastApiServiceGuide.md` under update workflow.

## 13. Environment Variables (Key Runtime Controls)

### Global agent and AWS

1. `OPENAI_API_KEY`
2. `AWS_REGION`
3. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (or `AWS_PROFILE`)

### Optional CloudWatch defaults

1. `CW_DASHBOARD_NAME`
2. `CW_TEST_LOG_GROUP`
3. `CW_TEST_INSTANCE_ID`
4. `CW_TEST_SNS_ARN`

### Alarm worker

1. `ALARM_SQS_QUEUE_URL`
2. `ALARM_TARGET_INSTANCE_NAME`
3. `ALARM_WORKER_LOG_LEVEL`
4. `ALARM_WORKER_MAX_MESSAGES`
5. `ALARM_WORKER_WAIT_TIME_SECONDS`
6. `ALARM_WORKER_VISIBILITY_TIMEOUT`
7. `ALARM_WORKER_LOOP_SLEEP_SECONDS`
8. `ALARM_WORKER_PROCESS_ONLY_ALARM`

## 14. What Changed Compared to Older Walkthrough Versions

Previous walkthrough versions commonly reflected an early EC2-only MCP server.
Current implementation has evolved to a full observability and operations platform with:

1. SSM command and host service management.
2. CloudWatch metrics/logs/alarms + SQS alarm polling.
3. X-Ray trace analysis and service graph tooling.
4. VPC and security group lifecycle management.
5. Event-driven alarm worker execution path.
6. Real FastAPI/Celery workload with distributed tracing.
7. Richer agent prompt guidance for SSM/X-Ray/log triage behavior.

## 15. Thesis-Oriented Summary

From a systems perspective, this project demonstrates:

1. LLM planning over a typed tool layer (MCP).
2. Separation of concerns between reasoning, protocol dispatch, and cloud API execution.
3. Policy-constrained autonomy.
4. End-to-end observability-driven incident analysis.
5. Persistent auditability of autonomous actions.

This combination makes the architecture suitable for discussing practical autonomous cloud operations, safety controls, and explainable decision trails in an academic setting.
