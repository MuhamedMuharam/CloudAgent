"""
One-shot deployment script: creates the IAM role, Lambda function, and EventBridge
rule that clean up per-instance SQS/SNS/alarms when an EC2 instance terminates.

The Lambda handler code is embedded in this file — instance_alarm_cleanup.py is no
longer required separately.

Run once from your workstation (not from the instance):
    python scripts/deploy_cleanup_lambda.py

Requirements:
  - AWS credentials with IAM, Lambda, and EventBridge permissions
"""

import io
import json
import os
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

# ── Configurable ──────────────────────────────────────────────────────────────
REGION        = os.getenv("AWS_REGION", "us-east-1")
ROLE_NAME     = "ai-agent-cleanup-lambda-role"
FUNCTION_NAME = "ai-agent-instance-alarm-cleanup"
RULE_NAME     = "ai-agent-instance-terminated"
# ─────────────────────────────────────────────────────────────────────────────

LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

CLEANUP_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": [
            "cloudwatch:DeleteAlarms",
            "sqs:GetQueueUrl",
            "sqs:DeleteQueue",
            "sns:ListTopics",
            "sns:ListSubscriptionsByTopic",
            "sns:Unsubscribe",
            "sns:DeleteTopic",
        ],
        "Resource": "*",
    }],
}

# Lambda handler — embedded so this deploy script is fully self-contained.
# Triggered by EventBridge on EC2 instance termination; deletes the
# per-instance SQS queue, SNS topic, and CloudWatch alarms created by
# instance_alarm_setup.py.
_LAMBDA_HANDLER = """\
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
    cw  = boto3.client("cloudwatch", region_name=REGION)

    deleted = []
    errors  = []

    # Delete CloudWatch alarms
    alarm_names = [f"{prefix}{instance_id}" for prefix in ALARM_PREFIXES]
    try:
        cw.delete_alarms(AlarmNames=alarm_names)
        deleted.append(f"alarms({len(alarm_names)})")
    except ClientError as e:
        errors.append(f"delete_alarms: {e}")

    # Delete SQS queue
    try:
        url = sqs.get_queue_url(QueueName=resource_name)["QueueUrl"]
        sqs.delete_queue(QueueUrl=url)
        deleted.append(f"sqs:{resource_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "AWS.SimpleQueueService.NonExistentQueue":
            errors.append(f"delete_queue: {e}")

    # Delete SNS topic (no get-by-name API — page through topics)
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
"""


def setup_iam_role(iam) -> str:
    print(f"==> Setting up Lambda IAM role: {ROLE_NAME}")
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        print("    Role already exists - skipping creation")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(LAMBDA_TRUST_POLICY),
            Description="Lambda execution role for per-instance alarm cleanup",
        )["Role"]["Arn"]
        print(f"    Created: {arn}")

    iam.attach_role_policy(
        RoleName=ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    print("    Attached: AWSLambdaBasicExecutionRole")

    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="CleanupPermissions",
        PolicyDocument=json.dumps(CLEANUP_POLICY),
    )
    print("    Inline policy applied: CleanupPermissions")
    return arn


def _zip_lambda() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lambda_function.py", _LAMBDA_HANDLER)
    return buf.getvalue()


def deploy() -> None:
    iam = boto3.client("iam")
    lm  = boto3.client("lambda", region_name=REGION)
    eb  = boto3.client("events", region_name=REGION)

    role_arn = setup_iam_role(iam)

    # IAM role must propagate before Lambda can assume it
    print("==> Waiting 10 s for IAM role to propagate...")
    time.sleep(10)

    code = _zip_lambda()

    # Create or update Lambda
    try:
        lm.get_function(FunctionName=FUNCTION_NAME)
        print(f"==> Updating Lambda: {FUNCTION_NAME}")
        lm.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=code)
        function_arn = lm.get_function(FunctionName=FUNCTION_NAME)["Configuration"]["FunctionArn"]
    except lm.exceptions.ResourceNotFoundException:
        print(f"==> Creating Lambda: {FUNCTION_NAME}")
        resp = lm.create_function(
            FunctionName=FUNCTION_NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="lambda_function.handler",
            Code={"ZipFile": code},
            Timeout=30,
            Description="Cleans up per-instance SQS/SNS/alarms on EC2 termination",
        )
        function_arn = resp["FunctionArn"]
        print(f"    ARN: {function_arn}")

    # EventBridge rule: fires on any EC2 instance termination
    print(f"==> Creating EventBridge rule: {RULE_NAME}")
    rule_resp = eb.put_rule(
        Name=RULE_NAME,
        EventPattern=json.dumps({
            "source": ["aws.ec2"],
            "detail-type": ["EC2 Instance State-change Notification"],
            "detail": {"state": ["terminated"]},
        }),
        State="ENABLED",
        Description="Trigger cleanup Lambda when any EC2 instance terminates",
    )
    rule_arn = rule_resp["RuleArn"]
    print(f"    Rule ARN: {rule_arn}")

    # Wire rule -> Lambda
    eb.put_targets(
        Rule=RULE_NAME,
        Targets=[{"Id": "cleanup-lambda", "Arn": function_arn}],
    )

    # Allow EventBridge to invoke the Lambda
    try:
        lm.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId="AllowEventBridgeInvoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
    except lm.exceptions.ResourceConflictException:
        pass  # permission already exists

    print()
    print("Done. Cleanup infrastructure is deployed.")
    print(f"  Lambda:      {function_arn}")
    print(f"  EventBridge: {rule_arn}")


if __name__ == "__main__":
    deploy()
