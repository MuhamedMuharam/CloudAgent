"""
One-shot deployment script: creates the Lambda + EventBridge rule that
cleans up per-instance SQS/SNS/alarms when an EC2 instance terminates.

Run once from your workstation (not from the instance):
    python scripts/deploy_cleanup_lambda.py

Requirements:
  - AWS credentials with IAM, Lambda, and EventBridge permissions
  - The cleanup Lambda code at scripts/instance_alarm_cleanup.py
"""

import io
import json
import os
import sys
import zipfile

import boto3

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
FUNCTION_NAME = "ai-agent-instance-alarm-cleanup"
RULE_NAME = "ai-agent-instance-terminated"
# Role ARN must exist; create it manually or via CloudFormation before running.
# Needs: AWSLambdaBasicExecutionRole + the inline policy below.
LAMBDA_ROLE_ARN = os.getenv("CLEANUP_LAMBDA_ROLE_ARN", "")

INLINE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
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
        }
    ],
}


def _zip_lambda() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        src = os.path.join(os.path.dirname(__file__), "instance_alarm_cleanup.py")
        zf.write(src, "lambda_function.py")
    return buf.getvalue()


def deploy() -> None:
    if not LAMBDA_ROLE_ARN:
        print(
            "ERROR: set CLEANUP_LAMBDA_ROLE_ARN to the ARN of the Lambda execution role.\n"
            "The role needs AWSLambdaBasicExecutionRole plus this inline policy:\n"
            + json.dumps(INLINE_POLICY, indent=2),
            file=sys.stderr,
        )
        sys.exit(1)

    lm = boto3.client("lambda", region_name=REGION)
    eb = boto3.client("events", region_name=REGION)

    code = _zip_lambda()

    # --- Create or update Lambda ---
    try:
        lm.get_function(FunctionName=FUNCTION_NAME)
        print(f"Updating Lambda: {FUNCTION_NAME}")
        lm.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=code)
    except lm.exceptions.ResourceNotFoundException:
        print(f"Creating Lambda: {FUNCTION_NAME}")
        resp = lm.create_function(
            FunctionName=FUNCTION_NAME,
            Runtime="python3.12",
            Role=LAMBDA_ROLE_ARN,
            Handler="lambda_function.handler",
            Code={"ZipFile": code},
            Timeout=30,
            Description="Cleans up per-instance SQS/SNS/alarms on EC2 termination",
        )
        function_arn = resp["FunctionArn"]
        print(f"  ARN: {function_arn}")
    else:
        function_arn = lm.get_function(FunctionName=FUNCTION_NAME)["Configuration"]["FunctionArn"]

    # --- EventBridge rule: fires on any EC2 instance termination ---
    print(f"Creating EventBridge rule: {RULE_NAME}")
    rule_resp = eb.put_rule(
        Name=RULE_NAME,
        EventPattern=json.dumps(
            {
                "source": ["aws.ec2"],
                "detail-type": ["EC2 Instance State-change Notification"],
                "detail": {"state": ["terminated"]},
            }
        ),
        State="ENABLED",
        Description="Trigger cleanup Lambda when any EC2 instance terminates",
    )
    rule_arn = rule_resp["RuleArn"]
    print(f"  Rule ARN: {rule_arn}")

    # --- Wire rule → Lambda ---
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

    print("Done. Cleanup infrastructure is deployed.")
    print(f"  Lambda:       {function_arn}")
    print(f"  EventBridge:  {rule_arn}")
    print()
    print("IMPORTANT: add these permissions to the EC2-CloudWatch-Agent-Role instance profile")
    print("so instances can run instance_alarm_setup.py:")
    print(json.dumps(INLINE_POLICY, indent=2))


if __name__ == "__main__":
    deploy()
