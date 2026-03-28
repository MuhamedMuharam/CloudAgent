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

# Import AWS managers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from cloud_providers.aws.ec2 import EC2Manager
from cloud_providers.aws.vpc import VPCManager
from cloud_providers.aws.security import SecurityGroupManager
from cloud_providers.aws.cloudwatch import CloudWatchManager
from cloud_providers.aws.ssm import SSMManager
from cloud_providers.aws.xray import XRayManager

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
async def aws_create_ec2_instance(name: str, cpu: int = 2, ram: int = 4) -> dict:
    """
    Create a new EC2 instance with specified resources.
    Automatically selects appropriate instance type based on CPU/RAM.
    Instances are tagged as 'ManagedBy: AIAgent' for tracking.
    
    Args:
        name: Name for the EC2 instance
        cpu: Number of virtual CPU cores (default: 2)
        ram: RAM in gigabytes (default: 4)
    
    Returns:
        Dictionary with success status, message, and instance details
    """
    try:
        instance = ec2_manager.create_instance(name=name, cpu=cpu, ram=ram)
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
async def aws_ssm_run_command(
    instance_ids: list,
    commands: list,
    comment: str = None,
    timeout_seconds: int = 600,
    working_directory: str = None,
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
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Start a systemd service on one or more EC2 instances via SSM.

    Args:
        instance_ids: List of EC2 instance IDs
        service_name: Systemd service unit name (e.g., ai-agent.service)
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
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Stop a systemd service on one or more EC2 instances via SSM.

    Args:
        instance_ids: List of EC2 instance IDs
        service_name: Systemd service unit name
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
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Restart a systemd service on one or more EC2 instances via SSM.

    Args:
        instance_ids: List of EC2 instance IDs
        service_name: Systemd service unit name
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
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    Get active/inactive status of a systemd service via SSM.

    Args:
        instance_ids: List of EC2 instance IDs
        service_name: Systemd service unit name
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
    wait_for_completion: bool = True,
    completion_timeout_seconds: int = 60,
    poll_interval_seconds: int = 2,
) -> dict:
    """
    List running systemd services on an EC2 instance via SSM.

    Args:
        instance_id: EC2 instance ID
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

        metrics = cloudwatch_manager.get_ec2_metrics(
            instance_id=resolved_instance_id,
            minutes=minutes,
            period_seconds=period_seconds,
            include_agent_metrics=include_agent_metrics,
            agent_namespace=agent_namespace,
        )
        return {
            "success": True,
            "metrics": metrics
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


@mcp.resource("aws://observability/snapshot")
def aws_observability_snapshot_resource() -> str:
    """
    Resource: region-level observability snapshot using configured defaults.

    Uses CW_TEST_INSTANCE_ID and CW_TEST_LOG_GROUP when available.
    """
    snapshot = _build_observability_snapshot()
    return json.dumps(snapshot, indent=2)


@mcp.resource("aws://observability/ec2/{instance_id}/snapshot")
def aws_observability_ec2_snapshot_resource(instance_id: str) -> str:
    """
    Resource: observability snapshot for a specific EC2 instance ID.
    """
    snapshot = _build_observability_snapshot(instance_id=instance_id)
    return json.dumps(snapshot, indent=2)


@mcp.resource("aws://observability/log-group/{log_group_name}/snapshot")
def aws_observability_log_group_snapshot_resource(log_group_name: str) -> str:
    """
    Resource: observability snapshot focused on a specific CloudWatch log group.
    """
    snapshot = _build_observability_snapshot(log_group_name=log_group_name)
    return json.dumps(snapshot, indent=2)


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
        "Use these MCP resources/tools where applicable:\\n"
        "- Resource: aws://observability/ec2/{instance_id}/snapshot\\n"
        "- Resource: aws://observability/log-group/{log_group_name}/snapshot\\n"
        "- Tool: aws_poll_alarm_notifications\n"
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
async def aws_create_nat_gateway(subnet_id: str, name: str, tags: dict = None) -> dict:
    """
    Create a NAT Gateway in a public subnet.
    NAT Gateways enable internet access for instances in private subnets.
    Note: This allocates an Elastic IP which costs money.
    
    Args:
        subnet_id: Public subnet ID to place NAT Gateway in
        name: Name tag for the NAT Gateway
        tags: Optional additional tags
    
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


if __name__ == "__main__":
    # FastMCP handles the stdio server setup automatically
    print("📡 Starting MCP AWS Server...", file=sys.stderr)
    mcp.run()

