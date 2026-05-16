"""
Re-deploy the cost optimization worker: build a new Docker image, push it to ECR,
and register a new ECS task definition revision pointing to the new image.

Does NOT update the EventBridge Scheduler — the schedule targets the task definition
family and ECS always resolves to the latest active revision automatically.

Usage: python scripts/update_cost_worker.py
Edit the variables below before running.
"""

import base64
import subprocess
import sys
from datetime import datetime

import boto3

# ── Edit these before running ────────────────────────────────────────────────
ACCOUNT_ID   = "YOUR_ACCOUNT_ID"
REGION       = "us-east-1"
ECR_REPO     = "cost-opt-worker"
TASK_FAMILY  = "cost-opt-worker"
IMAGE_TAG    = "v1"
LOCAL_IMAGE  = "cost-opt-worker:local"

# Set to True and fill S3_URI to also upload an updated env file to S3
UPLOAD_ENV   = False
ENV_FILE     = "config/cost_optimization/cost-optimization.worker.env"
ENV_S3_URI   = "s3://costoptimizationworkerbucket/ecs/env/cost-optimization.env"  #costoptimizationworkerbucket is the name of the bucket we created in deploy_cost_service.py , change if you used a different name
# ─────────────────────────────────────────────────────────────────────────────


def _run(cmd: list[str], step: str) -> None:
    print(f"    $ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"ERROR: {step} failed (exit {result.returncode})", file=sys.stderr)
        sys.exit(result.returncode)


def docker_login(ecr_client, registry: str) -> None:
    print(f"==> Logging in to ECR: {registry}")
    token = ecr_client.get_authorization_token()
    encoded = token["authorizationData"][0]["authorizationToken"]
    password = base64.b64decode(encoded).decode().split(":", 1)[1]
    proc = subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=password.encode(),
    )
    if proc.returncode != 0:
        print("ERROR: docker login failed", file=sys.stderr)
        sys.exit(proc.returncode)


def upload_env_file(s3_client, local_path: str, s3_uri: str) -> None:
    parts = s3_uri[5:].split("/", 1)
    bucket, key = parts[0], (parts[1] if len(parts) > 1 else "")
    print(f"==> Uploading env file: {local_path} -> {s3_uri}")
    s3_client.upload_file(local_path, bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
    print("    Done")


def register_new_revision(ecs_client, task_family: str, new_image: str) -> str:
    print(f"==> Fetching current task definition: {task_family}")
    resp = ecs_client.describe_task_definition(taskDefinition=task_family, include=["TAGS"])
    task_def = resp["taskDefinition"]
    tags = resp.get("tags", [])

    containers = task_def.get("containerDefinitions", [])
    if not containers:
        print(f"ERROR: No container definitions found in {task_family}", file=sys.stderr)
        sys.exit(1)
    containers[0]["image"] = new_image
    print(f"    Container image set to: {new_image}")

    optional_fields = [
        "taskRoleArn", "executionRoleArn", "networkMode",
        "volumes", "placementConstraints", "requiresCompatibilities",
        "cpu", "memory", "runtimePlatform", "proxyConfiguration",
        "inferenceAccelerators", "ephemeralStorage", "pidMode", "ipcMode",
    ]
    always_list = {"requiresCompatibilities", "volumes", "placementConstraints", "inferenceAccelerators"}

    payload: dict = {"family": task_def["family"], "containerDefinitions": containers}
    for field in optional_fields:
        value = task_def.get(field)
        if value is None or value == "" or value == []:
            continue
        if field in always_list and not isinstance(value, list):
            value = [value]
        payload[field] = value

    if tags:
        payload["tags"] = tags

    print("==> Registering new task definition revision")
    new_arn = ecs_client.register_task_definition(**payload)["taskDefinition"]["taskDefinitionArn"]
    print(f"    Registered: {new_arn}")
    return new_arn


def main() -> None:
    registry  = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
    ecr_image = f"{registry}/{ECR_REPO}:{IMAGE_TAG}"

    print(f"==> Account     : {ACCOUNT_ID}")
    print(f"==> Region      : {REGION}")
    print(f"==> ECR image   : {ecr_image}")
    print(f"==> Task family : {TASK_FAMILY}")
    print()

    ecr_client = boto3.client("ecr", region_name=REGION)
    ecs_client = boto3.client("ecs", region_name=REGION)
    s3_client  = boto3.client("s3",  region_name=REGION)

    print(f"==> Building Docker image: {LOCAL_IMAGE}")
    _run(["docker", "build", "-t", LOCAL_IMAGE, "."], "docker build")

    docker_login(ecr_client, registry)

    print(f"==> Tagging: {LOCAL_IMAGE} -> {ecr_image}")
    _run(["docker", "tag", LOCAL_IMAGE, ecr_image], "docker tag")

    print("==> Pushing to ECR")
    _run(["docker", "push", ecr_image], "docker push")

    if UPLOAD_ENV:
        upload_env_file(s3_client, ENV_FILE, ENV_S3_URI)

    new_arn = register_new_revision(ecs_client, TASK_FAMILY, ecr_image)

    print()
    print("Done.")
    print(f"  Image pushed    : {ecr_image}")
    print(f"  Task definition : {new_arn}")
    print()
    print("Validate with a manual run:")
    print(f"  aws ecs run-task --cluster cost-opt-worker-cluster \\")
    print(f"    --task-definition {TASK_FAMILY} --launch-type FARGATE --region {REGION} \\")
    print(f"    --network-configuration 'awsvpcConfiguration={{subnets=[<subnet-id>],securityGroups=[<sg-id>],assignPublicIp=ENABLED}}'")
    print()
    print(f"Monitor logs:")
    print(f"  aws logs tail /ecs/cost-opt-worker --follow --region {REGION}")


if __name__ == "__main__":
    main()
