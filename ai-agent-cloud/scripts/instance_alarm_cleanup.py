"""
Lambda function: per-instance alarm cleanup on EC2 termination.

Triggered by an EventBridge rule watching for:
  source:      aws.ec2
  detail-type: EC2 Instance State-change Notification
  detail.state: terminated

Deletes the SQS queue, SNS topic (+ subscription), and CloudWatch alarms
that were created by instance_alarm_setup.py for the terminated instance.

Deploy as a Lambda function (Python 3.12) with the IAM permissions listed
in docs/InstanceAlarmCleanupLambda.md.
"""

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"

ALARM_PREFIXES = [
    "CPUUtilizationAlarm-",
    "DiskPressureAlarm-",
    "MemWarningAlarm-",
    "FailedInstanceAlarm-",
    "SystemFailureAlarm-",
]


def handler(event: dict, context) -> dict:
    detail = event.get("detail", {})
    instance_id: str = detail.get("instance-id", "")
    state: str = detail.get("state", "")

    if state != "terminated" or not instance_id:
        return {"status": "skipped", "reason": f"state={state!r}, instance_id={instance_id!r}"}

    short_id = instance_id.replace("i-", "")
    resource_name = f"ai-agent-alarms-{short_id}"

    sqs = boto3.client("sqs", region_name=REGION)
    sns = boto3.client("sns", region_name=REGION)
    cw = boto3.client("cloudwatch", region_name=REGION)

    deleted = []
    errors = []

    # --- Delete CloudWatch alarms ---
    alarm_names = [f"{prefix}{instance_id}" for prefix in ALARM_PREFIXES]
    try:
        cw.delete_alarms(AlarmNames=alarm_names)
        deleted.append(f"alarms({len(alarm_names)})")
    except ClientError as e:
        errors.append(f"delete_alarms: {e}")

    # --- Delete SQS queue ---
    try:
        url = sqs.get_queue_url(QueueName=resource_name)["QueueUrl"]
        sqs.delete_queue(QueueUrl=url)
        deleted.append(f"sqs:{resource_name}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code != "AWS.SimpleQueueService.NonExistentQueue":
            errors.append(f"delete_queue: {e}")

    # --- Delete SNS topic ---
    # SNS has no get-by-name API; page through topics to find it.
    paginator = sns.get_paginator("list_topics")
    topic_arn = None
    for page in paginator.paginate():
        for t in page["Topics"]:
            if t["TopicArn"].endswith(f":{resource_name}"):
                topic_arn = t["TopicArn"]
                break
        if topic_arn:
            break

    if topic_arn:
        # Unsubscribe SQS before deleting topic (SNS delete_topic handles it,
        # but being explicit avoids stale subscriptions if the queue was already gone).
        try:
            subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn)
            for sub in subs.get("Subscriptions", []):
                if sub.get("Protocol") == "sqs":
                    sns.unsubscribe(SubscriptionArn=sub["SubscriptionArn"])
        except ClientError:
            pass

        try:
            sns.delete_topic(TopicArn=topic_arn)
            deleted.append(f"sns:{resource_name}")
        except ClientError as e:
            errors.append(f"delete_topic: {e}")
    else:
        errors.append(f"sns topic not found: {resource_name}")

    result = {
        "status": "errors" if errors else "cleaned",
        "instance_id": instance_id,
        "deleted": deleted,
        "errors": errors,
    }
    print(result)
    return result
