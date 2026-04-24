"""
MCP AWS Server
Exposes AWS EC2 operations as MCP tools using FastMCP.

This server implements the Model Context Protocol (MCP) to provide
cloud infrastructure management capabilities to AI agents.

Run this server with: python mcp_servers/aws_server.py
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone

# Import AWS managers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from cloud_providers.aws.ec2 import EC2Manager
from cloud_providers.aws.vpc import VPCManager
from cloud_providers.aws.security import SecurityGroupManager
from cloud_providers.aws.cloudwatch import CloudWatchManager
from cloud_providers.aws.ssm import SSMManager
from cloud_providers.aws.xray import XRayManager
from cloud_providers.aws.asg import ASGManager

# MCP FastMCP import
from fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("mcp-aws-server")

# Initialize EC2 manager with region from environment
region = os.getenv('AWS_REGION', 'us-east-1')
default_dashboard_name = os.getenv('CW_DASHBOARD_NAME')
default_log_group = os.getenv('CW_TEST_LOG_GROUP')
default_instance_id = os.getenv('CW_TEST_INSTANCE_ID')
default_sns_arn = os.getenv('CW_TEST_SNS_ARN')
default_alarm_queue_url = os.getenv('ALARM_SQS_QUEUE_URL')
print(f"🚀 Initializing MCP AWS Server (region: {region})", file=sys.stderr)

try:
    ec2_manager = EC2Manager(region=region)
    print("✅ AWS EC2 Manager initialized", file=sys.stderr)
    
    vpc_manager = VPCManager(region=region)
    print("✅ AWS VPC Manager initialized", file=sys.stderr)
    
    security_manager = SecurityGroupManager(region=region)
    print("✅ AWS Security Group Manager initialized", file=sys.stderr)

    cloudwatch_manager = CloudWatchManager(region=region)
    print("✅ AWS CloudWatch Manager initialized", file=sys.stderr)

    ssm_manager = SSMManager(region=region)
    print("✅ AWS SSM Manager initialized", file=sys.stderr)

    xray_manager = XRayManager(region=region)
    print("✅ AWS X-Ray Manager initialized", file=sys.stderr)

    asg_manager = ASGManager(region=region)
    print("✅ AWS ASG Manager initialized", file=sys.stderr)
except Exception as e:
    print(f"❌ Failed to initialize AWS Managers: {e}", file=sys.stderr)
    print("⚠️  Make sure AWS credentials are configured", file=sys.stderr)
    sys.exit(1)


def _build_observability_snapshot(
    instance_id: str = None,
    log_group_name: str = None,
    minutes: int = 15,
    metric_period_seconds: int = 60,
    log_limit: int = 50,
    alarm_state: str = None,
) -> dict:
    """
    Build a compact observability snapshot combining metrics, logs, and alarms.
    """
    resolved_instance_id = instance_id or default_instance_id
    resolved_log_group = log_group_name or default_log_group

    snapshot = {
        "region": region,
        "window_minutes": minutes,
        "instance_id": resolved_instance_id,
        "log_group_name": resolved_log_group,
        "metrics": None,
        "recent_logs": None,
        "alarms": None,
    }

    if resolved_instance_id:
        try:
            snapshot["metrics"] = cloudwatch_manager.get_ec2_metrics(
                instance_id=resolved_instance_id,
                minutes=minutes,
                period_seconds=metric_period_seconds,
            )
        except Exception as metrics_error:
            snapshot["metrics"] = {"error": str(metrics_error)}

        try:
            alarms = cloudwatch_manager.list_alarms(
                state_value=alarm_state,
                alarm_name_prefix=None,
                max_records=100,
            )
            matched_alarms = []
            for alarm in alarms:
                dims = alarm.get("dimensions", [])
                if any(d.get("name") == "InstanceId" and d.get("value") == resolved_instance_id for d in dims):
                    matched_alarms.append(alarm)

            snapshot["alarms"] = {
                "count": len(matched_alarms),
                "items": matched_alarms,
            }
        except Exception as alarms_error:
            snapshot["alarms"] = {"error": str(alarms_error)}

    if resolved_log_group:
        try:
            logs_result = cloudwatch_manager.filter_logs(
                log_group_name=resolved_log_group,
                filter_pattern="",
                minutes=minutes,
                limit=log_limit,
            )
            snapshot["recent_logs"] = logs_result
        except Exception as logs_error:
            snapshot["recent_logs"] = {"error": str(logs_error)}

    return snapshot


def _metric_values_from_series(metrics_bucket: dict, metric_name: str) -> list:
    metric_item = metrics_bucket.get(metric_name, {}) if isinstance(metrics_bucket, dict) else {}
    datapoints = metric_item.get("datapoints", []) if isinstance(metric_item, dict) else []
    values = []
    for datapoint in datapoints:
        value = datapoint.get("value")
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _normalize_metric_scope(scope: str) -> str:
    normalized = re.sub(r"[\s_]+", "-", (scope or "all").strip().lower())
    if normalized.endswith("-only"):
        normalized = normalized[:-5]

    aliases = {
        "mem": "memory",
        "ram": "memory",
        "net": "network",
        "health": "status",
        "checks": "status",
        "filesystem": "disk",
        "storage": "disk",
        "io": "disk",
    }
    return aliases.get(normalized, normalized)


def _canonical_metric_name(metric_name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (metric_name or "").strip().lower())


def _average(values: list):
    if not values:
        return None
    return sum(values) / len(values)


def _resolve_cost_metric_period(
    minutes: int,
    requested_period_seconds: int,
    max_datapoints_per_metric: int = 600,
) -> tuple:
    safe_minutes = max(1, int(minutes or 1))
    safe_requested_period = max(60, int(requested_period_seconds or 60))
    safe_requested_period = ((safe_requested_period + 59) // 60) * 60

    safe_max_datapoints = max(200, int(max_datapoints_per_metric or 1200))
    window_seconds = safe_minutes * 60
    minimum_period_from_window = (window_seconds + safe_max_datapoints - 1) // safe_max_datapoints
    minimum_period_from_window = max(60, minimum_period_from_window)
    minimum_period_from_window = ((minimum_period_from_window + 59) // 60) * 60

    if safe_requested_period >= minimum_period_from_window:
        return safe_requested_period, "requested"

    return minimum_period_from_window, "adapted_for_window"


def _estimate_datapoints_per_metric(minutes: int, period_seconds: int) -> int:
    window_seconds = max(1, int(minutes or 1)) * 60
    safe_period = max(60, int(period_seconds or 60))
    return max(1, (window_seconds + safe_period - 1) // safe_period)


def _summarize_metric_bucket(metrics_bucket: dict) -> dict:
    if not isinstance(metrics_bucket, dict):
        return {
            "total_metrics": 0,
            "metrics_with_data": 0,
            "metrics_without_data": [],
            "total_datapoints": 0,
        }

    total_metrics = len(metrics_bucket)
    metrics_with_data = 0
    metrics_without_data = []
    total_datapoints = 0

    for metric_name, metric_payload in metrics_bucket.items():
        datapoints = metric_payload.get("datapoints", []) if isinstance(metric_payload, dict) else []
        datapoint_count = len(datapoints)
        total_datapoints += datapoint_count
        if datapoint_count > 0:
            metrics_with_data += 1
        else:
            metrics_without_data.append(metric_name)

    return {
        "total_metrics": total_metrics,
        "metrics_with_data": metrics_with_data,
        "metrics_without_data": metrics_without_data,
        "total_datapoints": total_datapoints,
    }


def _build_scoped_metrics_view(metrics_payload: dict, scope: str) -> dict:
    scope_lc = _normalize_metric_scope(scope)

    primary_metrics = metrics_payload.get("metrics", {}) if isinstance(metrics_payload, dict) else {}
    agent_metrics = metrics_payload.get("agent_metrics", {}) if isinstance(metrics_payload, dict) else {}

    scope_map = {
        "cpu": ({"CPUUtilization"}, set()),
        "network": ({"NetworkIn", "NetworkOut"}, set()),
        "status": ({"StatusCheckFailed", "StatusCheckFailed_Instance", "StatusCheckFailed_System"}, set()),
        "disk": (set(), {"disk_used_percent", "disk_inodes_free", "diskio_io_time"}),
        "memory": (set(), {"mem_used_percent", "swap_used_percent"}),
    }

    if scope_lc in {"all", "full", "*"}:
        selected_primary = primary_metrics
        selected_agent = agent_metrics
        normalized_scope = "all"
    elif scope_lc in scope_map:
        wanted_primary, wanted_agent = scope_map[scope_lc]
        selected_primary = {k: v for k, v in primary_metrics.items() if k in wanted_primary}
        selected_agent = {k: v for k, v in agent_metrics.items() if k in wanted_agent}
        normalized_scope = scope_lc
    else:
        selected_primary = primary_metrics
        selected_agent = agent_metrics
        normalized_scope = "all"

    return {
        "scope": normalized_scope,
        "metrics": selected_primary,
        "agent_metrics": selected_agent,
        "availability_summary": {
            "primary": _summarize_metric_bucket(selected_primary),
            "agent": _summarize_metric_bucket(selected_agent),
        },
    }


def _build_metric_name_filtered_view(metrics_payload: dict, metric_names: list) -> dict:
    primary_metrics = metrics_payload.get("metrics", {}) if isinstance(metrics_payload, dict) else {}
    agent_metrics = metrics_payload.get("agent_metrics", {}) if isinstance(metrics_payload, dict) else {}

    requested_metric_names = [str(name).strip() for name in (metric_names or []) if str(name).strip()]
    lookup = {}

    for key, value in primary_metrics.items():
        lookup[_canonical_metric_name(key)] = ("metrics", key, value)
    for key, value in agent_metrics.items():
        lookup[_canonical_metric_name(key)] = ("agent_metrics", key, value)

    selected_primary = {}
    selected_agent = {}
    resolved_metric_names = []
    missing_metric_names = []

    for requested_name in requested_metric_names:
        canonical = _canonical_metric_name(requested_name)
        hit = lookup.get(canonical)
        if not hit:
            missing_metric_names.append(requested_name)
            continue

        bucket_name, key, value = hit
        resolved_metric_names.append(key)
        if bucket_name == "metrics":
            selected_primary[key] = value
        else:
            selected_agent[key] = value

    return {
        "metrics": selected_primary,
        "agent_metrics": selected_agent,
        "requested_metric_names": requested_metric_names,
        "resolved_metric_names": sorted(set(resolved_metric_names)),
        "missing_metric_names": missing_metric_names,
        "availability_summary": {
            "primary": _summarize_metric_bucket(selected_primary),
            "agent": _summarize_metric_bucket(selected_agent),
        },
    }


@mcp.tool()
async def aws_list_ec2_instances(tag_filter: dict = None) -> dict:
    """
    List all EC2 instances in the AWS account.
    Returns instance ID, name, type, state, and IP addresses.
    Optionally filter by tags (e.g., only instances managed by AI Agent).
    
    Args:
        tag_filter: Optional tags to filter instances (e.g., {'ManagedBy': 'AIAgent'})
    
    Returns:
        Dictionary with success status, count, and list of instances
    """
    try:
        instances = ec2_manager.list_instances(tag_filter=tag_filter)
        return {
            "success": True,
            "count": len(instances),
            "instances": instances
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_ec2_instance(
    name: str,
    cpu: int = 2,
    ram: int = 4,
    tags: dict = None,
    instance_type: str = None,
    os_name: str = "amazon-linux-2023",
    vpc_id: str = "default",
    security_group_id: str = "default",
    auto_assign_public_ip: bool = True,
) -> dict:
    """
    Create a new EC2 instance with specified resources.
    Automatically selects appropriate instance type based on CPU/RAM.
    Instances are tagged as 'ManagedBy: AIAgent' for tracking.
    
    Args:
        name: Name for the EC2 instance
        cpu: Number of virtual CPU cores (default: 2)
        ram: RAM in gigabytes (default: 4)
        tags: Optional tags for policy/budget attribution (Environment, Project, etc.)
        instance_type: Optional explicit instance type override
        os_name: OS selector (default: amazon-linux-2023; supports explicit ami-...)
        vpc_id: Target VPC ID or Name tag (default: default VPC)
        security_group_id: Security group ID or name (default: default SG in selected VPC)
        auto_assign_public_ip: Auto-assign public IPv4 on primary ENI (default: true)
    
    Returns:
        Dictionary with success status, message, and instance details
    """
    try:
        instance = ec2_manager.create_instance(
            name=name,
            cpu=cpu,
            ram=ram,
            tags=tags,
            instance_type=instance_type,
            os_name=os_name,
            vpc_id=vpc_id,
            security_group_id=security_group_id,
            auto_assign_public_ip=auto_assign_public_ip,
        )
        return {
            "success": True,
            "message": f"EC2 instance '{name}' created successfully",
            "instance": instance
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_ec2_instance(instance_id: str) -> dict:
    """
    Terminate (delete) an EC2 instance by ID.
    This action is irreversible. The instance will be stopped and deleted.
    
    Args:
        instance_id: EC2 instance ID (e.g., 'i-1234567890abcdef0')
    
    Returns:
        Dictionary with success status and termination details
    """
    try:
        result_data = ec2_manager.delete_instance(instance_id)
        return {
            "success": True,
            "message": f"Instance {instance_id} termination initiated",
            "details": result_data
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_get_ec2_instance_status(instance_id: str) -> dict:
    """
    Get detailed status and information about a specific EC2 instance.
    Returns current state, IP addresses, instance type, and launch time.
    
    Args:
        instance_id: EC2 instance ID to query
    
    Returns:
        Dictionary with success status and instance details
    """
    try:
        status = ec2_manager.get_instance_status(instance_id)
        return {
            "success": True,
            "instance": status
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_start_ec2_instance(instance_id: str) -> dict:
    """
    Start a stopped EC2 instance.

    Args:
        instance_id: EC2 instance ID

    Returns:
        Dictionary with success status and state transition details
    """
    try:
        result = ec2_manager.start_instance(instance_id)
        return {
            "success": True,
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_stop_ec2_instance(instance_id: str, force: bool = False) -> dict:
    """
    Stop a running EC2 instance.

    Args:
        instance_id: EC2 instance ID
        force: Force stop if graceful shutdown fails

    Returns:
        Dictionary with success status and state transition details
    """
    try:
        result = ec2_manager.stop_instance(instance_id, force=force)
        return {
            "success": True,
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_reboot_ec2_instance(instance_id: str) -> dict:
    """
    Reboot a running EC2 instance.

    Args:
        instance_id: EC2 instance ID

    Returns:
        Dictionary with success status
    """
    try:
        result = ec2_manager.reboot_instance(instance_id)
        return {
            "success": True,
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_get_ec2_instance_ssm_status(instance_id: str) -> dict:
    """
    Check whether an EC2 instance is managed by SSM and online.

    Args:
        instance_id: EC2 instance ID

    Returns:
        Dictionary with SSM managed-instance status
    """
    try:
        status = ec2_manager.get_instance_ssm_status(instance_id)
        return {
            "success": True,
            "status": status
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_collect_ec2_health_snapshot(
    instance_id: str,
    minutes: int = 15,
    period_seconds: int = 60,
    include_agent_metrics: bool = True,
    include_alarms: bool = True,
    alarm_state_value: str = "ALARM",
) -> dict:
    """
    Build a compact EC2 operational health snapshot.

    This combines instance status, EC2 host health checks, SSM managed status,
    recent CloudWatch metrics, and related alarms by InstanceId dimension.

    Args:
        instance_id: EC2 instance ID
        minutes: Lookback window in minutes for metrics
        period_seconds: CloudWatch period for metrics
        include_agent_metrics: Include CWAgent metrics
        include_alarms: Include related CloudWatch alarms
        alarm_state_value: Alarm state filter when include_alarms=True
    """
    try:
        status = ec2_manager.get_instance_status(instance_id)
        health_checks = ec2_manager.get_instance_health_checks(instance_id)
        ssm_status = ec2_manager.get_instance_ssm_status(instance_id)

        metrics = cloudwatch_manager.get_ec2_metrics(
            instance_id=instance_id,
            minutes=minutes,
            period_seconds=period_seconds,
            include_agent_metrics=include_agent_metrics,
        )

        related_alarms = []
        if include_alarms:
            alarms = cloudwatch_manager.list_alarms(
                state_value=alarm_state_value,
                alarm_name_prefix=None,
                max_records=100,
            )
            for alarm in alarms:
                dims = alarm.get("dimensions", [])
                if any(d.get("name") == "InstanceId" and d.get("value") == instance_id for d in dims):
                    related_alarms.append(alarm)

        findings = []
        if health_checks.get("is_impaired"):
            findings.append("EC2 health checks report impaired status.")
        if ssm_status.get("managed_by_ssm") and str(ssm_status.get("ping_status", "")).lower() != "online":
            findings.append("SSM is configured but the managed instance ping status is not Online.")
        if include_alarms and related_alarms:
            findings.append(f"Found {len(related_alarms)} related alarm(s) in state {alarm_state_value}.")

        return {
            "success": True,
            "instance_id": instance_id,
            "minutes": minutes,
            "period_seconds": period_seconds,
            "instance_status": status,
            "health_checks": health_checks,
            "ssm_status": ssm_status,
            "metrics": metrics,
            "related_alarms": {
                "state_value": alarm_state_value,
                "count": len(related_alarms),
                "alarms": related_alarms,
            },
            "findings": findings,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


@mcp.tool()
async def aws_ssm_collect_host_diagnostics(
    instance_id: str,
    target_os: str = None,
    include_journal_errors: bool = True,
    include_network_snapshot: bool = True,
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 120,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Collect standard host diagnostics from an EC2 instance via SSM.

    This tool is intentionally generic and useful across many incidents:
    disk pressure, memory pressure, service instability, and networking issues.
    """
    try:
        commands = [
            "echo '=== TIME_UTC ==='",
            "date -u",
            "echo '=== UPTIME ==='",
            "uptime",
            "echo '=== DISK_USAGE ==='",
            "df -h",
            "echo '=== INODE_USAGE ==='",
            "df -i",
            "echo '=== TOP_DISK_PATHS_ROOT_MAXDEPTH2 ==='",
            "sudo du -x -h / --max-depth=2 2>/dev/null | sort -h | tail -n 30",
            "echo '=== TOP_LARGEST_FILES_COMMON_PATHS_BYTES ==='",
            "sudo find /var /home /opt /tmp /root /usr/local -xdev -type f -printf '%s\\t%p\\n' 2>/dev/null | sort -nr | head -n 40",
            "echo '=== MEMORY ==='",
            "free -m",
            "echo '=== LOAD_AND_TOP ==='",
            "top -b -n1 | head -n 25",
            "echo '=== FAILED_SYSTEMD_UNITS ==='",
            "systemctl --failed --no-pager || true",
            "echo '=== AI_AGENT_SERVICE_STATUS ==='",
            "sudo systemctl status ai-agent.service --no-pager -l || true",
            "echo '=== KERNEL_LOG_TAIL ==='",
            "sudo dmesg | tail -n 80 || true",
        ]

        if include_journal_errors:
            commands.extend(
                [
                    "echo '=== JOURNAL_ERRORS_LAST_20_MIN ==='",
                    "sudo journalctl -p err..alert --since '20 min ago' --no-pager | tail -n 120 || true",
                ]
            )

        if include_network_snapshot:
            commands.extend(
                [
                    "echo '=== NETWORK_SOCKETS ==='",
                    "ss -tulpn | head -n 80 || true",
                ]
            )

        result = ssm_manager.run_command(
            instance_ids=[instance_id],
            commands=commands,
            comment="Collect host diagnostics snapshot",
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

        return {
            "success": True,
            "instance_id": instance_id,
            "target_os": target_os,
            "result": result,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


@mcp.tool()
async def aws_ssm_safe_disk_cleanup(
    instance_id: str,
    target_os: str = None,
    dry_run: bool = True,
    journal_vacuum_days: int = 7,
    clean_package_cache: bool = True,
    clean_tmp: bool = True,
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 180,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Perform safe, bounded disk cleanup via SSM.

    By default, this runs in dry-run mode and only reports potential reclaim areas.
    """
    try:
        safe_days = max(1, min(int(journal_vacuum_days), 30))
        commands = [
            "echo '=== DISK_BEFORE ==='",
            "df -h",
            "echo '=== JOURNAL_DISK_USAGE ==='",
            "sudo journalctl --disk-usage || true",
            "echo '=== TOP_VAR_LOG_PATHS ==='",
            "sudo du -x -h /var/log --max-depth=2 2>/dev/null | sort -h | tail -n 30 || true",
            "echo '=== TMP_FILES_OLDER_THAN_3_DAYS_PREVIEW ==='",
            "sudo find /tmp /var/tmp -xdev -type f -mtime +3 -print | head -n 200 || true",
        ]

        mode = "dry_run"
        if not dry_run:
            mode = "apply"
            commands.append(f"sudo journalctl --vacuum-time={safe_days}d || true")
            if clean_package_cache:
                commands.append("sudo dnf clean all || true")
            if clean_tmp:
                commands.append("sudo find /tmp /var/tmp -xdev -type f -mtime +3 -delete || true")

            commands.extend(
                [
                    "echo '=== DISK_AFTER ==='",
                    "df -h",
                    "echo '=== JOURNAL_DISK_USAGE_AFTER ==='",
                    "sudo journalctl --disk-usage || true",
                ]
            )

        result = ssm_manager.run_command(
            instance_ids=[instance_id],
            commands=commands,
            comment=f"Safe disk cleanup ({mode})",
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

        return {
            "success": True,
            "instance_id": instance_id,
            "target_os": target_os,
            "mode": mode,
            "journal_vacuum_days": safe_days,
            "clean_package_cache": clean_package_cache,
            "clean_tmp": clean_tmp,
            "result": result,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


@mcp.tool()
async def aws_analyze_ec2_cost_optimization(
    instance_id: str,
    minutes: int = 180,
    period_seconds: int = 300,
    cpu_idle_threshold_percent: float = 15.0,
    cpu_hot_threshold_percent: float = 70.0,
    network_idle_threshold_bytes_per_second: float = 555.0,
    cpu_peak_cap_percent: float = 50.0,
    include_memory_disk_signals: bool = True,
    memory_pressure_threshold_percent: float = 75.0,
    disk_pressure_threshold_percent: float = 80.0,
    swap_pressure_threshold_percent: float = 50.0,
    allowed_families: list = None,
    include_compute_optimizer: bool = True,
) -> dict:
    """
    Analyze a single EC2 instance for cost optimization and rightsizing.

    Recommendation-only tool by design. It does not mutate infrastructure.
    """
    try:
        resolved_period_seconds, period_resolution = _resolve_cost_metric_period(
            minutes=minutes,
            requested_period_seconds=period_seconds,
        )

        utilization = cloudwatch_manager.analyze_ec2_rightsizing(
            instance_id=instance_id,
            minutes=minutes,
            period_seconds=resolved_period_seconds,
            cpu_idle_threshold_percent=cpu_idle_threshold_percent,
            cpu_hot_threshold_percent=cpu_hot_threshold_percent,
            network_idle_threshold_bytes_per_second=network_idle_threshold_bytes_per_second,
            cpu_peak_cap_percent=cpu_peak_cap_percent,
            include_extended_signals=include_memory_disk_signals,
            memory_pressure_threshold_percent=memory_pressure_threshold_percent,
            disk_pressure_threshold_percent=disk_pressure_threshold_percent,
            swap_pressure_threshold_percent=swap_pressure_threshold_percent,
        )

        rightsizing = ec2_manager.get_rightsizing_recommendation(
            instance_id=instance_id,
            utilization=utilization,
            allowed_families=allowed_families,
        )

        compute_optimizer = None
        if include_compute_optimizer:
            try:
                instance_arn = ec2_manager.get_instance_arn(instance_id)
                compute_optimizer = ec2_manager.get_compute_optimizer_recommendations(
                    instance_arns=[instance_arn]
                )
            except Exception as compute_opt_error:
                compute_optimizer = {"error": str(compute_opt_error)}

        return {
            "success": True,
            "instance_id": instance_id,
            "analysis_mode": "recommendation_only",
            "analysis_window": {
                "minutes": minutes,
                "requested_period_seconds": period_seconds,
                "resolved_period_seconds": resolved_period_seconds,
                "period_resolution": period_resolution,
                "estimated_datapoints_per_metric": _estimate_datapoints_per_metric(
                    minutes=minutes,
                    period_seconds=resolved_period_seconds,
                ),
            },
            "utilization_analysis": utilization,
            "rightsizing_recommendation": rightsizing,
            "compute_optimizer": compute_optimizer,
            "estimated_hourly_savings": rightsizing.get('estimated_hourly_savings', 0.0),
            "estimated_monthly_savings": rightsizing.get('estimated_monthly_savings', 0.0),
            "next_action_hint": (
                "If user explicitly requests execution, call aws_resize_ec2_instance "
                "with create_backup=true."
            ),
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


@mcp.tool()
async def aws_analyze_ec2_fleet_cost_optimization(
    minutes: int = 180,
    period_seconds: int = 300,
    cpu_idle_threshold_percent: float = 15.0,
    cpu_hot_threshold_percent: float = 70.0,
    network_idle_threshold_bytes_per_second: float = 555.0,
    cpu_peak_cap_percent: float = 50.0,
    include_memory_disk_signals: bool = True,
    memory_pressure_threshold_percent: float = 75.0,
    disk_pressure_threshold_percent: float = 80.0,
    swap_pressure_threshold_percent: float = 50.0,
    allowed_families: list = None,
    max_instances: int = 50,
) -> dict:
    """
    Analyze multiple running EC2 instances and produce recommendation-only rightsizing output.
    """
    try:
        resolved_period_seconds, period_resolution = _resolve_cost_metric_period(
            minutes=minutes,
            requested_period_seconds=period_seconds,
        )

        instances = ec2_manager.list_instances()
        running_instances = [inst for inst in instances if inst.get('state') == 'running'][:max_instances]

        analyses = []
        total_hourly_savings = 0.0
        total_monthly_savings = 0.0

        for inst in running_instances:
            instance_id = inst.get('id')
            utilization = cloudwatch_manager.analyze_ec2_rightsizing(
                instance_id=instance_id,
                minutes=minutes,
                period_seconds=resolved_period_seconds,
                cpu_idle_threshold_percent=cpu_idle_threshold_percent,
                cpu_hot_threshold_percent=cpu_hot_threshold_percent,
                network_idle_threshold_bytes_per_second=network_idle_threshold_bytes_per_second,
                cpu_peak_cap_percent=cpu_peak_cap_percent,
                include_extended_signals=include_memory_disk_signals,
                memory_pressure_threshold_percent=memory_pressure_threshold_percent,
                disk_pressure_threshold_percent=disk_pressure_threshold_percent,
                swap_pressure_threshold_percent=swap_pressure_threshold_percent,
            )
            rightsizing = ec2_manager.get_rightsizing_recommendation(
                instance_id=instance_id,
                utilization=utilization,
                allowed_families=allowed_families,
            )

            hourly_savings = float(rightsizing.get('estimated_hourly_savings', 0.0) or 0.0)
            monthly_savings = float(rightsizing.get('estimated_monthly_savings', 0.0) or 0.0)
            total_hourly_savings += hourly_savings
            total_monthly_savings += monthly_savings

            analyses.append(
                {
                    'instance_id': instance_id,
                    'instance_name': inst.get('name'),
                    'current_instance_type': inst.get('type'),
                    'utilization_analysis': utilization,
                    'rightsizing_recommendation': rightsizing,
                }
            )

        return {
            'success': True,
            'analysis_mode': 'recommendation_only',
            'analysis_window': {
                'minutes': minutes,
                'requested_period_seconds': period_seconds,
                'resolved_period_seconds': resolved_period_seconds,
                'period_resolution': period_resolution,
                'estimated_datapoints_per_metric': _estimate_datapoints_per_metric(
                    minutes=minutes,
                    period_seconds=resolved_period_seconds,
                ),
            },
            'instance_count': len(analyses),
            'instances': analyses,
            'estimated_hourly_savings': total_hourly_savings,
            'estimated_monthly_savings': total_monthly_savings,
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
        }


@mcp.tool()
async def aws_get_compute_optimizer_recommendations(instance_arns: list = None) -> dict:
    """
    Fetch EC2 rightsizing recommendations from AWS Compute Optimizer.

    This is recommendation-only and does not change infrastructure.
    """
    try:
        result = ec2_manager.get_compute_optimizer_recommendations(instance_arns=instance_arns)
        return {
            "success": True,
            "result": result,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


@mcp.tool()
async def aws_resize_ec2_instance(
    instance_id: str,
    target_instance_type: str = None,
    min_cpu: int = None,
    min_ram_gb: int = None,
    allowed_families: list = None,
    create_backup: bool = True,
    backup_name_prefix: str = 'ai-agent-resize-backup',
    dry_run: bool = True,
    no_reboot_backup: bool = True,
    prefer_downsize_when_idle: bool = True,
    ensure_service_continuity: bool = True,
    strict_service_continuity: bool = False,
    service_recovery_timeout_seconds: int = 300,
) -> dict:
    """
    Resize an EC2 instance safely with compatibility checks and optional AMI backup.

    If target_instance_type is omitted, uses attribute-based cheapest-instance selection.
    """
    try:
        selection = None
        resolved_target_type = target_instance_type

        if not resolved_target_type:
            selection = ec2_manager.get_cheapest_compatible_instance_type(
                instance_id=instance_id,
                min_cpu=min_cpu,
                min_ram_gb=min_ram_gb,
                allowed_families=allowed_families,
                prefer_downsize_when_idle=prefer_downsize_when_idle,
            )
            resolved_target_type = selection.get('recommended_instance_type')

        if dry_run:
            compatibility = None
            try:
                current = ec2_manager.get_instance_status(instance_id)
                compatibility = ec2_manager.get_instance_type_compatibility(
                    current.get('type'),
                    resolved_target_type,
                )
            except Exception:
                pass

            dry_run_payload = {
                "success": True,
                "mode": "dry_run",
                "instance_id": instance_id,
                "target_instance_type": resolved_target_type,
                "selection": selection,
                "compatibility": compatibility,
                "backup_will_be_created": create_backup,
                "service_continuity_enabled": ensure_service_continuity,
                "strict_service_continuity": strict_service_continuity,
                "message": "Dry-run only. No infrastructure changes were applied.",
            }
            if selection:
                dry_run_payload["estimated_hourly_savings"] = selection.get('estimated_hourly_savings', 0.0)
                dry_run_payload["estimated_monthly_savings"] = selection.get('estimated_monthly_savings', 0.0)
            return dry_run_payload

        result = ec2_manager.resize_instance_type(
            instance_id=instance_id,
            target_instance_type=resolved_target_type,
            create_backup=create_backup,
            backup_name_prefix=backup_name_prefix,
            no_reboot_backup=no_reboot_backup,
            ensure_previously_running_services=ensure_service_continuity,
            strict_service_recovery=strict_service_continuity,
            service_recovery_timeout_seconds=service_recovery_timeout_seconds,
        )

        return {
            "success": True,
            "mode": "applied",
            "result": result,
            "estimated_hourly_savings": result.get('estimated_hourly_savings', 0.0),
            "estimated_monthly_savings": result.get('estimated_monthly_savings', 0.0),
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


@mcp.tool()
async def aws_apply_ec2_rightsizing(
    instance_id: str,
    min_cpu: int = None,
    min_ram_gb: int = None,
    allowed_families: list = None,
    create_backup: bool = True,
    backup_name_prefix: str = 'ai-agent-resize-backup',
    no_reboot_backup: bool = True,
    prefer_downsize_when_idle: bool = True,
    minutes: int = 180,
    period_seconds: int = 300,
    network_idle_threshold_bytes_per_second: float = 555.0,
    cpu_peak_cap_percent: float = 50.0,
    ensure_service_continuity: bool = True,
    strict_service_continuity: bool = False,
    service_recovery_timeout_seconds: int = 300,
) -> dict:
    """
    Smart apply wrapper:
    - Re-analyzes utilization
    - Chooses most suitable compatible target with cost savings
    - Applies resize only when there is a positive savings opportunity
    """
    try:
        utilization = cloudwatch_manager.analyze_ec2_rightsizing(
            instance_id=instance_id,
            minutes=minutes,
            period_seconds=period_seconds,
            network_idle_threshold_bytes_per_second=network_idle_threshold_bytes_per_second,
            cpu_peak_cap_percent=cpu_peak_cap_percent,
        )

        current = ec2_manager.get_instance_status(instance_id)
        current_type = current.get('type')

        selection = ec2_manager.get_rightsizing_recommendation(
            instance_id=instance_id,
            utilization=utilization,
            allowed_families=allowed_families,
        )
        target_type = selection.get('recommended_instance_type')
        hourly_savings = float(selection.get('estimated_hourly_savings', 0.0) or 0.0)

        if not target_type or target_type == current_type:
            return {
                'success': True,
                'mode': 'no_change',
                'message': 'No better compatible target instance type found.',
                'instance_id': instance_id,
                'current_instance_type': current_type,
                'rightsizing_recommendation': selection,
                'utilization_analysis': utilization,
            }

        if hourly_savings <= 0:
            return {
                'success': True,
                'mode': 'no_change',
                'message': 'Recommended target does not provide positive savings. Resize skipped.',
                'instance_id': instance_id,
                'current_instance_type': current_type,
                'target_instance_type': target_type,
                'rightsizing_recommendation': selection,
                'utilization_analysis': utilization,
                'estimated_hourly_savings': hourly_savings,
                'estimated_monthly_savings': float(selection.get('estimated_monthly_savings', 0.0) or 0.0),
            }

        result = ec2_manager.resize_instance_type(
            instance_id=instance_id,
            target_instance_type=target_type,
            create_backup=create_backup,
            backup_name_prefix=backup_name_prefix,
            no_reboot_backup=no_reboot_backup,
            ensure_previously_running_services=ensure_service_continuity,
            strict_service_recovery=strict_service_continuity,
            service_recovery_timeout_seconds=service_recovery_timeout_seconds,
        )

        return {
            'success': True,
            'mode': 'applied',
            'result': result,
            'rightsizing_recommendation': selection,
            'utilization_analysis': utilization,
            'estimated_hourly_savings': result.get('estimated_hourly_savings', 0.0),
            'estimated_monthly_savings': result.get('estimated_monthly_savings', 0.0),
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
        }


@mcp.tool()
async def aws_detect_idle_cost_leaks(
    minutes: int = 180,
    period_seconds: int = 300,
    cpu_idle_threshold_percent: float = 10.0,
    network_idle_threshold_bytes_per_second: float = 555.0,
    cpu_peak_cap_percent: float = 50.0,
    stale_alarm_days: int = 30,
    include_fleet_cost_analysis: bool = True,
    allowed_families: list = None,
    max_instances: int = 50,
) -> dict:
    """
    Detect lightweight cost leaks in small environments and provide
    a system-wide recommendation-only fleet cost analysis:
    - idle running EC2 instances
    - potentially stale CloudWatch alarms
    - fleet rightsizing opportunities and estimated savings
    """
    try:
        instances = ec2_manager.list_instances()
        running_instances = [instance for instance in instances if instance.get('state') == 'running']

        idle_instances = []
        utilization_by_instance_id = {}
        for instance in running_instances:
            instance_id = instance.get('id')
            analysis = cloudwatch_manager.analyze_ec2_rightsizing(
                instance_id=instance_id,
                minutes=minutes,
                period_seconds=period_seconds,
                cpu_idle_threshold_percent=cpu_idle_threshold_percent,
                network_idle_threshold_bytes_per_second=network_idle_threshold_bytes_per_second,
                cpu_peak_cap_percent=cpu_peak_cap_percent,
            )
            utilization_by_instance_id[instance_id] = analysis
            if analysis.get('recommendation') == 'downsize':
                idle_instances.append(
                    {
                        'instance_id': instance_id,
                        'instance_name': instance.get('name'),
                        'instance_type': instance.get('type'),
                        'analysis': analysis,
                    }
                )

        warnings = []
        stale_alarm_error = None

        try:
            alarms = cloudwatch_manager.list_alarms(max_records=100)
        except Exception as alarm_error:
            alarms = []
            stale_alarm_error = str(alarm_error)
            warnings.append(
                "CloudWatch alarm stale analysis was skipped due to an alarm API error. "
                "Idle EC2 analysis is still valid."
            )

        stale_alarms = []
        now = datetime.now(timezone.utc)
        for alarm in alarms:
            ts_raw = alarm.get('state_updated_timestamp')
            if not ts_raw:
                continue
            try:
                state_updated = datetime.fromisoformat(ts_raw.replace('Z', '+00:00'))
            except Exception:
                continue

            age_days = (now - state_updated).days
            if age_days >= stale_alarm_days:
                stale_alarms.append(
                    {
                        'alarm_name': alarm.get('alarm_name'),
                        'state_value': alarm.get('state_value'),
                        'state_updated_timestamp': ts_raw,
                        'age_days': age_days,
                    }
                )

        fleet_cost_analysis = None
        if include_fleet_cost_analysis:
            analyzed_instances = running_instances[:max(1, int(max_instances))]
            analyses = []
            total_hourly_savings = 0.0
            total_monthly_savings = 0.0
            actionable_recommendations_count = 0
            low_confidence_instances = []

            for instance in analyzed_instances:
                instance_id = instance.get('id')
                utilization = utilization_by_instance_id.get(instance_id)
                if not utilization:
                    utilization = cloudwatch_manager.analyze_ec2_rightsizing(
                        instance_id=instance_id,
                        minutes=minutes,
                        period_seconds=period_seconds,
                        cpu_idle_threshold_percent=cpu_idle_threshold_percent,
                        network_idle_threshold_bytes_per_second=network_idle_threshold_bytes_per_second,
                        cpu_peak_cap_percent=cpu_peak_cap_percent,
                    )

                rightsizing = ec2_manager.get_rightsizing_recommendation(
                    instance_id=instance_id,
                    utilization=utilization,
                    allowed_families=allowed_families,
                )

                hourly_savings = float(rightsizing.get('estimated_hourly_savings', 0.0) or 0.0)
                monthly_savings = float(rightsizing.get('estimated_monthly_savings', 0.0) or 0.0)
                total_hourly_savings += hourly_savings
                total_monthly_savings += monthly_savings

                current_type = instance.get('type')
                target_type = rightsizing.get('recommended_instance_type')
                if target_type and target_type != current_type and hourly_savings > 0:
                    actionable_recommendations_count += 1

                datapoint_count = utilization.get('metrics_summary', {}).get('datapoint_count', {})
                min_points = min(
                    int(datapoint_count.get('cpu', 0) or 0),
                    int(datapoint_count.get('network_in', 0) or 0),
                    int(datapoint_count.get('network_out', 0) or 0),
                )
                if min_points < 3:
                    low_confidence_instances.append(
                        {
                            'instance_id': instance_id,
                            'instance_name': instance.get('name'),
                            'datapoint_count': datapoint_count,
                        }
                    )

                analyses.append(
                    {
                        'instance_id': instance_id,
                        'instance_name': instance.get('name'),
                        'current_instance_type': current_type,
                        'utilization_analysis': utilization,
                        'rightsizing_recommendation': rightsizing,
                    }
                )

            if low_confidence_instances:
                warnings.append(
                    'Some instances have very few datapoints (<3). Rightsizing confidence is low for those instances.'
                )

            fleet_cost_analysis = {
                'analysis_mode': 'recommendation_only',
                'scope': 'system_running_ec2_instances',
                'instance_count': len(analyses),
                'instances': analyses,
                'estimated_hourly_savings': total_hourly_savings,
                'estimated_monthly_savings': total_monthly_savings,
                'actionable_recommendations_count': actionable_recommendations_count,
                'low_confidence_instances': low_confidence_instances,
            }

        final_recommendation = (
            'Review idle instances and stale alarms first. Resize/stop only after explicit user approval.'
        )
        if fleet_cost_analysis:
            if fleet_cost_analysis.get('actionable_recommendations_count', 0) > 0:
                final_recommendation = (
                    'System-wide analysis found cost optimization opportunities. '
                    'Review fleet_cost_analysis.instances and apply changes only with explicit user approval.'
                )
            else:
                final_recommendation = (
                    'No actionable rightsizing changes with positive savings were found in the analyzed fleet. '
                    'Continue periodic monitoring.'
                )

        return {
            'success': True,
            'window_minutes': minutes,
            'running_instances_count': len(running_instances),
            'idle_instances_count': len(idle_instances),
            'idle_instances': idle_instances,
            'stale_alarms_count': len(stale_alarms),
            'stale_alarms': stale_alarms,
            'stale_alarm_error': stale_alarm_error,
            'warnings': warnings,
            'fleet_cost_analysis': fleet_cost_analysis,
            'recommendation': final_recommendation,
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
        }


@mcp.tool()
async def aws_ssm_run_command(
    instance_ids: list,
    commands: list,
    comment: str = None,
    timeout_seconds: int = 600,
    working_directory: str = None,
    target_os: str = None,
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Execute shell commands on EC2 instances via AWS SSM Run Command.

    Args:
        instance_ids: List of EC2 instance IDs
        commands: List of shell commands to execute in order
        comment: Optional command description
        timeout_seconds: Command timeout in seconds
        working_directory: Optional working directory on target host
        target_os: Optional OS family hint for policy checks (amazon-linux, ubuntu, rhel, etc.)
        wait_for_completion: When True, waits and returns stdout/stderr inline
        completion_timeout_seconds: Maximum wait time for final command status
        poll_interval_seconds: Poll interval while waiting for completion

    Returns:
        Dictionary with command metadata and optional invocation outputs
    """
    try:
        result = ssm_manager.run_command(
            instance_ids=instance_ids,
            commands=commands,
            comment=comment,
            timeout_seconds=timeout_seconds,
            working_directory=working_directory,
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return {
            "success": True,
            "result": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_ssm_get_command_output(command_id: str, instance_id: str) -> dict:
    """
    Get output for a previously issued SSM Run Command invocation.

    Args:
        command_id: SSM command ID
        instance_id: EC2 instance ID where command executed

    Returns:
        Dictionary with status, stdout, and stderr
    """
    try:
        output = ssm_manager.get_command_output(command_id=command_id, instance_id=instance_id)
        return {
            "success": True,
            "output": output
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_ssm_start_service(
    instance_ids: list,
    service_name: str,
    target_os: str = None,
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Start a systemd service on one or more EC2 instances via SSM.

    Args:
        instance_ids: List of EC2 instance IDs
        service_name: Systemd service unit name (e.g., ai-agent.service)
        target_os: Optional OS family hint for policy checks (amazon-linux, ubuntu, rhel, etc.)
        wait_for_completion: When True, waits and returns stdout/stderr inline
        completion_timeout_seconds: Maximum wait time for final command status
        poll_interval_seconds: Poll interval while waiting for completion

    Returns:
        Dictionary with command metadata and optional invocation outputs
    """
    try:
        result = ssm_manager.start_service(
            instance_ids=instance_ids,
            service_name=service_name,
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return {
            "success": True,
            "result": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_ssm_stop_service(
    instance_ids: list,
    service_name: str,
    target_os: str = None,
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Stop a systemd service on one or more EC2 instances via SSM.

    Args:
        instance_ids: List of EC2 instance IDs
        service_name: Systemd service unit name
        target_os: Optional OS family hint for policy checks (amazon-linux, ubuntu, rhel, etc.)
        wait_for_completion: When True, waits and returns stdout/stderr inline
        completion_timeout_seconds: Maximum wait time for final command status
        poll_interval_seconds: Poll interval while waiting for completion

    Returns:
        Dictionary with command metadata and optional invocation outputs
    """
    try:
        result = ssm_manager.stop_service(
            instance_ids=instance_ids,
            service_name=service_name,
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return {
            "success": True,
            "result": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_ssm_restart_service(
    instance_ids: list,
    service_name: str,
    target_os: str = None,
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Restart a systemd service on one or more EC2 instances via SSM.

    Args:
        instance_ids: List of EC2 instance IDs
        service_name: Systemd service unit name
        target_os: Optional OS family hint for policy checks (amazon-linux, ubuntu, rhel, etc.)
        wait_for_completion: When True, waits and returns stdout/stderr inline
        completion_timeout_seconds: Maximum wait time for final command status
        poll_interval_seconds: Poll interval while waiting for completion

    Returns:
        Dictionary with command metadata and optional invocation outputs
    """
    try:
        result = ssm_manager.restart_service(
            instance_ids=instance_ids,
            service_name=service_name,
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return {
            "success": True,
            "result": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_ssm_get_service_status(
    instance_ids: list,
    service_name: str,
    target_os: str = None,
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Get active/inactive status of a systemd service via SSM.

    Args:
        instance_ids: List of EC2 instance IDs
        service_name: Systemd service unit name
        target_os: Optional OS family hint for policy checks (amazon-linux, ubuntu, rhel, etc.)
        wait_for_completion: When True, waits and returns stdout/stderr inline
        completion_timeout_seconds: Maximum wait time for final command status
        poll_interval_seconds: Poll interval while waiting for completion

    Returns:
        Dictionary with command metadata and optional invocation outputs
    """
    try:
        result = ssm_manager.get_service_status(
            instance_ids=instance_ids,
            service_name=service_name,
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return {
            "success": True,
            "result": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_ssm_list_running_services(
    instance_id: str,
    target_os: str = None,
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    List running systemd services on an EC2 instance via SSM.

    Args:
        instance_id: EC2 instance ID
        target_os: Optional OS family hint for policy checks (amazon-linux, ubuntu, rhel, etc.)
        wait_for_completion: When True, waits and returns stdout/stderr inline
        completion_timeout_seconds: Maximum wait time for final command status
        poll_interval_seconds: Poll interval while waiting for completion

    Returns:
        Dictionary with command metadata and optional invocation outputs
    """
    try:
        result = ssm_manager.list_running_services(
            instance_id=instance_id,
            wait_for_completion=wait_for_completion,
            completion_timeout_seconds=completion_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return {
            "success": True,
            "result": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_get_xray_trace_summaries(
    minutes: int = 15,
    max_results: int = 20,
    filter_expression: str = None,
    service_names: list = None,
    exclude_loopback_only: bool = False,
) -> dict:
    """
    Get recent AWS X-Ray trace summaries.

    Args:
        minutes: Lookback window in minutes
        max_results: Maximum trace summaries to return
        filter_expression: Optional X-Ray filter expression
        service_names: Optional list of service names to include (translated to service("name") filter)
        exclude_loopback_only: Exclude traces that only contain localhost/127.0.0.1 service names

    Returns:
        Dictionary with trace summary list
    """
    try:
        merged_filter = filter_expression
        cleaned = []
        if service_names:
            cleaned = [str(name).strip() for name in service_names if str(name).strip()]
            if cleaned:
                service_filter = " OR ".join(
                    f'service("{name.replace("\\", "\\\\").replace("\"", "\\\"")}")'
                    for name in cleaned
                )
                merged_filter = f"({merged_filter}) AND ({service_filter})" if merged_filter else service_filter

        result = xray_manager.get_trace_summaries(
            minutes=minutes,
            max_results=max_results,
            filter_expression=merged_filter,
            exclude_loopback_only=exclude_loopback_only,
        )

        if service_names:
            result["requested_service_names"] = service_names
        result["applied_filter_expression"] = merged_filter

        warnings = result.setdefault("analysis", {}).setdefault("warnings", []) if isinstance(result.get("analysis"), dict) else []

        likely_instance_tokens = [
            name for name in cleaned if re.match(r"^i-[0-9a-f]{8,}$", name.lower())
        ]
        if likely_instance_tokens:
            warnings.append(
                "Some service_names look like EC2 instance IDs. X-Ray service filters require service names (for example: real-api, real-worker), not instance IDs."
            )

        if cleaned and result.get("count", 0) == 0:
            discovery_result = xray_manager.get_trace_summaries(
                minutes=minutes,
                max_results=max(max_results, 20),
                filter_expression=filter_expression,
                exclude_loopback_only=True,
            )

            suggested_services = [
                item.get("name")
                for item in discovery_result.get("analysis", {}).get("top_service_names", [])
                if item.get("name")
            ]

            result["fallback_discovery"] = {
                "count": discovery_result.get("count", 0),
                "suggested_service_names": suggested_services,
                "applied_filter_expression": filter_expression,
                "exclude_loopback_only": True,
            }

            if suggested_services:
                warnings.append(
                    "No traces matched the requested service_names. Try one of fallback_discovery.suggested_service_names."
                )
            else:
                warnings.append(
                    "No traces matched requested service_names and fallback discovery also found no non-loopback traces."
                )

        return {
            "success": True,
            "result": result,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


@mcp.tool()
async def aws_get_xray_trace_details(trace_ids: list) -> dict:
    """
    Get detailed AWS X-Ray traces by trace IDs.

    Args:
        trace_ids: List of X-Ray trace IDs

    Returns:
        Dictionary with trace documents and segment data
    """
    try:
        result = xray_manager.batch_get_traces(trace_ids=trace_ids)
        return {
            "success": True,
            "result": result,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


@mcp.tool()
async def aws_get_xray_service_graph(minutes: int = 15) -> dict:
    """
    Get AWS X-Ray service graph for recent traffic.

    Args:
        minutes: Lookback window in minutes

    Returns:
        Dictionary containing X-Ray service graph nodes/edges
    """
    try:
        result = xray_manager.get_service_graph(minutes=minutes)
        return {
            "success": True,
            "result": result,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }
    

# ========================================
# CLOUDWATCH OBSERVABILITY TOOLS
# ========================================

@mcp.tool()
async def aws_get_ec2_metrics(
    instance_id: str = None,
    minutes: int = 15,
    period_seconds: int = 60,
    include_agent_metrics: bool = True,
    agent_namespace: str = "CWAgent",
) -> dict:
    """
    Get recent EC2 CloudWatch metrics for an instance.

    Args:
        instance_id: EC2 instance ID (optional if CW_TEST_INSTANCE_ID is set)
        minutes: Lookback window in minutes (default: 15)
        period_seconds: Metric period in seconds (default: 60)
        include_agent_metrics: Include CloudWatch Agent metrics (disk, mem, swap, io, netstat)
        agent_namespace: Namespace for CloudWatch Agent metrics

    Returns:
        Dictionary with EC2 metrics and optional CloudWatch Agent metrics
    """
    try:
        resolved_instance_id = instance_id or default_instance_id
        if not resolved_instance_id:
            return {
                "success": False,
                "error": "Missing instance_id. Provide one or set CW_TEST_INSTANCE_ID in environment."
            }

        metrics_payload = cloudwatch_manager.get_ec2_metrics(
            instance_id=resolved_instance_id,
            minutes=minutes,
            period_seconds=period_seconds,
            include_agent_metrics=include_agent_metrics,
            agent_namespace=agent_namespace,
        )

        return {
            "success": True,
            "metrics": metrics_payload
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_get_ec2_metrics_scoped(
    instance_id: str = None,
    scope: str = "all",
    minutes: int = 15,
    period_seconds: int = 60,
    include_agent_metrics: bool = True,
    agent_namespace: str = "CWAgent",
) -> dict:
    """
    Get a scoped subset of EC2 metrics instead of returning the full metrics bundle.

    Supported scope values:
    - all
    - cpu
    - network
    - status
    - disk
    - memory
    """
    try:
        resolved_instance_id = instance_id or default_instance_id
        if not resolved_instance_id:
            return {
                "success": False,
                "error": "Missing instance_id. Provide one or set CW_TEST_INSTANCE_ID in environment."
            }

        metrics_payload = cloudwatch_manager.get_ec2_metrics(
            instance_id=resolved_instance_id,
            minutes=minutes,
            period_seconds=period_seconds,
            include_agent_metrics=include_agent_metrics,
            agent_namespace=agent_namespace,
        )

        scoped_view = _build_scoped_metrics_view(metrics_payload, scope)

        return {
            "success": True,
            "instance_id": resolved_instance_id,
            "minutes": minutes,
            "period_seconds": period_seconds,
            "scope": scoped_view.get("scope"),
            "metrics": scoped_view.get("metrics", {}),
            "agent_metrics": scoped_view.get("agent_metrics", {}),
            "availability_summary": scoped_view.get("availability_summary", {}),
            "interpretation_guardrails": metrics_payload.get("interpretation_guardrails", []),
            "warnings": metrics_payload.get("warnings", []),
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_list_log_groups(prefix: str = None, limit: int = 50) -> dict:
    """
    List CloudWatch log groups.

    Args:
        prefix: Optional log group name prefix filter
        limit: Maximum results (default: 50)

    Returns:
        Dictionary with log groups
    """
    try:
        groups = cloudwatch_manager.list_log_groups(prefix=prefix, limit=limit)
        return {
            "success": True,
            "count": len(groups),
            "log_groups": groups
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_list_log_streams(log_group_name: str = None, limit: int = 25, descending: bool = True) -> dict:
    """
    List log streams in a CloudWatch log group.

    Args:
        log_group_name: CloudWatch log group name (optional if CW_TEST_LOG_GROUP is set)
        limit: Maximum streams to return (default: 25)
        descending: Newest streams first when True

    Returns:
        Dictionary with log streams
    """
    try:
        resolved_log_group = log_group_name or default_log_group
        if not resolved_log_group:
            return {
                "success": False,
                "error": "Missing log_group_name. Provide one or set CW_TEST_LOG_GROUP in environment."
            }

        streams = cloudwatch_manager.list_log_streams(
            log_group_name=resolved_log_group,
            limit=limit,
            descending=descending,
        )
        return {
            "success": True,
            "log_group_name": resolved_log_group,
            "count": len(streams),
            "log_streams": streams
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_get_log_events(
    log_group_name: str = None,
    log_stream_name: str = None,
    limit: int = 100,
    start_from_head: bool = False,
) -> dict:
    """
    Get events from a specific CloudWatch log stream.

    Args:
        log_group_name: CloudWatch log group name (optional if CW_TEST_LOG_GROUP is set)
        log_stream_name: Log stream name (optional; newest stream is used if omitted)
        limit: Max events (default: 100)
        start_from_head: If True, fetch oldest first

    Returns:
        Dictionary with log events
    """
    try:
        resolved_log_group = log_group_name or default_log_group
        if not resolved_log_group:
            return {
                "success": False,
                "error": "Missing log_group_name. Provide one or set CW_TEST_LOG_GROUP in environment."
            }

        resolved_log_stream = log_stream_name
        if not resolved_log_stream:
            streams = cloudwatch_manager.list_log_streams(
                log_group_name=resolved_log_group,
                limit=1,
                descending=True,
            )
            if not streams:
                return {
                    "success": False,
                    "error": f"No log streams found in log group: {resolved_log_group}"
                }
            resolved_log_stream = streams[0].get("log_stream_name")

        result = cloudwatch_manager.get_log_events(
            log_group_name=resolved_log_group,
            log_stream_name=resolved_log_stream,
            limit=limit,
            start_from_head=start_from_head,
        )
        return {
            "success": True,
            **result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_filter_logs(
    log_group_name: str = None,
    filter_pattern: str = "",
    minutes: int = 15,
    limit: int = 100,
) -> dict:
    """
    Filter CloudWatch log events by pattern over a recent time window.

    Args:
        log_group_name: CloudWatch log group name (optional if CW_TEST_LOG_GROUP is set)
        filter_pattern: CloudWatch filter pattern (empty means no filter)
        minutes: Lookback in minutes (default: 15)
        limit: Max events (default: 100)

    Returns:
        Dictionary with filtered log events
    """
    try:
        resolved_log_group = log_group_name or default_log_group
        if not resolved_log_group:
            return {
                "success": False,
                "error": "Missing log_group_name. Provide one or set CW_TEST_LOG_GROUP in environment."
            }

        result = cloudwatch_manager.filter_logs(
            log_group_name=resolved_log_group,
            filter_pattern=filter_pattern,
            minutes=minutes,
            limit=limit,
        )
        return {
            "success": True,
            **result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_list_alarms(
    state_value: str = None,
    alarm_name_prefix: str = None,
    max_records: int = 100,
) -> dict:
    """
    List CloudWatch alarms with optional filters.

    Args:
        state_value: Optional state filter (OK, ALARM, INSUFFICIENT_DATA)
        alarm_name_prefix: Optional alarm name prefix filter
        max_records: Maximum records to return

    Returns:
        Dictionary with alarms
    """
    try:
        alarms = cloudwatch_manager.list_alarms(
            state_value=state_value,
            alarm_name_prefix=alarm_name_prefix,
            max_records=max_records,
        )
        return {
            "success": True,
            "count": len(alarms),
            "alarms": alarms
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_list_ec2_alarms(
    instance_id: str = None,
    instance_name: str = None,
    state_value: str = None,
    max_records: int = 100,
) -> dict:
    """
    List CloudWatch alarms related to a specific EC2 instance.

    This tool resolves alarms by CloudWatch metric dimensions (InstanceId),
    which is more reliable than filtering by alarm-name prefix.

    Args:
        instance_id: EC2 instance ID (optional if instance_name is provided)
        instance_name: EC2 Name tag (optional if instance_id is provided)
        state_value: Optional state filter (OK, ALARM, INSUFFICIENT_DATA)
        max_records: Maximum records to retrieve before filtering

    Returns:
        Dictionary with resolved instance and matching alarms
    """
    try:
        resolved_instance_id = instance_id or default_instance_id
        resolved_instance_name = instance_name

        # Resolve instance ID by name if needed
        if not resolved_instance_id and instance_name:
            instances = ec2_manager.list_instances()
            target = next(
                (inst for inst in instances if inst.get("name", "").lower() == instance_name.lower()),
                None,
            )
            if not target:
                return {
                    "success": False,
                    "error": f"EC2 instance not found by name: {instance_name}"
                }
            resolved_instance_id = target.get("id")
            resolved_instance_name = target.get("name")

        if not resolved_instance_id:
            return {
                "success": False,
                "error": "Provide either instance_id or instance_name"
            }

        alarms = cloudwatch_manager.list_alarms(
            state_value=state_value,
            alarm_name_prefix=None,
            max_records=max_records,
        )

        # Match alarms by metric dimension InstanceId
        matched_alarms = []
        for alarm in alarms:
            dims = alarm.get("dimensions", [])
            if any(d.get("name") == "InstanceId" and d.get("value") == resolved_instance_id for d in dims):
                matched_alarms.append(alarm)

        return {
            "success": True,
            "instance_id": resolved_instance_id,
            "instance_name": resolved_instance_name,
            "count": len(matched_alarms),
            "alarms": matched_alarms,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_poll_alarm_notifications(
    queue_url: str = None,
    max_messages: int = 5,
    wait_time_seconds: int = 5,
    visibility_timeout: int = 60,
    delete_on_read: bool = False,
) -> dict:
    """
    Poll SQS for CloudWatch alarm notifications delivered via SNS.

    Args:
        queue_url: SQS queue URL (optional if ALARM_SQS_QUEUE_URL is set)
        max_messages: Number of messages to retrieve (1-10)
        wait_time_seconds: Long-poll wait duration in seconds
        visibility_timeout: Visibility timeout in seconds for received messages
        delete_on_read: Delete messages after reading when True

    Returns:
        Dictionary with normalized alarm notifications
    """
    try:
        resolved_queue_url = queue_url or default_alarm_queue_url
        if not resolved_queue_url:
            return {
                "success": False,
                "error": "Missing queue_url. Provide one or set ALARM_SQS_QUEUE_URL in environment."
            }

        result = cloudwatch_manager.poll_alarm_notifications(
            queue_url=resolved_queue_url,
            max_messages=max_messages,
            wait_time_seconds=wait_time_seconds,
            visibility_timeout=visibility_timeout,
            delete_on_read=delete_on_read,
        )
        return {
            "success": True,
            **result,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_alarm_notification(queue_url: str = None, receipt_handle: str = None) -> dict:
    """
    Acknowledge an SQS alarm notification by deleting it with the receipt handle.

    Args:
        queue_url: SQS queue URL (optional if ALARM_SQS_QUEUE_URL is set)
        receipt_handle: SQS receipt handle from aws_poll_alarm_notifications

    Returns:
        Dictionary with deletion status
    """
    try:
        resolved_queue_url = queue_url or default_alarm_queue_url
        if not resolved_queue_url:
            return {
                "success": False,
                "error": "Missing queue_url. Provide one or set ALARM_SQS_QUEUE_URL in environment."
            }
        if not receipt_handle:
            return {
                "success": False,
                "error": "Missing receipt_handle from aws_poll_alarm_notifications response."
            }

        result = cloudwatch_manager.delete_alarm_notification(
            queue_url=resolved_queue_url,
            receipt_handle=receipt_handle,
        )
        return {
            "success": True,
            **result,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_metric_alarm(
    alarm_name: str,
    metric_name: str,
    namespace: str,
    threshold: float,
    comparison_operator: str,
    evaluation_periods: int,
    period: int,
    statistic: str = "Average",
    dimensions: list = None,
    alarm_actions: list = None,
    ok_actions: list = None,
    treat_missing_data: str = "missing",
    alarm_description: str = None,
) -> dict:
    """
    Create or update a CloudWatch metric alarm.

    Args:
        alarm_name: Alarm name
        metric_name: Metric name
        namespace: Metric namespace (e.g., AWS/EC2)
        threshold: Threshold value
        comparison_operator: Comparison operator
        evaluation_periods: Number of periods to evaluate
        period: Period length in seconds
        statistic: Statistic to evaluate (default: Average)
        dimensions: Optional dimensions list [{"Name": "InstanceId", "Value": "i-..."}]
        alarm_actions: Optional alarm action ARNs (e.g., SNS topic ARN)
        ok_actions: Optional OK action ARNs
        treat_missing_data: missing | breaching | notBreaching | ignore
        alarm_description: Optional description

    Returns:
        Dictionary with alarm creation status
    """
    try:
        if not alarm_actions and default_sns_arn:
            alarm_actions = [default_sns_arn]
        if not ok_actions and default_sns_arn:
            ok_actions = [default_sns_arn]

        result = cloudwatch_manager.create_metric_alarm(
            alarm_name=alarm_name,
            metric_name=metric_name,
            namespace=namespace,
            threshold=threshold,
            comparison_operator=comparison_operator,
            evaluation_periods=evaluation_periods,
            period=period,
            statistic=statistic,
            dimensions=dimensions,
            alarm_actions=alarm_actions,
            ok_actions=ok_actions,
            treat_missing_data=treat_missing_data,
            alarm_description=alarm_description,
        )
        return {
            "success": True,
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_get_dashboard(dashboard_name: str = None) -> dict:
    """
    Get a CloudWatch dashboard body by name.

    Args:
        dashboard_name: Dashboard name (optional if CW_DASHBOARD_NAME is set)

    Returns:
        Dashboard definition and metadata
    """
    try:
        resolved_dashboard_name = dashboard_name or default_dashboard_name
        if not resolved_dashboard_name:
            return {
                "success": False,
                "error": "Missing dashboard_name. Provide one or set CW_DASHBOARD_NAME in environment."
            }

        dashboard = cloudwatch_manager.get_dashboard(resolved_dashboard_name)
        return {
            "success": True,
            "dashboard": dashboard
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.prompt()
def aws_incident_triage_prompt(
    incident_summary: str,
    severity: str = "SEV2",
    instance_id: str = "",
    log_group_name: str = "",
) -> str:
    """
    Prompt template for EC2/CloudWatch incident triage and first-response actions.
    """
    resolved_instance_id = instance_id or default_instance_id or "<unknown-instance-id>"
    resolved_log_group = log_group_name or default_log_group or "<unknown-log-group>"

    return (
        "You are the on-call cloud reliability engineer for AWS infrastructure.\\n"
        f"Severity: {severity}\\n"
        f"Incident summary: {incident_summary}\\n"
        f"Target instance: {resolved_instance_id}\\n"
        f"Target log group: {resolved_log_group}\\n\\n"
        "Perform triage in this order:\\n"
        "1) Confirm current instance health and status-check metrics over last 15 minutes.\\n"
        "2) Identify active ALARM state CloudWatch alarms for this instance by dimension InstanceId.\\n"
        "3) Pull recent logs and find first error signature and latest recurring pattern.\\n"
        "4) Correlate timeline across alarms, metrics spikes, and logs.\\n"
        "5) Propose immediate mitigations and rollback/safety checks.\\n\\n"
        "Use these MCP tools where applicable:\\n"
        "- Tool: aws_poll_alarm_notifications\n"
        "- Tool: aws_collect_ec2_health_snapshot\\n"
        "- Tool: aws_get_ec2_metrics\\n"
        "- Tool: aws_list_ec2_alarms\\n"
        "- Tool: aws_filter_logs\\n"
        "- Tool: aws_get_log_events\\n\\n"
        "Output format:\\n"
        "- Situation summary (3-5 bullets)\\n"
        "- Most likely root cause\\n"
        "- Confidence (High/Medium/Low) with evidence\\n"
        "- Immediate mitigation plan\\n"
        "- 24-hour prevention follow-up actions"
    )


@mcp.prompt()
def aws_observability_snapshot_interpreter_prompt(
    snapshot_json: str,
    objective: str = "Diagnose likely cause and propose mitigation",
) -> str:
    """
    Prompt template to interpret an observability snapshot payload.
    """
    return (
        "You are analyzing an AWS observability snapshot containing metrics, logs, and alarms.\\n"
        f"Objective: {objective}\\n\\n"
        "Snapshot JSON:\\n"
        f"{snapshot_json}\\n\\n"
        "Provide:\\n"
        "1) Top anomalies detected\\n"
        "2) Probable root cause hypotheses ranked by likelihood\\n"
        "3) Fast mitigation actions that are safe and reversible\\n"
        "4) Additional data needed to confirm root cause"
    )


# ========================================
# VPC MANAGEMENT TOOLS
# ========================================

@mcp.tool()
async def aws_create_vpc(cidr_block: str, name: str, tags: dict = None) -> dict:
    """
    Create a VPC with specified CIDR block.
    
    Args:
        cidr_block: CIDR block for VPC (e.g., '10.0.0.0/16')
        name: Name tag for the VPC
        tags: Optional additional tags as dict
    
    Returns:
        Dictionary with VPC details including vpc_id
    """
    try:
        vpc = vpc_manager.create_vpc(cidr_block, name, tags)
        return {
            "success": True,
            "message": f"VPC '{name}' created successfully",
            "vpc": vpc
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_subnet(vpc_id: str, cidr_block: str, availability_zone: str, 
                           name: str, is_public: bool = False, tags: dict = None) -> dict:
    """
    Create a subnet in a VPC.
    
    Args:
        vpc_id: VPC ID to create subnet in
        cidr_block: CIDR block for subnet (e.g., '10.0.1.0/24')
        availability_zone: AZ for subnet (e.g., 'us-east-1a')
        name: Name tag for the subnet
        is_public: Whether this is a public subnet (will auto-assign public IPs)
        tags: Optional additional tags
    
    Returns:
        Dictionary with subnet details including subnet_id
    """
    try:
        subnet = vpc_manager.create_subnet(vpc_id, cidr_block, availability_zone, name, is_public, tags)
        return {
            "success": True,
            "message": f"Subnet '{name}' created successfully",
            "subnet": subnet
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_internet_gateway(vpc_id: str, name: str, tags: dict = None) -> dict:
    """
    Create and attach an Internet Gateway to a VPC.
    Internet Gateways enable internet access for public subnets.
    
    Args:
        vpc_id: VPC ID to attach IGW to
        name: Name tag for the IGW
        tags: Optional additional tags
    
    Returns:
        Dictionary with IGW details including igw_id
    """
    try:
        igw = vpc_manager.create_internet_gateway(vpc_id, name, tags)
        return {
            "success": True,
            "message": f"Internet Gateway '{name}' created and attached to VPC",
            "internet_gateway": igw
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_nat_gateway(
    subnet_id: str,
    name: str,
    tags: dict = None,
    justification: str = None,
) -> dict:
    """
    Create a NAT Gateway in a public subnet.
    NAT Gateways enable internet access for instances in private subnets.
    Note: This allocates an Elastic IP which costs money.
    
    Args:
        subnet_id: Public subnet ID to place NAT Gateway in
        name: Name tag for the NAT Gateway
        tags: Optional additional tags
        justification: Optional reason for creating NAT (used by policy engine)
    
    Returns:
        Dictionary with NAT Gateway details including nat_gateway_id and public_ip
    """
    try:
        nat = vpc_manager.create_nat_gateway(subnet_id, name, tags)
        return {
            "success": True,
            "message": f"NAT Gateway '{name}' created with public IP {nat['public_ip']}",
            "nat_gateway": nat
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_route_table(vpc_id: str, name: str, routes: list = None, tags: dict = None) -> dict:
    """
    Create a route table with optional routes.
    
    Args:
        vpc_id: VPC ID to create route table in
        name: Name tag for the route table
        routes: List of route dicts:
                [{'destination': '0.0.0.0/0', 'gateway_id': 'igw-xxx'}]
                or
                [{'destination': '0.0.0.0/0', 'nat_gateway_id': 'nat-xxx'}]
        tags: Optional additional tags
    
    Returns:
        Dictionary with route table details including route_table_id
    """
    try:
        route_table = vpc_manager.create_route_table(vpc_id, name, routes, tags)
        return {
            "success": True,
            "message": f"Route table '{name}' created successfully",
            "route_table": route_table
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_associate_route_table(route_table_id: str, subnet_id: str) -> dict:
    """
    Associate a route table with a subnet.
    This determines how traffic from the subnet is routed.
    
    Args:
        route_table_id: Route table ID to associate
        subnet_id: Subnet ID to associate with
    
    Returns:
        Dictionary with association details
    """
    try:
        association = vpc_manager.associate_route_table(route_table_id, subnet_id)
        return {
            "success": True,
            "message": f"Route table associated with subnet successfully",
            "association": association
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_list_route_tables(vpc_id: str) -> dict:
    """
    List all route tables for a specific VPC with their routes and subnet associations.
    Use this to check what route tables already exist before creating new ones.
    
    Args:
        vpc_id: VPC ID to list route tables for
    
    Returns:
        Dictionary with list of route tables including:
        - Route table ID and name
        - Whether it's the main route table
        - All routes (destination -> target)
        - Associated subnets
    """
    try:
        # Get VPC details which includes route tables
        all_vpcs = vpc_manager.list_vpcs()
        target_vpc = next((vpc for vpc in all_vpcs if vpc['vpc_id'] == vpc_id), None)
        
        if not target_vpc:
            return {
                "success": False,
                "error": f"VPC not found: {vpc_id}"
            }
        
        route_tables = target_vpc.get('route_tables', [])
        
        return {
            "success": True,
            "vpc_id": vpc_id,
            "vpc_name": target_vpc['name'],
            "count": len(route_tables),
            "route_tables": route_tables
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_list_vpcs(tag_filter: dict = None) -> dict:
    """
    List all VPCs with their subnets, internet gateways, NAT gateways, route tables, and details.
    This returns comprehensive information about VPC networking topology.
    
    Args:
        tag_filter: Optional tag filter (e.g., {'ManagedBy': 'AIAgent'})
    
    Returns:
        Dictionary with list of VPCs and their complete network configuration including:
        - Subnets (with CIDR, AZ, type)
        - Internet Gateways
        - NAT Gateways (with public IPs)
        - Route Tables (with routes and subnet associations)
    """
    try:
        vpcs = vpc_manager.list_vpcs(tag_filter)
        return {
            "success": True,
            "count": len(vpcs),
            "vpcs": vpcs
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_get_vpc_details(vpc_id: str = None, vpc_name: str = None) -> dict:
    """
    Get detailed information about a specific VPC by ID or name.
    Use this to check if route tables, IGW, NAT gateways already exist before creating them.
    
    Args:
        vpc_id: VPC ID to query (e.g., 'vpc-123abc')
        vpc_name: VPC name to search for (alternative to vpc_id)
    
    Returns:
        Dictionary with detailed VPC information including:
        - All subnets with their CIDR blocks and availability zones
        - All route tables with their routes and associated subnets
        - Internet Gateways
        - NAT Gateways with public IPs and states
        - Security groups count
    """
    try:
        # Get all VPCs
        all_vpcs = vpc_manager.list_vpcs()
        
        # Find the requested VPC
        target_vpc = None
        if vpc_id:
            target_vpc = next((vpc for vpc in all_vpcs if vpc['vpc_id'] == vpc_id), None)
        elif vpc_name:
            target_vpc = next((vpc for vpc in all_vpcs if vpc['name'] == vpc_name), None)
        
        if not target_vpc:
            return {
                "success": False,
                "error": f"VPC not found with {'ID: ' + vpc_id if vpc_id else 'name: ' + vpc_name}"
            }
        
        # Return detailed information
        return {
            "success": True,
            "vpc": target_vpc,
            "summary": {
                "vpc_id": target_vpc['vpc_id'],
                "name": target_vpc['name'],
                "cidr_block": target_vpc['cidr_block'],
                "subnet_count": len(target_vpc.get('subnets', [])),
                "route_table_count": len(target_vpc.get('route_tables', [])),
                "internet_gateway_count": len(target_vpc.get('internet_gateways', [])),
                "nat_gateway_count": len(target_vpc.get('nat_gateways', [])),
                "has_internet_gateway": len(target_vpc.get('internet_gateways', [])) > 0,
                "has_nat_gateway": len(target_vpc.get('nat_gateways', [])) > 0
            }
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_vpc(vpc_id: str, force: bool = False) -> dict:
    """
    Delete a VPC and optionally all its dependencies.
    
    Args:
        vpc_id: VPC ID (vpc-xxx) or VPC name to delete
        force: If True, automatically delete all dependencies in the correct order:
               - Terminate EC2 instances
               - Disassociate and delete route tables
               - Delete NAT Gateways (waits for full deletion)
               - Release Elastic IPs
               - Detach and delete Internet Gateways
               - Delete subnets
               - Delete security groups
               - Delete VPC
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = vpc_manager.delete_vpc(vpc_id, force)
        return {
            "success": True,
            "message": f"VPC deleted successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_subnet(subnet_id: str) -> dict:
    """
    Delete a subnet.
    Note: Subnet must not have any dependencies (instances, ENIs, etc.).
    
    Args:
        subnet_id: Subnet ID to delete
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = vpc_manager.delete_subnet(subnet_id)
        return {
            "success": True,
            "message": f"Subnet {subnet_id} deleted successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_internet_gateway(igw_id: str, vpc_id: str = None) -> dict:
    """
    Detach and delete an Internet Gateway.
    
    Args:
        igw_id: Internet Gateway ID to delete
        vpc_id: Optional VPC ID to detach from (if not provided, will detect automatically)
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = vpc_manager.delete_internet_gateway(igw_id, vpc_id)
        return {
            "success": True,
            "message": f"Internet Gateway {igw_id} deleted successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_nat_gateway(nat_gateway_id: str) -> dict:
    """
    Delete a NAT Gateway.
    Note: NAT Gateway deletion is asynchronous and can take several minutes.
    
    Args:
        nat_gateway_id: NAT Gateway ID to delete
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = vpc_manager.delete_nat_gateway(nat_gateway_id)
        return {
            "success": True,
            "message": f"NAT Gateway {nat_gateway_id} deletion initiated",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_route_table(route_table_id: str) -> dict:
    """
    Delete a route table.
    Note: Cannot delete the main route table or route tables with subnet associations.
    
    Args:
        route_table_id: Route table ID to delete
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = vpc_manager.delete_route_table(route_table_id)
        return {
            "success": True,
            "message": f"Route table {route_table_id} deleted successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# ========================================
# SECURITY GROUP TOOLS
# ========================================

@mcp.tool()
async def aws_create_security_group(vpc_id: str, name: str, description: str, 
                                   rules: list = None, tags: dict = None) -> dict:
    """
    Create a security group with inbound/outbound rules.
    
    Args:
        vpc_id: VPC ID to create security group in
        name: Name for the security group
        description: Description of the security group's purpose
        rules: List of rule dicts. Each rule must have:
               - 'type': 'ingress' or 'egress'
               - 'protocol': 'tcp', 'udp', 'icmp', or '-1' (all)
               - 'port': 80 (or use 'from_port' and 'to_port' for ranges)
               - Source/Destination (choose ONE):
                 * 'cidr': '0.0.0.0/0' (for IP-based access)
                 * 'source_security_group_id': 'sg-xxx' (for SG-to-SG access)
               
               Examples:
               [
                   # Allow HTTP from anywhere
                   {'type': 'ingress', 'protocol': 'tcp', 'port': 80, 'cidr': '0.0.0.0/0'},
                   
                   # Allow port 8080 from another security group
                   {'type': 'ingress', 'protocol': 'tcp', 'port': 8080, 
                    'source_security_group_id': 'sg-1234567890abcdef0'}
               ]
        tags: Optional additional tags
    
    Returns:
        Dictionary with security group details including security_group_id
    """
    try:
        sg = security_manager.create_security_group(vpc_id, name, description, rules, tags)
        return {
            "success": True,
            "message": f"Security group '{name}' created successfully",
            "security_group": sg
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_add_security_group_rule(security_group_id: str, rule: dict) -> dict:
    """
    Add a single rule to an existing security group.
    
    Args:
        security_group_id: Security group ID to add rule to
        rule: Rule dict with:
              - 'type': 'ingress' or 'egress'
              - 'protocol': 'tcp', 'udp', 'icmp', or '-1'
              - 'port': port number or 'from_port' and 'to_port'
              - Source (choose ONE):
                * 'cidr': '10.0.0.0/16' (for IP-based access)
                * 'source_security_group_id': 'sg-xxx' (for security group access)
              
              Example allowing database access from app tier:
              {
                  'type': 'ingress',
                  'protocol': 'tcp', 
                  'port': 3306,
                  'source_security_group_id': 'sg-app-tier-id'
              }
    
    Returns:
        Dictionary with rule addition status
    """
    try:
        result = security_manager.add_security_group_rule(security_group_id, rule)
        return {
            "success": True,
            "message": f"Rule added to security group successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_edit_security_group_rule(security_group_id: str, old_rule: dict, new_rule: dict) -> dict:
    """
    Edit a security group rule by replacing an existing rule with a new one.

    Args:
        security_group_id: Security group ID to update
        old_rule: Existing rule definition to remove
        new_rule: New rule definition to add

    Returns:
        Dictionary with update status and replacement details
    """
    try:
        result = security_manager.edit_security_group_rule(
            security_group_id=security_group_id,
            old_rule=old_rule,
            new_rule=new_rule,
        )
        return {
            "success": True,
            "message": "Security group rule updated successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_remove_security_group_rule(security_group_id: str, rule: dict) -> dict:
    """
    Remove a single rule from an existing security group.

    Args:
        security_group_id: Security group ID to remove rule from
        rule: Rule definition to remove

    Returns:
        Dictionary with removal status and rule details
    """
    try:
        result = security_manager.remove_security_group_rule(
            security_group_id=security_group_id,
            rule=rule,
        )
        return {
            "success": True,
            "message": "Security group rule removed successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_list_security_groups(vpc_id: str = None, tag_filter: dict = None) -> dict:
    """
    List security groups with their rules.
    
    Args:
        vpc_id: Optional VPC ID to filter by
        tag_filter: Optional tag filter (e.g., {'ManagedBy': 'AIAgent'})
    
    Returns:
        Dictionary with list of security groups and their rules
    """
    try:
        sgs = security_manager.list_security_groups(vpc_id, tag_filter)
        return {
            "success": True,
            "count": len(sgs),
            "security_groups": sgs
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_security_group(security_group_id: str) -> dict:
    """
    Delete a security group.
    Note: Cannot delete security groups that are in use by instances.
    
    Args:
        security_group_id: Security group ID to delete
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = security_manager.delete_security_group(security_group_id)
        return {
            "success": True,
            "message": f"Security group {security_group_id} deleted successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  Launch Template Tools
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def aws_list_launch_templates(name_filter: str = None) -> dict:
    """
    List all EC2 launch templates in the region.

    Args:
        name_filter: Optional substring to filter by launch template name
    """
    try:
        return asg_manager.list_launch_templates(name_filter=name_filter)
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def aws_describe_launch_template(
    launch_template_id: str = None,
    launch_template_name: str = None,
    versions: list = None,
) -> dict:
    """
    Get details and version history of a launch template.

    Args:
        launch_template_id: The launch template ID (lt-xxxxxxxxxxxxxxxxx)
        launch_template_name: The launch template name (alternative to ID)
        versions: List of version numbers or aliases to fetch, e.g. ["$Default", "$Latest", "1", "3"].
                  Defaults to ["$Default", "$Latest"]
    """
    try:
        return asg_manager.describe_launch_template(
            launch_template_id=launch_template_id,
            launch_template_name=launch_template_name,
            versions=versions,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def aws_create_launch_template_version(
    launch_template_id: str = None,
    launch_template_name: str = None,
    source_version: str = "$Default",
    new_instance_type: str = None,
    description: str = None,
    set_as_default: bool = False,
) -> dict:
    """
    Create a new launch template version, inheriting all settings from source_version
    and optionally overriding the instance type.

    Args:
        launch_template_id: Launch template ID (provide this or launch_template_name)
        launch_template_name: Launch template name
        source_version: Version to base the new version on. Use "$Default", "$Latest", or a version number. Defaults to "$Default"
        new_instance_type: Override the instance type in the new version (e.g. "t3.small")
        description: Human-readable description for the new version
        set_as_default: If true, set the new version as the template's default version
    """
    try:
        return asg_manager.create_launch_template_version(
            launch_template_id=launch_template_id,
            launch_template_name=launch_template_name,
            source_version=source_version,
            new_instance_type=new_instance_type,
            description=description,
            set_as_default=set_as_default,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def aws_set_launch_template_default_version(
    version: str,
    launch_template_id: str = None,
    launch_template_name: str = None,
) -> dict:
    """
    Set the default version of a launch template.

    Args:
        version: Version number to set as default (e.g. "3" or "$Latest")
        launch_template_id: Launch template ID
        launch_template_name: Launch template name (alternative to ID)
    """
    try:
        return asg_manager.set_launch_template_default_version(
            version=version,
            launch_template_id=launch_template_id,
            launch_template_name=launch_template_name,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
#  Auto Scaling Group Tools
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def aws_list_asgs(asg_names: list = None) -> dict:
    """
    List Auto Scaling Groups with configuration summary.

    Args:
        asg_names: Optional list of specific ASG names to retrieve
    """
    try:
        return asg_manager.list_asgs(asg_names=asg_names)
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def aws_describe_asg(asg_name: str) -> dict:
    """
    Get full details of an Auto Scaling Group including instances, policies, and launch template.

    Args:
        asg_name: The Auto Scaling Group name
    """
    try:
        return asg_manager.describe_asg(asg_name=asg_name)
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def aws_get_instance_asg(instance_id: str) -> dict:
    """
    Check whether an EC2 instance belongs to an Auto Scaling Group and return ASG details.

    Args:
        instance_id: The EC2 instance ID (e.g. i-xxxxxxxxxxxxxxxxx)
    """
    try:
        return asg_manager.get_instance_asg(instance_id=instance_id)
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def aws_update_asg_launch_template(
    asg_name: str,
    launch_template_id: str = None,
    launch_template_name: str = None,
    version: str = "$Latest",
) -> dict:
    """
    Update an Auto Scaling Group to use a specific launch template version.
    If no launch template id/name is provided, the ASG's current template is kept
    and only the version pointer is updated.

    Args:
        asg_name: The Auto Scaling Group name
        launch_template_id: Override the launch template ID (optional)
        launch_template_name: Override the launch template name (optional)
        version: Version to use — a number, "$Latest", or "$Default". Defaults to "$Latest"
    """
    try:
        return asg_manager.update_asg_launch_template(
            asg_name=asg_name,
            launch_template_id=launch_template_id,
            launch_template_name=launch_template_name,
            version=version,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def aws_sync_asg_launch_template_after_resize(
    instance_id: str,
    new_instance_type: str,
    set_lt_as_default: bool = True,
) -> dict:
    """
    After a vertical resize (EC2 rightsizing), propagate the new instance type to the
    ASG's launch template so future scale-out events use the updated type.

    Workflow:
      1. Checks if the instance belongs to an ASG.
      2. Reads the ASG's current launch template reference.
      3. Creates a new launch template version with new_instance_type.
      4. Optionally sets the new version as the LT default.
      5. Updates the ASG to use $Latest.

    If the instance is not in an ASG, returns success with synced=false.

    Args:
        instance_id: The EC2 instance that was resized
        new_instance_type: The instance type it was resized to (e.g. "t3.small")
        set_lt_as_default: Whether to also set the new LT version as the template default (default: true)
    """
    try:
        return asg_manager.sync_asg_after_resize(
            instance_id=instance_id,
            new_instance_type=new_instance_type,
            set_lt_as_default=set_lt_as_default,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
#  Scaling Policy Tools
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def aws_put_asg_scaling_policy(
    asg_name: str,
    policy_name: str,
    policy_type: str = "TargetTrackingScaling",
    target_value: float = None,
    predefined_metric_type: str = None,
    disable_scale_in: bool = False,
    adjustment_type: str = None,
    scaling_adjustment: int = None,
    cooldown: int = None,
    step_adjustments: list = None,
    estimated_instance_warmup: int = None,
    metric_aggregation_type: str = "Average",
    alarm_name: str = None,
) -> dict:
    """
    Create or update a scaling policy on an Auto Scaling Group.

    policy_type options:
      - TargetTrackingScaling (default): AWS manages scale-in/out to maintain the target metric.
          Requires: target_value
          Optional: predefined_metric_type, disable_scale_in
      - SimpleScaling: scale by a fixed amount after a CloudWatch alarm fires.
          Requires: adjustment_type, scaling_adjustment
          Optional: cooldown, alarm_name
      - StepScaling: scale in steps based on alarm breach magnitude.
          Requires: adjustment_type, step_adjustments
          Optional: estimated_instance_warmup, metric_aggregation_type, alarm_name

    predefined_metric_type values (for TargetTrackingScaling):
      ASGAverageCPUUtilization | ASGAverageNetworkIn | ASGAverageNetworkOut | ALBRequestCountPerTarget

    adjustment_type values (for Simple/StepScaling):
      ChangeInCapacity | ExactCapacity | PercentChangeInCapacity

    step_adjustments format:
      [{"MetricIntervalLowerBound": 0, "MetricIntervalUpperBound": 10, "ScalingAdjustment": 1}, ...]
      Omit UpperBound on the last step to handle unbounded breaches.

    Args:
        asg_name: The Auto Scaling Group name
        policy_name: Unique name for the policy (used to update if it already exists)
        policy_type: TargetTrackingScaling | SimpleScaling | StepScaling
        target_value: Target metric value (TargetTracking only)
        predefined_metric_type: Metric to track (TargetTracking only, default: ASGAverageCPUUtilization)
        disable_scale_in: Prevent scale-in actions for this policy (TargetTracking only)
        adjustment_type: How scaling_adjustment is interpreted (Simple/Step only)
        scaling_adjustment: Number of instances to add/remove (SimpleScaling only)
        cooldown: Seconds to wait after scaling before allowing another action (SimpleScaling only)
        step_adjustments: List of step definitions (StepScaling only)
        estimated_instance_warmup: Seconds for new instances to contribute to metrics (StepScaling only)
        metric_aggregation_type: Average | Minimum | Maximum (StepScaling only, default: Average)
        alarm_name: Optional. Name of a pre-existing CloudWatch alarm to attach to this policy.
            When provided, the policy ARN is added to that alarm's AlarmActions so the alarm
            triggers this policy. Omit if no alarm attachment is needed right now.
    """
    try:
        return asg_manager.put_scaling_policy(
            asg_name=asg_name,
            policy_name=policy_name,
            policy_type=policy_type,
            target_value=target_value,
            predefined_metric_type=predefined_metric_type,
            disable_scale_in=disable_scale_in,
            adjustment_type=adjustment_type,
            scaling_adjustment=scaling_adjustment,
            cooldown=cooldown,
            step_adjustments=step_adjustments,
            estimated_instance_warmup=estimated_instance_warmup,
            metric_aggregation_type=metric_aggregation_type,
            alarm_name=alarm_name,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def aws_describe_asg_scaling_policies(asg_name: str) -> dict:
    """
    List all scaling policies attached to an Auto Scaling Group.

    Args:
        asg_name: The Auto Scaling Group name
    """
    try:
        return asg_manager.describe_scaling_policies(asg_name=asg_name)
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def aws_delete_asg_scaling_policy(asg_name: str, policy_name: str) -> dict:
    """
    Delete a scaling policy from an Auto Scaling Group.

    Args:
        asg_name: The Auto Scaling Group name
        policy_name: The name of the policy to delete
    """
    try:
        return asg_manager.delete_scaling_policy(asg_name=asg_name, policy_name=policy_name)
    except Exception as e:
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    # FastMCP handles the stdio server setup automatically
    print("📡 Starting MCP AWS Server...", file=sys.stderr)
    mcp.run()

