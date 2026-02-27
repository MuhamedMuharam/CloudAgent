# State Files Guide

## Overview
The AI Agent uses persistent state tracking to maintain audit trails and infrastructure snapshots. This is crucial for your bachelor thesis evaluation.

## State Files Location
All state files are stored in: `state/`

---

## 📄 state.json
**Purpose:** Current infrastructure snapshot and statistics

**Structure:**
```json
{
  "version": "1.0",
  "initialized_at": "2026-02-27T03:29:08.517421",
  "last_updated": "2026-02-27T20:45:00.000000",
  "providers": {
    "aws": {
      "ec2_instances": [
        {
          "id": "i-1234567890abcdef0",
          "name": "my-instance",
          "type": "t3.micro",
          "state": "running",
          "public_ip": "54.123.45.67",
          "private_ip": "10.0.1.123",
          "launch_time": "2026-02-27T20:30:00.000000"
        }
      ],
      "last_sync": "2026-02-27T20:45:00.000000"
    },
    "azure": {
      "vms": [],
      "last_sync": null
    },
    "gcp": {
      "instances": [],
      "last_sync": null
    }
  },
  "statistics": {
    "total_goals_executed": 5,
    "total_resources_created": 3,
    "total_resources_deleted": 1
  }
}
```

**When Updated:**
- `sync_aws_state.py` - Updates AWS EC2 instances from actual AWS state
- Agent execution - Updates statistics when goals complete
- Resource creation/deletion - Increments counters

**Use Cases:**
- View current tracked infrastructure: `python view_state.py`
- Sync with AWS reality: `python sync_aws_state.py`
- Detect drift between expected and actual state

---

## 📄 audit_log.jsonl
**Purpose:** Append-only log of ALL agent actions (JSONL = JSON Lines format)

**Format:** Each line is a separate JSON object
```jsonl
{"timestamp": "2026-02-27T20:30:15.123456", "action": "goal_executed", "details": {"goal": "Create instance", "outcome": "Success", "actions_taken": ["aws_create_ec2_instance({...})"]}}
{"timestamp": "2026-02-27T20:30:16.234567", "action": "resource_created", "details": {"provider": "aws", "resource_type": "ec2_instance", "resource_id": "i-abc123", "resource_name": "my-server"}}
{"timestamp": "2026-02-27T20:35:22.345678", "action": "resource_deleted", "details": {"provider": "aws", "resource_type": "ec2_instance", "resource_id": "i-abc123"}}
```

**Action Types:**
- `goal_executed` - When agent completes a goal
- `resource_created` - EC2 instance/VM created
- `resource_deleted` - EC2 instance/VM terminated

**When Updated:**
- Every time agent executes a goal
- Every resource creation (via tool calls)
- Every resource deletion (via tool calls)

**Use Cases:**
- Full audit trail for thesis evaluation: `python view_state.py --log`
- Prove autonomous decision-making
- Debug agent behavior
- Show timeline of infrastructure changes

---

## 🔧 Utility Scripts

### sync_aws_state.py
**Purpose:** Sync state.json with actual AWS infrastructure

```bash
python sync_aws_state.py
```

**What it does:**
1. Queries AWS EC2 API for all instances
2. Updates `state.json` → `providers.aws.ec2_instances`
3. Sets `last_sync` timestamp

**When to use:**
- Before first agent run (establish baseline)
- After manual AWS changes (outside agent)
- To detect drift/discrepancies

---

### view_state.py
**Purpose:** View state and audit logs in human-readable format

```bash
# View current state snapshot
python view_state.py

# View detailed audit log
python view_state.py --log

# View last N log entries
python view_state.py --log --limit 10

# View statistics only
python view_state.py --stats

# Refresh from AWS first, then display
python view_state.py --sync
```

**Output:**
- Infrastructure snapshot (instances tracked)
- Statistics (goals, resources created/deleted)
- Recent actions (last 5 by default)
- Full audit trail (with --log flag)

---

## 🎓 Thesis Benefits

### 1. Autonomous Decision Tracking
- `audit_log.jsonl` proves agent made decisions independently
- Shows which tools were called and when
- Documents reasoning through goals executed

### 2. Infrastructure State Management
- `state.json` shows "desired state" vs actual AWS state
- Demonstrates infrastructure-as-code principles
- Tracks drift detection capability

### 3. Evaluation Metrics
From `state.json` → `statistics`:
- **Goals executed:** How many high-level tasks completed
- **Resources created:** Infrastructure provisioned by agent
- **Resources deleted:** Cleanup operations performed

### 4. Reproducibility
- Audit log allows replaying agent decisions
- State snapshots enable before/after comparisons
- Timestamps prove chronological ordering

---

## 🔍 Common Issues & Fixes

### Issue: "No instances tracked" but AWS has instances
**Solution:** Run `python sync_aws_state.py` to sync from AWS

### Issue: State file corrupted/invalid JSON
**Solution:** Delete `state/state.json` - it will auto-regenerate on next run

### Issue: Audit log too large
**Solution:** Archive old logs:
```bash
mv state/audit_log.jsonl state/audit_log_backup_$(date +%Y%m%d).jsonl
```
A new audit_log.jsonl will be created automatically.

### Issue: DateTime serialization errors
**Solution:** Already fixed! `state_manager.py` now uses `default=str` for JSON serialization.

---

## 📊 Example Workflow

```bash
# 1. Establish baseline (before agent runs)
python sync_aws_state.py

# 2. Run agent with goal
python main.py

# 3. View what happened
python view_state.py

# 4. See detailed audit trail
python view_state.py --log

# 5. Verify AWS matches expectations
python sync_aws_state.py
python view_state.py
```

---

## 🗂️ File Ownership

| File | Written By | Read By |
|------|-----------|---------|
| `state.json` | StateManager, sync_aws_state.py | view_state.py, agent |
| `audit_log.jsonl` | StateManager (agent core) | view_state.py |
| `sync_aws_state.py` | You (manual) | N/A (script) |
| `view_state.py` | You (manual) | N/A (script) |

---
