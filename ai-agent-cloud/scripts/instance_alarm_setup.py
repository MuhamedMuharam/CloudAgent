"""
Per-instance alarm provisioning script.

Runs as a systemd oneshot service (ai-agent-setup.service) BEFORE
ai-agent.service starts on every new ASG instance boot.

Creates:
  - A dedicated SQS queue  (ai-agent-alarms-<short-id>)
  - A dedicated SNS topic   (ai-agent-alarms-<short-id>)
  - SNS → SQS subscription with the correct queue policy
  - Per-instance CloudWatch alarms → SNS topic

Then patches the .env file with the new ALARM_SQS_QUEUE_URL so the
alarm worker on this instance only receives its own alarm notifications.

Environment variables (all optional, have defaults):
  AWS_DEFAULT_REGION   - AWS region (default: us-east-1)
  ENV_FILE_PATH        - path to the .env file to patch
                         (default: /home/ec2-user/ai-agent-cloud/.env)
  AGENT_IAM_USER_ARN   - ARN of the ai-agent IAM user that polls SQS
                         e.g. arn:aws:iam::123456789012:user/ai-agent
  DISK_DEVICE          - device name for disk alarm dimension
                         (default: nvme0n1p1 for NVMe; set to xvda1 for older HVM)
  DISK_FSTYPE          - filesystem type for disk alarm dimension (default: xfs)
  DISK_PATH            - mount path for disk alarm dimension (default: /)
"""

import json
import os
import re
import sys
import urllib.request

from dotenv import load_dotenv

# Load .env first so AGENT_IAM_USER_ARN and other non-credential vars are available,
# then clear the AWS credential keys so boto3 uses the EC2 instance profile instead.
load_dotenv()
for _var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ.pop(_var, None)

import boto3
from botocore.exceptions import ClientError

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
ENV_FILE = os.getenv("ENV_FILE_PATH", "/home/ec2-user/CloudAgent/ai-agent-cloud/.env")
AGENT_IAM_USER_ARN = os.getenv("AGENT_IAM_USER_ARN", "")
DISK_DEVICE = os.getenv("DISK_DEVICE", "nvme0n1p1")
DISK_FSTYPE = os.getenv("DISK_FSTYPE", "xfs")
DISK_PATH = os.getenv("DISK_PATH", "/")


def _log(msg: str) -> None:
    print(f"[instance-alarm-setup] {msg}", flush=True)


def get_instance_id() -> str:
    """Fetch instance ID from EC2 IMDS v2."""
    token_req = urllib.request.Request(
        "http://169.254.169.254/latest/api/token",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        method="PUT",
    )
    with urllib.request.urlopen(token_req, timeout=5) as r:
        token = r.read().decode()

    meta_req = urllib.request.Request(
        "http://169.254.169.254/latest/meta-data/instance-id",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with urllib.request.urlopen(meta_req, timeout=5) as r:
        return r.read().decode().strip()


def create_sqs_queue(sqs, name: str) -> tuple[str, str]:
    """Create (or re-use) SQS queue. Returns (queue_url, queue_arn)."""
    try:
        resp = sqs.create_queue(
            QueueName=name,
            Attributes={
                "VisibilityTimeout": "300",
                "MessageRetentionPeriod": "86400",  # 1 day
            },
        )
        queue_url = resp["QueueUrl"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "QueueAlreadyExists":
            queue_url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
        else:
            raise

    attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
    return queue_url, attrs["Attributes"]["QueueArn"]


def create_sns_topic(sns, name: str) -> str:
    """Create (or re-use) SNS topic. Returns topic ARN."""
    return sns.create_topic(Name=name)["TopicArn"]


def wire_sns_to_sqs(sns, sqs, topic_arn: str, queue_arn: str, queue_url: str) -> None:
    """Subscribe SQS to SNS and set the queue policy to allow SNS delivery and agent polling."""
    sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)

    statements = [
        {
            "Sid": "AllowSNSPublish",
            "Effect": "Allow",
            "Principal": {"Service": "sns.amazonaws.com"},
            "Action": "sqs:SendMessage",
            "Resource": queue_arn,
            "Condition": {"ArnEquals": {"aws:SourceArn": topic_arn}},
        }
    ]

    if AGENT_IAM_USER_ARN:
        statements.append({
            "Sid": "AllowAgentToReceive",
            "Effect": "Allow",
            "Principal": {"AWS": AGENT_IAM_USER_ARN},
            "Action": [
                "sqs:ReceiveMessage",
                "sqs:DeleteMessage",
                "sqs:GetQueueAttributes",
                "sqs:ChangeMessageVisibility",
            ],
            "Resource": queue_arn,
        })
    else:
        _log("WARNING: AGENT_IAM_USER_ARN not set — agent will not be able to poll this queue")

    sqs.set_queue_attributes(
        QueueUrl=queue_url,
        Attributes={"Policy": json.dumps({"Version": "2012-10-17", "Statement": statements})},
    )


def create_alarms(cw, instance_id: str, topic_arn: str) -> list[str]:
    """Create per-instance metric alarms. Returns list of alarm names created."""

    alarm_defs = [
        {
            "AlarmName": f"CPUUtilizationAlarm-{instance_id}",
            "MetricName": "CPUUtilization",
            "Namespace": "AWS/EC2",
            "Statistic": "Average",
            "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
            "Period": 300,
            "EvaluationPeriods": 1,
            "Threshold": 70.0,
            "ComparisonOperator": "GreaterThanThreshold",
            "AlarmDescription": f"CPU > 70% on {instance_id}",
        },
        {
            "AlarmName": f"DiskPressureAlarm-{instance_id}",
            "MetricName": "disk_used_percent",
            "Namespace": "CWAgent",
            "Statistic": "Average",
            "Dimensions": [
                {"Name": "InstanceId", "Value": instance_id},
                {"Name": "path", "Value": DISK_PATH},
                {"Name": "device", "Value": DISK_DEVICE},
                {"Name": "fstype", "Value": DISK_FSTYPE},
            ],
            "Period": 300,
            "EvaluationPeriods": 2,
            "Threshold": 85.0,
            "ComparisonOperator": "GreaterThanThreshold",
            "AlarmDescription": f"Disk > 85% on {instance_id}",
        },
        {
            "AlarmName": f"MemWarningAlarm-{instance_id}",
            "MetricName": "mem_used_percent",
            "Namespace": "CWAgent",
            "Statistic": "Average",
            "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
            "Period": 60,
            "EvaluationPeriods": 5,
            "Threshold": 85.0,
            "ComparisonOperator": "GreaterThanThreshold",
            "AlarmDescription": f"Memory > 85% on {instance_id}",
        },
        {
            "AlarmName": f"FailedInstanceAlarm-{instance_id}",
            "MetricName": "StatusCheckFailed_Instance",
            "Namespace": "AWS/EC2",
            "Statistic": "Maximum",
            "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
            "Period": 60,
            "EvaluationPeriods": 2,
            "Threshold": 0,
            "ComparisonOperator": "GreaterThanThreshold",
            "AlarmDescription": f"Instance status check failed on {instance_id}",
        },
        {
            "AlarmName": f"SystemFailureAlarm-{instance_id}",
            "MetricName": "StatusCheckFailed_System",
            "Namespace": "AWS/EC2",
            "Statistic": "Maximum",
            "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
            "Period": 60,
            "EvaluationPeriods": 2,
            "Threshold": 0,
            "ComparisonOperator": "GreaterThanThreshold",
            "AlarmDescription": f"System status check failed on {instance_id}",
        },
    ]

    names = []
    for defn in alarm_defs:
        cw.put_metric_alarm(
            **defn,
            AlarmActions=[topic_arn],
            OKActions=[topic_arn],
            TreatMissingData="notBreaching",
        )
        names.append(defn["AlarmName"])

    return names


def patch_env_file(env_path: str, queue_url: str) -> None:
    """Write or replace ALARM_SQS_QUEUE_URL in the .env file."""
    try:
        with open(env_path) as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    key = "ALARM_SQS_QUEUE_URL"
    new_line = f"{key}={queue_url}"

    if re.search(rf"^{key}=.*$", content, re.MULTILINE):
        content = re.sub(rf"^{key}=.*$", new_line, content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{new_line}\n"

    with open(env_path, "w") as f:
        f.write(content)


def main() -> None:
    _log("Starting per-instance alarm setup")

    instance_id = get_instance_id()
    _log(f"Instance ID: {instance_id}")

    # Use the short hex suffix to stay within the 80-char SQS/SNS name limit.
    short_id = instance_id.replace("i-", "")
    resource_name = f"ai-agent-alarms-{short_id}"

    sqs = boto3.client("sqs", region_name=REGION)
    sns = boto3.client("sns", region_name=REGION)
    cw = boto3.client("cloudwatch", region_name=REGION)

    _log(f"Creating SQS queue: {resource_name}")
    queue_url, queue_arn = create_sqs_queue(sqs, resource_name)
    _log(f"  Queue URL: {queue_url}")

    _log(f"Creating SNS topic: {resource_name}")
    topic_arn = create_sns_topic(sns, resource_name)
    _log(f"  Topic ARN: {topic_arn}")

    _log("Subscribing SQS to SNS")
    wire_sns_to_sqs(sns, sqs, topic_arn, queue_arn, queue_url)

    _log("Creating CloudWatch alarms")
    alarm_names = create_alarms(cw, instance_id, topic_arn)
    _log(f"  Created: {alarm_names}")

    _log(f"Patching env file: {ENV_FILE}")
    patch_env_file(ENV_FILE, queue_url)

    _log("Setup complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[instance-alarm-setup] FATAL: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
