"""
Alarm Worker Entry Point

Runs an always-on loop that polls SQS for alarm notifications and
triggers the AI agent automatically for ALARM events.
"""

import os
import re
import time
import logging
import json
from datetime import datetime, timezone
from typing import Dict, List

from dotenv import load_dotenv

from agent.core import run_agent_sync
from cloud_providers.aws.cloudwatch import CloudWatchManager


def _configure_logging() -> logging.Logger:
    """Configure worker logging to stdout/stderr for systemd capture."""
    log_level_name = os.getenv("ALARM_WORKER_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
    )
    return logging.getLogger("alarm_worker")


def _log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": logging.getLevelName(level),
        "event": event,
    }
    payload.update(fields)
    logger.log(level, json.dumps(payload, ensure_ascii=True))


def _extract_instance_id(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""

    trigger = payload.get("Trigger", {})
    dimensions = trigger.get("Dimensions", []) if isinstance(trigger, dict) else []

    for dim in dimensions:
        if not isinstance(dim, dict):
            continue

        name = dim.get("name") or dim.get("Name")
        value = dim.get("value") or dim.get("Value")
        if name == "InstanceId" and value:
            return str(value)

    return ""


def _extract_metric_name(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""

    trigger = payload.get("Trigger", {})
    if not isinstance(trigger, dict):
        return ""

    metric_name = trigger.get("MetricName")
    return str(metric_name) if metric_name else ""


def _extract_dimensions(payload: dict) -> List[Dict[str, str]]:
    if not isinstance(payload, dict):
        return []

    trigger = payload.get("Trigger", {})
    dimensions = trigger.get("Dimensions", []) if isinstance(trigger, dict) else []
    if not isinstance(dimensions, list):
        return []

    result = []
    for dim in dimensions:
        if not isinstance(dim, dict):
            continue
        name = dim.get("name") or dim.get("Name")
        value = dim.get("value") or dim.get("Value")
        if name and value is not None:
            result.append({"name": str(name), "value": str(value)})
    return result


def _classify_alarm(alarm_name: str, payload: dict) -> str:
    name = str(alarm_name or "").lower()
    metric_name = _extract_metric_name(payload).lower()

    if "statuscheckfailed_system" in metric_name or "systemfailure" in name:
        return "ec2_system_status"
    if "statuscheckfailed_instance" in metric_name or "failedinstance" in name:
        return "ec2_instance_status"
    if "disk_used_percent" in metric_name or "disk" in name:
        return "disk_pressure"
    if "mem_used_percent" in metric_name or "memory" in name or "memwarning" in name:
        return "memory_pressure"
    if "cpuutilization" in metric_name or "cpu" in name:
        return "cpu_pressure"
    if (
        "mernerrorcount" in metric_name
        or "mernservicedown" in metric_name
        or "applicationerror" in name
        or "mern" in name
        or ("error" in name and "count" in name)
        or ("service" in name and "down" in name)
    ):
        return "application_error"
    return "generic_alarm"


def _extract_triggering_child_alarm(reason: str, payload: dict) -> str:
    """
    Return the name of the child alarm that triggered a composite alarm.
    Prefers structured TriggeringChildren from payload; falls back to parsing reason text.
    Returns empty string for simple (non-composite) alarms.
    """
    if isinstance(payload, dict):
        children = payload.get("TriggeringChildren", [])
        if isinstance(children, list):
            for child in children:
                if isinstance(child, str) and child:
                    return child
                if isinstance(child, dict):
                    name = child.get("AlarmName") or child.get("Arn", "")
                    if name:
                        return str(name)

    if reason:
        # "The alarm name is [MERN-ServiceDown-Alarm]" or "alarm name is MERN-ServiceDown-Alarm"
        match = re.search(r"alarm name is \[?([^\]\s,;]+)\]?", reason, re.IGNORECASE)
        if match:
            return match.group(1)
        # "MERN-ServiceDown-Alarm transitioned to ALARM"
        match = re.search(r"([A-Za-z0-9_\-]+(?:ServiceDown|ErrorCount|Alarm)[A-Za-z0-9_\-]*)\s+transitioned", reason, re.IGNORECASE)
        if match:
            return match.group(1)

    return ""


def _build_helper_analysis_request(
    alarm_family: str,
    instance_id: str,
    fallback_instance_name: str,
    metric_name: str,
    triggering_child: str = "",
) -> str:
    target = f"instance_id={instance_id}" if instance_id else f"instance_name={fallback_instance_name}"
    metric_hint = metric_name or "unknown"
    base = (
        f"target={target}; "
        "time_window=last 60 minutes; "
        f"metric_hint={metric_hint}; "
    )

    if alarm_family in {"ec2_system_status", "ec2_instance_status"}:
        scope = "scope=host health and status checks first, then metrics if needed"
    elif alarm_family == "disk_pressure":
        scope = (
            "scope=disk metrics and system/application logs, plus host filesystem checks "
            "to identify top large files/paths and inode pressure; traces only if logs indicate request-path errors"
        )
    elif alarm_family == "memory_pressure":
        scope = "scope=memory metrics and app/agent logs; traces only if logs indicate latency/error propagation"
    elif alarm_family == "cpu_pressure":
        scope = "scope=cpu metrics first, then correlated app/agent logs; xray traces only for targeted confirmation"
    elif alarm_family == "application_error":
        child_lower = triggering_child.lower()
        if triggering_child and ("servicedown" in child_lower or ("service" in child_lower and "down" in child_lower)):
            scope = (
                "scope=service outage (triggering_child=" + triggering_child + "): "
                "skip log scanning entirely; "
                "verify mern-api.service and nginx.service status via SSM immediately; "
                "if either service is inactive/failed, restart it if restart is allowed; "
                "do not waste calls checking /mern-app/api logs before confirming service state"
            )
        elif triggering_child and ("errorcount" in child_lower or ("error" in child_lower and "count" in child_lower)):
            scope = (
                "scope=application error logs (triggering_child=" + triggering_child + "): "
                "check /mern-app/api for recent error patterns "
                "(exceptions, MongoError, ECONNREFUSED, UnhandledPromiseRejection, crashed); "
                "then verify mern-api.service and nginx.service are active via SSM"
            )
        else:
            scope = (
                "scope=application logs first: check /mern-app/api for error patterns "
                "(exceptions, MongoError, ECONNREFUSED, UnhandledPromiseRejection, crashed), "
                "then verify mern-api.service and nginx.service are active via SSM; "
                "identify whether the trigger was a log error or a service outage"
            )
    else:
        scope = "scope=balanced diagnostics across metrics, logs, traces"

    return (
        "Base analysis_request seed for delegate_observability_analysis: "
        + base
        + scope
        + ". The agent may append additional context, hypotheses, and focused checks as needed."
    )


def _parse_state_change_time(raw: str) -> datetime | None:
    """Parse CloudWatch StateChangeTime string into an aware datetime."""
    if not raw:
        return None
    try:
        normalized = str(raw).strip().replace("+0000", "+00:00")
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


def _parse_bool_env(var_name: str, default: bool) -> bool:
    raw = os.getenv(var_name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_int_env(var_name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(var_name)
    if raw is None:
        return max(default, minimum)

    try:
        parsed = int(raw)
    except ValueError:
        return max(default, minimum)

    return max(parsed, minimum)


def _parse_csv_env(var_name: str) -> List[str]:
    raw = os.getenv(var_name, "")
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _build_goal(
    notification: dict,
    fallback_instance_name: str,
    target_os: str,
    auto_mitigate: bool,
    allow_reboot_on_status_check_failure: bool,
    allow_disk_cleanup_apply: bool,
    restart_services: List[str],
) -> str:
    alarm = notification.get("alarm", {}) if isinstance(notification, dict) else {}
    payload = notification.get("payload", {}) if isinstance(notification, dict) else {}

    alarm_name = alarm.get("name") or "<unknown-alarm>"
    new_state = alarm.get("new_state") or "<unknown-state>"
    reason = alarm.get("reason") or ""
    metric_name = _extract_metric_name(payload)
    dimensions = _extract_dimensions(payload)
    alarm_family = _classify_alarm(alarm_name, payload)

    instance_id = _extract_instance_id(payload)
    triggering_child = _extract_triggering_child_alarm(reason, payload)
    target_phrase = f"instance_id {instance_id}" if instance_id else f"instance named {fallback_instance_name}"
    dimensions_summary = ", ".join(f"{d['name']}={d['value']}" for d in dimensions[:10]) or "<none>"
    allowed_service_list = ", ".join(restart_services) if restart_services else "<none>"
    mitigation_mode = "execute-safe-mitigations" if auto_mitigate else "recommend-only"
    helper_analysis_request_base = _build_helper_analysis_request(
        alarm_family=alarm_family,
        instance_id=instance_id,
        fallback_instance_name=fallback_instance_name,
        metric_name=metric_name,
        triggering_child=triggering_child,
    )

    child_context = f"Triggering child alarm: {triggering_child}. " if triggering_child else ""

    child_lower = triggering_child.lower()
    is_service_outage = alarm_family == "application_error" and (
        "servicedown" in child_lower or ("service" in child_lower and "down" in child_lower)
    )

    if is_service_outage:
        pre_delegate_guidance = (
            "IMPORTANT: This is a service-outage alarm. "
            "Do NOT call delegate_observability_analysis — it is unnecessary here. "
            "Act directly: resolve the instance_id if needed (aws_list_ec2_instances), "
            "then call aws_ssm_get_service_status for mern-api.service and nginx.service, "
            "restart any inactive service using aws_ssm_restart_service if it is in the allowed list, "
            "verify the service is active after restart, and report findings. "
        )
    elif alarm_family == "application_error":
        pre_delegate_guidance = (
            "Call delegate_observability_analysis before collecting metrics or health snapshots. "
            "Do NOT call aws_collect_ec2_health_snapshot before the helper. "
        )
    else:
        pre_delegate_guidance = ""

    delegate_instruction = (
        ""
        if is_service_outage
        else (
            "Before deciding mitigation, call delegate_observability_analysis once. "
            f"Start helper analysis_request from this base seed and extend it as needed: {helper_analysis_request_base}. "
            "Keep the base fields target, time_window, metric_hint, and scope, then add any extra context you need. "
            "Use the helper structured JSON report as primary evidence. "
        )
    )

    return (
        "An alarm notification was already received from SQS by the worker. "
        "Use this provided alarm context directly for triage and mitigation. "
        "Do not call aws_poll_alarm_notifications again unless required context is missing. "
        f"{pre_delegate_guidance}"
        f"{delegate_instruction}"
        f"Triage this ALARM for {target_phrase}. "
        f"Alarm context: name={alarm_name}, state={new_state}, family_hint={alarm_family}, metric_hint={metric_name or '<unknown>'}. "
        f"Dimensions: {dimensions_summary}. "
        f"Reason: {reason}. "
        f"{child_context}"
        "You can use generic mitigation tools when needed (not alarm-specific): "
        "aws_collect_ec2_health_snapshot, aws_get_ec2_instance_status, aws_get_ec2_instance_ssm_status, "
        "aws_get_ec2_metrics, aws_list_ec2_alarms, aws_ssm_collect_host_diagnostics, aws_ssm_safe_disk_cleanup, "
        "aws_ssm_get_service_status, aws_ssm_restart_service, aws_reboot_ec2_instance. "
        f"Execution mode: {mitigation_mode}. "
        f"target_os for SSM tools: {target_os}. "
        f"Allowed automatic service restarts: {allowed_service_list}. "
        f"Reboot allowed for status-check failures: {allow_reboot_on_status_check_failure}. "
        f"Disk cleanup apply mode allowed: {allow_disk_cleanup_apply}. "
        "If mode is recommend-only, do not execute mutating actions. "
        "If mode is execute-safe-mitigations: run diagnostics first, prefer reversible actions, "
        "and only execute changes that match the permissions above. "
        "For disk pressure, always run aws_ssm_safe_disk_cleanup with dry_run=true before any apply attempt. "
        "Return: root cause, actions taken, current risk, and safe follow-up actions."
    )


def run_alarm_worker() -> None:
    load_dotenv()
    logger = _configure_logging()

    region = os.getenv("AWS_REGION", "us-east-1")
    queue_url = os.getenv("ALARM_SQS_QUEUE_URL", "").strip()
    fallback_instance_name = os.getenv("ALARM_TARGET_INSTANCE_NAME", "TestWebServer")

    max_messages = int(os.getenv("ALARM_WORKER_MAX_MESSAGES", "1"))
    wait_time_seconds = int(os.getenv("ALARM_WORKER_WAIT_TIME_SECONDS", "20"))
    visibility_timeout = int(os.getenv("ALARM_WORKER_VISIBILITY_TIMEOUT", "300"))
    loop_sleep_seconds = int(os.getenv("ALARM_WORKER_LOOP_SLEEP_SECONDS", "2"))
    process_only_alarm = os.getenv("ALARM_WORKER_PROCESS_ONLY_ALARM", "true").lower() == "true"
    require_success_for_ack = _parse_bool_env("ALARM_WORKER_REQUIRE_SUCCESS_FOR_ACK", True)
    auto_mitigate = _parse_bool_env("ALARM_WORKER_AUTO_MITIGATE", True)
    allow_reboot_on_status_check_failure = _parse_bool_env(
        "ALARM_WORKER_ALLOW_REBOOT_ON_STATUS_CHECK_FAILURE",
        False,
    )
    allow_disk_cleanup_apply = _parse_bool_env("ALARM_WORKER_ALLOW_DISK_CLEANUP_APPLY", False)
    target_os = os.getenv("ALARM_WORKER_TARGET_OS", "amazon-linux-2023").strip() or "amazon-linux-2023"
    restart_services = _parse_csv_env("ALARM_WORKER_RESTART_SERVICES")
    agent_attempts = _parse_int_env("ALARM_WORKER_AGENT_ATTEMPTS", 1, minimum=1)

    if not queue_url:
        raise RuntimeError("ALARM_SQS_QUEUE_URL is required for alarm worker")

    manager = CloudWatchManager(region=region)

    _log_event(
        logger,
        logging.INFO,
        "worker_started",
        region=region,
        queue_url=queue_url,
        process_only_alarm=process_only_alarm,
        require_success_for_ack=require_success_for_ack,
        auto_mitigate=auto_mitigate,
        allow_reboot_on_status_check_failure=allow_reboot_on_status_check_failure,
        allow_disk_cleanup_apply=allow_disk_cleanup_apply,
        target_os=target_os,
        restart_services=restart_services,
        agent_attempts=agent_attempts,
    )

    while True:
        try:
            polled = manager.poll_alarm_notifications(
                queue_url=queue_url,
                max_messages=max_messages,
                wait_time_seconds=wait_time_seconds,
                visibility_timeout=visibility_timeout,
                delete_on_read=False,
            )
        except Exception:
            logger.exception(json.dumps({"event": "sqs_poll_failed", "note": "Retrying after backoff."}, ensure_ascii=True))
            time.sleep(loop_sleep_seconds)
            continue

        notifications = polled.get("notifications", [])
        if not notifications:
            _log_event(logger, logging.DEBUG, "no_notifications")
            time.sleep(loop_sleep_seconds)
            continue

        for notification in notifications:
            alarm = notification.get("alarm", {}) if isinstance(notification, dict) else {}
            state = str(alarm.get("new_state") or "").upper()
            receipt_handle = notification.get("receipt_handle")

            _log_event(
                logger,
                logging.INFO,
                "alarm_received",
                alarm_name=alarm.get("name"),
                state=state,
                instance_id=_extract_instance_id(notification.get("payload", {})),
                action="evaluate",
            )

            if process_only_alarm and state != "ALARM":
                _log_event(
                    logger,
                    logging.INFO,
                    "alarm_skipped",
                    alarm_name=alarm.get("name"),
                    state=state,
                    reason="non_alarm_state",
                    action="acknowledge",
                )
                if receipt_handle:
                    try:
                        manager.delete_alarm_notification(queue_url=queue_url, receipt_handle=receipt_handle)
                    except Exception:
                        logger.exception(json.dumps({"event": "ack_failed", "reason": "non_alarm_ack"}, ensure_ascii=True))
                continue

            processing_start = datetime.now(timezone.utc)
            state_change_time = _parse_state_change_time(alarm.get("state_changed_at"))
            # notification_delay: SQS queue dwell time (alarm fired → worker picked it up).
            # In production this is ~10-30s; high values mean the worker was offline.
            # Clamped to 0 — negative values are sub-second clock skew between local clock and AWS.
            notification_delay_seconds = (
                max(0.0, (processing_start - state_change_time).total_seconds())
                if state_change_time else None
            )

            goal = _build_goal(
                notification=notification,
                fallback_instance_name=fallback_instance_name,
                target_os=target_os,
                auto_mitigate=auto_mitigate,
                allow_reboot_on_status_check_failure=allow_reboot_on_status_check_failure,
                allow_disk_cleanup_apply=allow_disk_cleanup_apply,
                restart_services=restart_services,
            )

            agent_success = False
            last_agent_result = None

            for attempt in range(1, agent_attempts + 1):
                _log_event(
                    logger,
                    logging.INFO,
                    "trigger_agent",
                    alarm_name=alarm.get("name"),
                    state=state,
                    attempt=attempt,
                    action="run_agent_sync",
                    notification_delay_seconds=round(notification_delay_seconds, 1) if notification_delay_seconds is not None else None,
                    auto_mitigate=auto_mitigate,
                )

                try:
                    last_agent_result = run_agent_sync(goal)
                    if isinstance(last_agent_result, dict):
                        agent_success = bool(last_agent_result.get("success"))
                    else:
                        # Backward-compat fallback for legacy return types.
                        agent_success = True

                    if agent_success:
                        break

                    _log_event(
                        logger,
                        logging.WARNING,
                        "agent_attempt_incomplete",
                        alarm_name=alarm.get("name"),
                        state=state,
                        attempt=attempt,
                        reason=(last_agent_result or {}).get("reason") if isinstance(last_agent_result, dict) else "unknown",
                    )
                except Exception as exc:
                    logger.exception(json.dumps({"event": "worker_processing_failed", "attempt": attempt}, ensure_ascii=True))
                    _log_event(
                        logger,
                        logging.WARNING,
                        "agent_attempt_failed",
                        alarm_name=alarm.get("name"),
                        state=state,
                        attempt=attempt,
                        error=str(exc),
                    )

            ttm_seconds = (
                last_agent_result.get("ttm_seconds")
                if isinstance(last_agent_result, dict) else None
            )
            trajectory_length = (
                last_agent_result.get("trajectory_length")
                if isinstance(last_agent_result, dict) else None
            )
            # e2e: total clock time from alarm firing to last mutating action.
            # e2e = notification_delay + ttm (agent diagnosis + remediation time).
            e2e_seconds = (
                round(notification_delay_seconds + ttm_seconds, 1)
                if notification_delay_seconds is not None and ttm_seconds is not None
                else None
            )

            should_acknowledge = bool(receipt_handle) and (agent_success or not require_success_for_ack)
            if should_acknowledge:
                try:
                    manager.delete_alarm_notification(queue_url=queue_url, receipt_handle=receipt_handle)
                    _log_event(
                        logger,
                        logging.INFO,
                        "notification_processed",
                        alarm_name=alarm.get("name"),
                        state=state,
                        action="acknowledged",
                        agent_success=agent_success,
                        require_success_for_ack=require_success_for_ack,
                        notification_delay_seconds=round(notification_delay_seconds, 1) if notification_delay_seconds is not None else None,
                        ttm_seconds=round(ttm_seconds, 1) if ttm_seconds is not None else None,
                        e2e_seconds=e2e_seconds,
                        trajectory_length=trajectory_length,
                    )
                except Exception:
                    logger.exception(json.dumps({"event": "ack_failed", "reason": "post_agent"}, ensure_ascii=True))
                    _log_event(
                        logger,
                        logging.WARNING,
                        "message_not_acknowledged",
                        alarm_name=alarm.get("name"),
                        state=state,
                        note="Acknowledgment failed; message will reappear after visibility timeout.",
                    )
            else:
                _log_event(
                    logger,
                    logging.WARNING,
                    "message_not_acknowledged",
                    alarm_name=alarm.get("name"),
                    state=state,
                    agent_success=agent_success,
                    require_success_for_ack=require_success_for_ack,
                    last_agent_result=last_agent_result,
                    note="Message will become visible again after visibility timeout.",
                )

        time.sleep(loop_sleep_seconds)


if __name__ == "__main__":
    run_alarm_worker()
