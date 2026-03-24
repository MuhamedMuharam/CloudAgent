"""
Alarm Worker Entry Point

Runs an always-on loop that polls SQS for alarm notifications and
triggers the AI agent automatically for ALARM events.
"""

import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from agent.core import run_agent_sync
from cloud_providers.aws.cloudwatch import CloudWatchManager


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
        "Check pending alarm notifications from SQS and triage the latest ALARM "
        f"for {target_phrase}. "
        f"Alarm context: name={alarm_name}, state={new_state}. "
        f"Reason: {reason}. "
        "Provide immediate mitigation steps and safe follow-up actions."
    )


def run_alarm_worker() -> None:
    load_dotenv()

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

    print("=" * 60)
    print("AI Agent Alarm Worker")
    print("=" * 60)
    print(f"Region: {region}")
    print(f"Queue: {queue_url}")
    print(f"Process only ALARM: {process_only_alarm}")
    print("Polling started. Press Ctrl+C to stop.")

    while True:
        polled = manager.poll_alarm_notifications(
            queue_url=queue_url,
            max_messages=max_messages,
            wait_time_seconds=wait_time_seconds,
            visibility_timeout=visibility_timeout,
            delete_on_read=False,
        )

        notifications = polled.get("notifications", [])
        if not notifications:
            time.sleep(loop_sleep_seconds)
            continue

        for notification in notifications:
            alarm = notification.get("alarm", {}) if isinstance(notification, dict) else {}
            state = str(alarm.get("new_state") or "").upper()
            receipt_handle = notification.get("receipt_handle")

            timestamp = datetime.now(timezone.utc).isoformat()
            print(f"\n[{timestamp}] Received notification state={state} alarm={alarm.get('name')}")

            if process_only_alarm and state != "ALARM":
                print("Skipping non-ALARM notification and acknowledging message.")
                if receipt_handle:
                    manager.delete_alarm_notification(queue_url=queue_url, receipt_handle=receipt_handle)
                continue

            goal = _build_goal(notification, fallback_instance_name)
            print("Triggering run_agent_sync for notification...")

            try:
                run_agent_sync(goal)
                if receipt_handle:
                    manager.delete_alarm_notification(queue_url=queue_url, receipt_handle=receipt_handle)
                print("Notification processed and acknowledged.")
            except Exception as exc:
                print(f"Worker failed to process notification: {exc}")
                print("Message not acknowledged; it will become visible again after visibility timeout.")

        time.sleep(loop_sleep_seconds)


if __name__ == "__main__":
    run_alarm_worker()
