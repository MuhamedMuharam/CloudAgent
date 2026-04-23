# Horizontal Scaling & ASG Integration Guide

## Overview

This document covers the Auto Scaling Group (ASG) and Launch Template capabilities added to the AI agent, and explains how vertical scaling (EC2 rightsizing) and horizontal scaling (ASG) are integrated.

---

## Vertical ↔ Horizontal Scaling Integration

### The problem it solves

When the cost optimization worker **resizes an EC2 instance** (e.g. `t3.medium` → `t3.small`), only that one instance changes. If the ASG later scales out, new instances are launched from the existing launch template — which still has the old instance type. This integration closes that gap.

### How it works

After every successful rightsizing action (`mode == "applied"`), the worker automatically calls `aws_sync_asg_launch_template_after_resize`:

```
resize instance → apply_ec2_rightsizing (mode=applied)
                       │
                       ▼
         aws_sync_asg_launch_template_after_resize
                       │
          ┌────────────┼─────────────────┐
          ▼            ▼                 ▼
   get_instance_asg  create_lt_version  update_asg
   (is it in ASG?)   (new instance type) (use $Latest)
```

1. Checks if the instance belongs to an ASG (`describe_auto_scaling_instances`).
2. Reads the ASG's current launch template reference.
3. Creates a **new launch template version** based on `$Default`, overriding only `InstanceType`.
4. Optionally sets the new version as the LT default (controlled by `set_lt_as_default`, default: `true`).
5. Updates the ASG's version pointer to `$Latest`.

If the instance is **not in an ASG**, the sync step is skipped gracefully (`synced: false`).

### Logged events

| Event | When |
|-------|------|
| `asg_launch_template_sync_finished` | After every apply attempt (success or fail) |

Key fields: `synced`, `asg_name`, `new_instance_type`, `new_lt_version`, `reason`.

---

## New Cloud Provider Module: `cloud_providers/aws/asg.py`

### `ASGManager` — method reference

#### Launch Template methods

| Method | Description |
|--------|-------------|
| `list_launch_templates(name_filter)` | List all LTs, optionally filtered by name substring |
| `describe_launch_template(id, name, versions)` | Get LT details + selected versions |
| `create_launch_template_version(id, name, source_version, new_instance_type, description, set_as_default)` | Create a new LT version, inheriting all settings and optionally overriding instance type |
| `set_launch_template_default_version(version, id, name)` | Change which version is the template default |

#### ASG methods

| Method | Description |
|--------|-------------|
| `list_asgs(asg_names)` | List ASGs with config summary |
| `describe_asg(asg_name)` | Full details — instances, LT ref, policies, tags |
| `get_instance_asg(instance_id)` | Find which ASG an instance belongs to |
| `update_asg_launch_template(asg_name, id, name, version)` | Point ASG at a specific LT version |
| `sync_asg_after_resize(instance_id, new_instance_type, set_lt_as_default)` | High-level: do steps 1–5 above |

#### Scaling Policy methods

| Method | Description |
|--------|-------------|
| `put_scaling_policy(...)` | Create or update a policy (all three types) |
| `describe_scaling_policies(asg_name)` | List all policies on an ASG |
| `delete_scaling_policy(asg_name, policy_name)` | Remove a policy |

---

## New MCP Tools

### Launch Template tools

| Tool | Purpose |
|------|---------|
| `aws_list_launch_templates` | List all launch templates |
| `aws_describe_launch_template` | Describe a LT and its versions |
| `aws_create_launch_template_version` | Create a new version with optional instance type override |
| `aws_set_launch_template_default_version` | Change the default version |

### ASG tools

| Tool | Purpose |
|------|---------|
| `aws_list_asgs` | List all Auto Scaling Groups |
| `aws_describe_asg` | Full details of one ASG |
| `aws_get_instance_asg` | Check if an instance is in an ASG |
| `aws_update_asg_launch_template` | Point an ASG at a specific LT version |
| `aws_sync_asg_launch_template_after_resize` | Compound: propagate vertical resize to ASG |

### Scaling Policy tools

| Tool | Purpose |
|------|---------|
| `aws_put_asg_scaling_policy` | Create/update a scaling policy (all types) |
| `aws_describe_asg_scaling_policies` | List policies on an ASG |
| `aws_delete_asg_scaling_policy` | Remove a policy |

---

## Scaling Policy Quick Reference

### Target Tracking (recommended for most cases)

AWS automatically adds/removes instances to keep a metric at the target. No CloudWatch alarm needed — AWS creates them.

```
aws_put_asg_scaling_policy(
    asg_name="my-asg",
    policy_name="cpu-target-tracking",
    policy_type="TargetTrackingScaling",
    target_value=50.0,                              # keep CPU at 50%
    predefined_metric_type="ASGAverageCPUUtilization"
)
```

Available `predefined_metric_type` values:

| Value | Tracks |
|-------|--------|
| `ASGAverageCPUUtilization` | Average CPU % across ASG |
| `ASGAverageNetworkIn` | Average bytes received per instance |
| `ASGAverageNetworkOut` | Average bytes sent per instance |
| `ALBRequestCountPerTarget` | Requests per target in a target group |

### Simple Scaling

Scale by a fixed amount when a CloudWatch alarm fires. Uses a cooldown to avoid rapid changes.

```
aws_put_asg_scaling_policy(
    asg_name="my-asg",
    policy_name="scale-out-simple",
    policy_type="SimpleScaling",
    adjustment_type="ChangeInCapacity",
    scaling_adjustment=2,     # add 2 instances
    cooldown=300              # wait 5 min before next action
)
```

### Step Scaling

Scale in variable increments based on how far the metric breaches the threshold.

```
aws_put_asg_scaling_policy(
    asg_name="my-asg",
    policy_name="scale-out-steps",
    policy_type="StepScaling",
    adjustment_type="ChangeInCapacity",
    step_adjustments=[
        {"MetricIntervalLowerBound": 0,  "MetricIntervalUpperBound": 20, "ScalingAdjustment": 1},
        {"MetricIntervalLowerBound": 20, "MetricIntervalUpperBound": 40, "ScalingAdjustment": 2},
        {"MetricIntervalLowerBound": 40,                                 "ScalingAdjustment": 4}
    ],
    estimated_instance_warmup=120
)
```

`adjustment_type` options: `ChangeInCapacity` | `ExactCapacity` | `PercentChangeInCapacity`

---

## Important Constraints

- **Launch configuration vs launch template**: `sync_asg_after_resize` only works when the ASG uses a **launch template**. If the ASG uses an older launch configuration, the tool returns `synced: false` with a message to update manually.
- **MixedInstancesPolicy**: ASGs with a `MixedInstancesPolicy` (Spot + On-Demand mix) store the launch template inside the policy, not at the top level. The current implementation reads the top-level `LaunchTemplate` field only. For mixed-instances ASGs, use `aws_describe_asg` to inspect the structure and `aws_update_asg_launch_template` manually.
- **New instances don't auto-replace existing ones**: Updating the launch template version affects only **future** scale-out launches. Existing instances in the ASG keep their current type. Use an instance refresh (`StartInstanceRefresh` API) to roll existing instances to the new type — not yet implemented as an MCP tool.
- **IAM permissions required**: The agent's AWS credentials need `autoscaling:*` and `ec2:*LaunchTemplate*` permissions in addition to the existing EC2 permissions.
