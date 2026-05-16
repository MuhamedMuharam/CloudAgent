"""
One-shot infrastructure deployment for the cost optimization worker.

Provisions everything needed to run cost_optimization_worker.py as a
weekly ECS Fargate task triggered by EventBridge Scheduler:

  - IAM task role    (CostOptWorkerTaskRole)   - EC2/CW/SSM/ASG permissions
  - IAM exec role    (CostOptWorkerExecRole)   - ECR pull + S3 env file
  - IAM scheduler role (CostOptWorkerSchedulerRole) - lets EB Scheduler run ECS
  - S3 bucket        (costoptimizationworkerbucket) - stores the env file
  - ECR repository   (cost-opt-worker)
  - ECS Fargate cluster (cost-opt-worker-cluster)
  - CloudWatch log group (/ecs/cost-opt-worker, 30-day retention)
  - Security group   (cost-opt-worker-sg) in the default VPC
  - ECS task definition (cost-opt-worker family, placeholder image)
  - EventBridge Scheduler weekly schedule (Sundays 03:00 UTC)

Run once from your workstation (not from the EC2 instance):
    python scripts/deploy_cost_service.py

After this script completes, push the first Docker image with:
    python scripts/update_cost_worker.py   (edit ACCOUNT_ID and IMAGE_TAG first)

Prerequisites:
  - AWS credentials with IAM, ECS, ECR, S3, EventBridge Scheduler, EC2 permissions
  - config/cost_optimization/cost-optimization.worker.env should exist before running
"""

import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

# ── Configurable - change these if you need different names or schedules ─────
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

TASK_ROLE_NAME      = "CostOptWorkerTaskRole"
EXEC_ROLE_NAME      = "CostOptWorkerExecRole"
SCHEDULER_ROLE_NAME = "CostOptWorkerSchedulerRole"

CLUSTER_NAME    = "cost-opt-worker-cluster"
ECR_REPO_NAME   = "cost-opt-worker"
TASK_FAMILY     = "cost-opt-worker"
CONTAINER_NAME  = "cost-opt-worker"
LOG_GROUP       = "/ecs/cost-opt-worker"
SG_NAME         = "cost-opt-worker-sg"

S3_BUCKET       = "costoptimizationworkerbucket"  # no uppercase letters allowed in bucket names
S3_ENV_KEY      = "ecs/env/cost-optimization.env"
LOCAL_ENV_FILE  = "config/cost_optimization/cost-optimization.worker.env"

SCHEDULE_NAME       = "cost-opt-worker-weekly"
SCHEDULE_GROUP      = "default"
SCHEDULE_EXPRESSION = "cron(0 3 ? * SUN *)"    # Sundays 03:00 UTC
# ─────────────────────────────────────────────────────────────────────────────

ECS_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}


def _get_or_create_role(iam, name: str, trust: dict, description: str) -> str:
    try:
        return iam.get_role(RoleName=name)["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
    arn = iam.create_role(
        RoleName=name,
        AssumeRolePolicyDocument=json.dumps(trust),
        Description=description,
    )["Role"]["Arn"]
    print(f"    Created")
    return arn


def setup_task_role(iam) -> str:
    print(f"==> Setting up ECS task role: {TASK_ROLE_NAME}")
    arn = _get_or_create_role(iam, TASK_ROLE_NAME, ECS_TRUST_POLICY,
                              "Cost optimization worker - EC2/CW/SSM/ASG read+write")
    iam.put_role_policy(
        RoleName=TASK_ROLE_NAME,
        PolicyName="CostOptWorkerPolicy",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "EC2Rightsizing",
                    "Effect": "Allow",
                    "Action": [
                        "ec2:DescribeInstances",
                        "ec2:DescribeInstanceStatus",
                        "ec2:DescribeInstanceTypes",
                        "ec2:DescribeImages",
                        "ec2:CreateImage",
                        "ec2:CreateTags",
                        "ec2:StopInstances",
                        "ec2:StartInstances",
                        "ec2:ModifyInstanceAttribute",
                        "ec2:DescribeLaunchTemplates",
                        "ec2:DescribeLaunchTemplateVersions",
                        "ec2:CreateLaunchTemplateVersion",
                        "ec2:ModifyLaunchTemplate",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "CloudWatchRead",
                    "Effect": "Allow",
                    "Action": [
                        "cloudwatch:GetMetricStatistics",
                        "cloudwatch:ListMetrics",
                        "cloudwatch:DescribeAlarms",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "CloudWatchLogsRead",
                    "Effect": "Allow",
                    "Action": [
                        "logs:DescribeLogGroups",
                        "logs:DescribeLogStreams",
                        "logs:FilterLogEvents",
                        "logs:GetLogEvents",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "ComputeOptimizerRead",
                    "Effect": "Allow",
                    "Action": ["compute-optimizer:GetEC2InstanceRecommendations"],
                    "Resource": "*",
                },
                {
                    "Sid": "SSMForWorkerOperations",
                    "Effect": "Allow",
                    "Action": [
                        "ssm:SendCommand",
                        "ssm:GetCommandInvocation",
                        "ssm:ListCommandInvocations",
                        "ssm:ListCommands",
                        "ssm:DescribeInstanceInformation",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "ASGSyncAfterResize",
                    "Effect": "Allow",
                    "Action": [
                        "autoscaling:DescribeAutoScalingGroups",
                        "autoscaling:DescribeAutoScalingInstances",
                        "autoscaling:UpdateAutoScalingGroup",
                    ],
                    "Resource": "*",
                },
            ],
        }),
    )
    print(f"    Inline policy applied: CostOptWorkerPolicy")
    return arn


def setup_exec_role(iam, account_id: str) -> str:
    print(f"==> Setting up ECS execution role: {EXEC_ROLE_NAME}")
    arn = _get_or_create_role(iam, EXEC_ROLE_NAME, ECS_TRUST_POLICY,
                              "Cost optimization worker execution - ECR pull + S3 env file + SSM secrets")
    iam.attach_role_policy(
        RoleName=EXEC_ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
    )
    print("    Attached: AmazonECSTaskExecutionRolePolicy")
    iam.put_role_policy(
        RoleName=EXEC_ROLE_NAME,
        PolicyName="AllowECSAgentToFetchSecrets",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AccessSSMParameters",
                    "Effect": "Allow",
                    "Action": ["ssm:GetParameters"],
                    "Resource": [
                        f"arn:aws:ssm:{REGION}:{account_id}:parameter/cloudagent/cost/prod/*",
                    ],
                },
                {
                    "Sid": "DecryptSecrets",
                    "Effect": "Allow",
                    "Action": ["kms:Decrypt"],
                    "Resource": [f"arn:aws:kms:{REGION}:{account_id}:key/*"],
                    "Condition": {
                        "StringEquals": {"kms:ViaService": f"ssm.{REGION}.amazonaws.com"},
                    },
                },
                {
                    "Sid": "AccessS3EnvFile",
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:GetBucketLocation"],
                    "Resource": [
                        f"arn:aws:s3:::{S3_BUCKET}",
                        f"arn:aws:s3:::{S3_BUCKET}/*",
                    ],
                },
            ],
        }),
    )
    print("    Inline policy applied: AllowECSAgentToFetchSecrets")
    return arn


def setup_scheduler_role(iam, account_id: str, task_role_arn: str, exec_role_arn: str) -> str:
    print(f"==> Setting up EventBridge Scheduler role: {SCHEDULER_ROLE_NAME}")
    scheduler_trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "scheduler.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    arn = _get_or_create_role(iam, SCHEDULER_ROLE_NAME, scheduler_trust,
                              "Allows EventBridge Scheduler to start cost-opt-worker Fargate tasks")
    iam.put_role_policy(
        RoleName=SCHEDULER_ROLE_NAME,
        PolicyName="AllowRunECSTask",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "RunECSTask",
                    "Effect": "Allow",
                    "Action": "ecs:RunTask",
                    "Resource": f"arn:aws:ecs:{REGION}:{account_id}:task-definition/{TASK_FAMILY}:*",
                },
                {
                    "Sid": "PassRoleToECS",
                    "Effect": "Allow",
                    "Action": "iam:PassRole",
                    "Resource": [task_role_arn, exec_role_arn],
                },
            ],
        }),
    )
    print("    Inline policy applied: AllowRunECSTask")
    return arn


def setup_s3(s3) -> str:
    if S3_BUCKET != S3_BUCKET.lower():
        print(f"ERROR: S3 bucket names must be lowercase.", file=sys.stderr)
        print(f"       Change S3_BUCKET to: \"{S3_BUCKET.lower()}\"", file=sys.stderr)
        sys.exit(1)
    print(f"==> Setting up S3 bucket: {S3_BUCKET}")
    try:
        if REGION == "us-east-1":
            s3.create_bucket(Bucket=S3_BUCKET)
        else:
            s3.create_bucket(
                Bucket=S3_BUCKET,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
        s3.put_public_access_block(
            Bucket=S3_BUCKET,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        print(f"    Created and public access blocked")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "BucketAlreadyOwnedByYou":
            print("    Bucket already exists and is yours - skipping creation")
        elif code == "BucketAlreadyExists":
            print(f"ERROR: Bucket name '{S3_BUCKET}' is already taken by another AWS account.", file=sys.stderr)
            print(f"       Change S3_BUCKET at the top of this script to a unique name.", file=sys.stderr)
            sys.exit(1)
        else:
            raise

    if os.path.exists(LOCAL_ENV_FILE):
        s3.upload_file(
            LOCAL_ENV_FILE, S3_BUCKET, S3_ENV_KEY,
            ExtraArgs={"ServerSideEncryption": "AES256"},
        )
        print(f"    Uploaded: {LOCAL_ENV_FILE} → s3://{S3_BUCKET}/{S3_ENV_KEY}")
    else:
        print(f"    WARNING: {LOCAL_ENV_FILE} not found - skipping upload")
        print(f"    Upload manually before running the first task:")
        print(f"    aws s3 cp {LOCAL_ENV_FILE} s3://{S3_BUCKET}/{S3_ENV_KEY} --sse AES256")

    return f"arn:aws:s3:::{S3_BUCKET}/{S3_ENV_KEY}"


def setup_ecr(ecr, account_id: str) -> str:
    repo_uri = f"{account_id}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO_NAME}"
    print(f"==> Setting up ECR repository: {ECR_REPO_NAME}")
    try:
        ecr.create_repository(
            repositoryName=ECR_REPO_NAME,
            imageScanningConfiguration={"scanOnPush": True},
            encryptionConfiguration={"encryptionType": "AES256"},
        )
        print(f"    Created: {repo_uri}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "RepositoryAlreadyExistsException":
            print("    Repository already exists - skipping")
        else:
            raise
    return repo_uri


def setup_cluster(ecs) -> str:
    print(f"==> Setting up ECS cluster: {CLUSTER_NAME}")
    # create_cluster is idempotent - returns existing cluster if it already exists
    resp = ecs.create_cluster(
        clusterName=CLUSTER_NAME,
        capacityProviders=["FARGATE", "FARGATE_SPOT"],
        defaultCapacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}],
    )
    cluster_arn = resp["cluster"]["clusterArn"]
    print(f"    ARN: {cluster_arn}")
    return cluster_arn


def setup_log_group(logs) -> None:
    print(f"==> Setting up CloudWatch log group: {LOG_GROUP}")
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)
        print("    Created with 30-day retention")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceAlreadyExistsException":
            print("    Log group already exists - skipping")
        else:
            raise


def discover_default_vpc_subnets(ec2_client) -> tuple[str, list[str]]:
    print("==> Discovering default VPC subnets")
    vpcs = ec2_client.describe_vpcs(
        Filters=[{"Name": "isDefault", "Values": ["true"]}]
    )["Vpcs"]
    if not vpcs:
        print("ERROR: No default VPC found.", file=sys.stderr)
        print("       Set VPC_ID and subnet IDs manually in this script.", file=sys.stderr)
        sys.exit(1)
    vpc_id = vpcs[0]["VpcId"]
    subnets = ec2_client.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )["Subnets"]
    subnet_ids = [s["SubnetId"] for s in subnets]
    print(f"    VPC: {vpc_id}")
    print(f"    Subnets: {subnet_ids}")
    return vpc_id, subnet_ids


def get_or_create_security_group(ec2_client, vpc_id: str) -> str:
    print(f"==> Resolving security group for VPC {vpc_id}")

    # Prefer the VPC's default SG - every VPC has one
    default_sgs = ec2_client.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": ["default"]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    if default_sgs:
        sg_id = default_sgs[0]["GroupId"]
        print(f"    Using default security group: {sg_id}")
        return sg_id

    # No default SG found - create a dedicated one
    print(f"    Default SG not found - creating {SG_NAME}")
    sg_id = ec2_client.create_security_group(
        GroupName=SG_NAME,
        Description="Outbound-only SG for cost-opt-worker Fargate tasks",
        VpcId=vpc_id,
    )["GroupId"]
    print(f"    Created: {sg_id}")
    return sg_id


def register_task_definition(
    ecs,
    task_role_arn: str,
    exec_role_arn: str,
    repo_uri: str,
    env_file_arn: str,
) -> str:
    placeholder_image = f"{repo_uri}:latest"
    print(f"==> Registering ECS task definition: {TASK_FAMILY}")
    print(f"    Placeholder image: {placeholder_image}")
    print(f"    Run deploy_cost_worker.ps1 to push the real image and update this definition.")
    resp = ecs.register_task_definition(
        family=TASK_FAMILY,
        taskRoleArn=task_role_arn,
        executionRoleArn=exec_role_arn,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="512",
        memory="1024",
        containerDefinitions=[{
            "name": CONTAINER_NAME,
            "image": placeholder_image,
            "command": ["python", "cost_optimization_worker.py"],
            "essential": True,
            "environmentFiles": [{"type": "s3", "value": env_file_arn}],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": LOG_GROUP,
                    "awslogs-region": REGION,
                    "awslogs-stream-prefix": "ecs",
                },
            },
        }],
    )
    task_def_arn = resp["taskDefinition"]["taskDefinitionArn"]
    print(f"    Registered: {task_def_arn}")
    return task_def_arn


def setup_scheduler(
    scheduler_client,
    task_def_arn: str,
    scheduler_role_arn: str,
    cluster_arn: str,
    subnet_ids: list[str],
    sg_id: str,
) -> None:
    print(f"==> Setting up EventBridge Scheduler: {SCHEDULE_NAME}")
    try:
        scheduler_client.create_schedule(
            Name=SCHEDULE_NAME,
            GroupName=SCHEDULE_GROUP,
            ScheduleExpression=SCHEDULE_EXPRESSION,
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            State="ENABLED",
            Description="Weekly cost optimization worker (Fargate)",
            Target={
                "Arn": cluster_arn,
                "RoleArn": scheduler_role_arn,
                "EcsParameters": {
                    "TaskDefinitionArn": task_def_arn,
                    "TaskCount": 1,
                    "LaunchType": "FARGATE",
                    "NetworkConfiguration": {
                        "awsvpcConfiguration": {
                            "Subnets": subnet_ids,
                            "SecurityGroups": [sg_id],
                            "AssignPublicIp": "ENABLED",
                        },
                    },
                },
                "RetryPolicy": {
                    "MaximumRetryAttempts": 2,
                    "MaximumEventAgeInSeconds": 3600,
                },
            },
        )
        print(f"    Created: {SCHEDULE_NAME} ({SCHEDULE_EXPRESSION} UTC)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConflictException":
            print("    Schedule already exists - skipping")
        else:
            raise


def main() -> None:
    sts = boto3.client("sts", region_name=REGION)
    identity = sts.get_caller_identity()
    account_id = identity["Account"]
    caller_arn = identity["Arn"]

    print(f"==> Running as : {caller_arn}")
    print(f"==> Account ID : {account_id}")
    print(f"==> Region     : {REGION}")
    print()

    iam       = boto3.client("iam")
    s3        = boto3.client("s3", region_name=REGION)
    ecr       = boto3.client("ecr", region_name=REGION)
    ecs       = boto3.client("ecs", region_name=REGION)
    logs      = boto3.client("logs", region_name=REGION)
    ec2_c     = boto3.client("ec2", region_name=REGION)
    scheduler = boto3.client("scheduler", region_name=REGION)

    task_role_arn = setup_task_role(iam)
    exec_role_arn = setup_exec_role(iam, account_id)

    # IAM roles take a few seconds to propagate before ECS/Scheduler can reference them
    print("==> Waiting 10 s for IAM roles to propagate...")
    time.sleep(10)

    scheduler_role_arn = setup_scheduler_role(iam, account_id, task_role_arn, exec_role_arn)

    env_file_arn = setup_s3(s3)
    repo_uri     = setup_ecr(ecr, account_id)
    cluster_arn  = setup_cluster(ecs)
    setup_log_group(logs)

    vpc_id, subnet_ids = discover_default_vpc_subnets(ec2_c)
    sg_id = get_or_create_security_group(ec2_c, vpc_id)

    task_def_arn = register_task_definition(ecs, task_role_arn, exec_role_arn, repo_uri, env_file_arn)

    # Strip the revision suffix (:N) so the Scheduler always runs the latest active revision.
    # Full ARN format: arn:aws:ecs:region:account:task-definition/family:N
    task_def_family_arn = task_def_arn.rsplit(":", 1)[0]

    # Brief pause before Scheduler validates the roles it will use
    time.sleep(5)
    setup_scheduler(scheduler, task_def_family_arn, scheduler_role_arn, cluster_arn, subnet_ids, sg_id)

    print()
    print("=" * 65)
    print("Done. Cost optimization worker infrastructure is deployed.")
    print("=" * 65)
    print()
    print(f"  ECS Cluster     : {cluster_arn}")
    print(f"  Task Definition : {task_def_arn}")
    print(f"  ECR Repository  : {repo_uri}")
    print(f"  S3 Env File     : s3://{S3_BUCKET}/{S3_ENV_KEY}")
    print(f"  Schedule        : {SCHEDULE_NAME} ({SCHEDULE_EXPRESSION} UTC)")
    print()
    print("Next steps:")
    print("  1. Open scripts/update_cost_worker.py and verify these variables match:")
    print(f"       ACCOUNT_ID  = \"{account_id}\"")
    print(f"       ECR_REPO    = \"{ECR_REPO_NAME}\"")
    print(f"       TASK_FAMILY = \"{TASK_FAMILY}\"")
    print(f"     Set IMAGE_TAG to your desired tag (e.g. \"v1\"), then run:")
    print(f"     python scripts/update_cost_worker.py")
    print()
    print("  2. Run one manual task to validate before the schedule fires:")
    print(f"     aws ecs run-task --cluster {CLUSTER_NAME} --task-definition {TASK_FAMILY} \\")
    print(f"       --launch-type FARGATE --region {REGION} \\")
    print(f"       --network-configuration 'awsvpcConfiguration={{subnets=[{subnet_ids[0]}],securityGroups=[{sg_id}],assignPublicIp=ENABLED}}'")
    print()
    print("  3. Monitor logs:")
    print(f"     aws logs tail {LOG_GROUP} --follow --region {REGION}")


if __name__ == "__main__":
    main()
