"""
One-time IAM bootstrap for the AI Agent Cloud framework.
Run once from any machine with AWS credentials configured as an IAM admin.

Creates:
  1. ai-agent IAM user  — used by the alarm worker on EC2 instances
  2. EC2AgentRole       — EC2 instance role attached to every agent instance

Usage: python scripts/setup_iam.py
"""

import json

import boto3
from botocore.exceptions import ClientError

USER_NAME = "ai-agent"
ROLE_NAME = "EC2AgentRole"
INSTANCE_PROFILE_NAME = ROLE_NAME

USER_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess",
    "arn:aws:iam::aws:policy/AmazonEC2FullAccess",
    "arn:aws:iam::aws:policy/AWSXrayReadOnlyAccess",
    "arn:aws:iam::aws:policy/CloudWatchLogsReadOnlyAccess",
    "arn:aws:iam::aws:policy/ComputeOptimizerReadOnlyAccess",
]

ROLE_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess",
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
]

EC2_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}


def _create_or_skip_user(iam) -> None:
    try:
        iam.get_user(UserName=USER_NAME)
        print("    User already exists — skipping creation")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_user(UserName=USER_NAME)
        print("    Created")


def setup_iam_user(iam, account_id: str, region: str) -> None:
    print(f"==> Creating IAM user: {USER_NAME}")
    _create_or_skip_user(iam)

    print(f"==> Attaching managed policies to {USER_NAME}")
    for arn in USER_MANAGED_POLICIES:
        iam.attach_user_policy(UserName=USER_NAME, PolicyArn=arn)
        print(f"    Attached: {arn.split('/')[-1]}")

    print("==> Putting inline policy: AIAgent-ASG-LaunchTemplate-PassRole")
    iam.put_user_policy(
        UserName=USER_NAME,
        PolicyName="AIAgent-ASG-LaunchTemplate-PassRole",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowPassOnlySpecificEc2InstanceRoles",
                    "Effect": "Allow",
                    "Action": "iam:PassRole",
                    "Resource": f"arn:aws:iam::{account_id}:role/{ROLE_NAME}",
                    "Condition": {
                        "StringEquals": {
                            "iam:PassedToService": "ec2.amazonaws.com"
                        }
                    },
                }
            ],
        }),
    )

    print("==> Putting inline policy: AllowAgentToRunSSMCommands")
    iam.put_user_policy(
        UserName=USER_NAME,
        PolicyName="AllowAgentToRunSSMCommands",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "RunCommandOnTargetInstance",
                    "Effect": "Allow",
                    "Action": ["ssm:SendCommand"],
                    "Resource": [
                        f"arn:aws:ec2:{region}:{account_id}:instance/*",
                        f"arn:aws:ssm:{region}::document/AWS-RunShellScript",
                    ],
                },
                {
                    "Sid": "ReadCommandResults",
                    "Effect": "Allow",
                    "Action": [
                        "ssm:GetCommandInvocation",
                        "ssm:ListCommands",
                        "ssm:ListCommandInvocations",
                        "ssm:DescribeInstanceInformation",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "ManageParameterStore",
                    "Effect": "Allow",
                    "Action": [
                        "ssm:PutParameter",
                        "ssm:GetParameter",
                        "ssm:GetParameters",
                        "ssm:GetParametersByPath",
                    ],
                    "Resource": f"arn:aws:ssm:{region}:{account_id}:parameter/aicloudpilot/cost/prod/*",
                },
            ],
        }),
    )

    print()
    keys = iam.list_access_keys(UserName=USER_NAME)["AccessKeyMetadata"]
    if keys:
        print(f"==> Access key already exists for {USER_NAME} — skipping creation")
        print(f"    To rotate: aws iam delete-access-key --user-name {USER_NAME} --access-key-id {keys[0]['AccessKeyId']}")
    else:
        print(f"==> Creating access key for {USER_NAME}")
        key = iam.create_access_key(UserName=USER_NAME)["AccessKey"]
        print()
        print("┌─────────────────────────────────────────────────────────────┐")
        print("│  SAVE THESE — the secret key is shown only once             │")
        print("├─────────────────────────────────────────────────────────────┤")
        print(f"│  AWS_ACCESS_KEY_ID     = {key['AccessKeyId']}")
        print(f"│  AWS_SECRET_ACCESS_KEY = {key['SecretAccessKey']}")
        print("└─────────────────────────────────────────────────────────────┘")
        print()
        print("  Add these to .env on every instance, or let setup_alarm_worker.sh")
        print("  prompt for them when it writes ~/.aws/credentials.")


def setup_ec2_role(iam, account_id: str) -> None:
    print(f"==> Creating EC2 role: {ROLE_NAME}")
    try:
        iam.get_role(RoleName=ROLE_NAME)
        print("    Role already exists — skipping creation")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(EC2_TRUST_POLICY),
            Description=(
                "Attached to every AI Agent EC2 instance: "
                "CloudWatch agent, X-Ray daemon, SSM, and per-instance alarm provisioning"
            ),
        )
        print("    Created")

    print(f"==> Attaching managed policies to {ROLE_NAME}")
    for arn in ROLE_MANAGED_POLICIES:
        iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=arn)
        print(f"    Attached: {arn.split('/')[-1]}")

    print("==> Putting inline policy: AllowCreationSQS-SNS-Alarms")
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="AllowCreationSQS-SNS-Alarms",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "AllowEC2SetupService",
                "Effect": "Allow",
                "Action": [
                    "sqs:CreateQueue",
                    "sqs:GetQueueUrl",
                    "sqs:GetQueueAttributes",
                    "sqs:SetQueueAttributes",
                    "sqs:DeleteQueue",
                    "sns:CreateTopic",
                    "sns:Subscribe",
                    "sns:DeleteTopic",
                    "sns:ListSubscriptionsByTopic",
                    "sns:Unsubscribe",
                    "cloudwatch:PutMetricAlarm",
                    "cloudwatch:DeleteAlarms",
                ],
                "Resource": "*",
            }],
        }),
    )

    print(f"==> Creating instance profile: {INSTANCE_PROFILE_NAME}")
    try:
        iam.get_instance_profile(InstanceProfileName=INSTANCE_PROFILE_NAME)
        print("    Instance profile already exists — skipping")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_instance_profile(InstanceProfileName=INSTANCE_PROFILE_NAME)
        iam.add_role_to_instance_profile(
            InstanceProfileName=INSTANCE_PROFILE_NAME,
            RoleName=ROLE_NAME,
        )
        print("    Created and linked to role")


def main() -> None:
    sts = boto3.client("sts")
    identity = sts.get_caller_identity()
    account_id = identity["Account"]
    caller_arn = identity["Arn"]
    region = boto3.session.Session().region_name or "us-east-1"

    print(f"==> Running as : {caller_arn}")
    print(f"==> Account ID : {account_id}")
    print(f"==> Region     : {region}")
    print()

    iam = boto3.client("iam")
    setup_iam_user(iam, account_id, region)
    setup_ec2_role(iam, account_id)

    print()
    print("Done.")
    print()
    print(f"  IAM user ARN  : arn:aws:iam::{account_id}:user/{USER_NAME}")
    print(f"  EC2 role ARN  : arn:aws:iam::{account_id}:role/{ROLE_NAME}")
    print()
    print("Next steps:")
    print(f"  1. Set AGENT_IAM_USER_ARN=arn:aws:iam::{account_id}:user/{USER_NAME} in .env")
    print(f"  2. Attach {INSTANCE_PROFILE_NAME} to every AI Agent EC2 instance")
    print("     (EC2 console → Instance → Actions → Security → Modify IAM role)")


if __name__ == "__main__":
    main()
