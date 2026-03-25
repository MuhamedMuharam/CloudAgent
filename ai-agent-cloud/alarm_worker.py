"""
Alarm Worker Entry Point

Runs an always-on loop that polls SQS for alarm notifications and
triggers the AI agent automatically for ALARM events.
"""

import os
import time
import logging
import json
from datetime import datetime, timezone

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


def _build_goal(notification: dict, fallback_instance_name: str) -> str:
    alarm = notification.get("alarm", {}) if isinstance(notification, dict) else {}
    payload = notification.get("payload", {}) if isinstance(notification, dict) else {}

    alarm_name = alarm.get("name") or "<unknown-alarm>"
    new_state = alarm.get("new_state") or "<unknown-state>"
    reason = alarm.get("reason") or ""

    instance_id = _extract_instance_id(payload)
    target_phrase = f"instance_id {instance_id}" if instance_id else f"instance named {fallback_instance_name}"

    return (
        "An alarm notification was already received from SQS by the worker. "
        "Use the provided alarm context directly for triage and mitigation. "
        "Do not call aws_poll_alarm_notifications again unless required context is missing. "
        "Triage this ALARM "
        f"for {target_phrase}. "
        f"Alarm context: name={alarm_name}, state={new_state}. "
        f"Reason: {reason}. "
        "Provide immediate mitigation steps and safe follow-up actions."
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

            goal = _build_goal(notification, fallback_instance_name)
            _log_event(
                logger,
                logging.INFO,
                "trigger_agent",
                alarm_name=alarm.get("name"),
                state=state,
                action="run_agent_sync",
            )

            try:
                run_agent_sync(goal)
                if receipt_handle:
                    manager.delete_alarm_notification(queue_url=queue_url, receipt_handle=receipt_handle)
                _log_event(
                    logger,
                    logging.INFO,
                    "notification_processed",
                    alarm_name=alarm.get("name"),
                    state=state,
                    action="acknowledged",
                )
            except Exception:
                logger.exception(json.dumps({"event": "worker_processing_failed"}, ensure_ascii=True))
                _log_event(
                    logger,
                    logging.WARNING,
                    "message_not_acknowledged",
                    alarm_name=alarm.get("name"),
                    state=state,
                    note="Message will become visible again after visibility timeout.",
                )

        time.sleep(loop_sleep_seconds)


if __name__ == "__main__":
    run_alarm_worker()
