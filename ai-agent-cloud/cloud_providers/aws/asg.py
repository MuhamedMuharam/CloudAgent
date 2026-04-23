"""
AWS Auto Scaling Group (ASG) and Launch Template Manager
Handles ASG lifecycle, launch template versioning, and scaling policies.
"""

import boto3
from botocore.exceptions import ClientError
from typing import Dict, List, Optional


class ASGManager:
    """Manages AWS Auto Scaling Groups and Launch Templates."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.ec2 = boto3.client("ec2", region_name=region)
        self.autoscaling = boto3.client("autoscaling", region_name=region)
        self.cloudwatch = boto3.client("cloudwatch", region_name=region)

    # ─────────────────────────── Launch Templates ─────────────────────────────

    def list_launch_templates(self, name_filter: Optional[str] = None) -> Dict:
        """List all launch templates, optionally filtered by name substring."""
        try:
            kwargs: Dict = {}
            if name_filter:
                kwargs["Filters"] = [
                    {"Name": "launch-template-name", "Values": [f"*{name_filter}*"]}
                ]
            paginator = self.ec2.get_paginator("describe_launch_templates")
            templates = []
            for page in paginator.paginate(**kwargs):
                for lt in page.get("LaunchTemplates", []):
                    templates.append({
                        "launch_template_id": lt["LaunchTemplateId"],
                        "launch_template_name": lt["LaunchTemplateName"],
                        "default_version_number": lt["DefaultVersionNumber"],
                        "latest_version_number": lt["LatestVersionNumber"],
                        "created_by": lt.get("CreatedBy"),
                        "tags": {t["Key"]: t["Value"] for t in lt.get("Tags", [])},
                    })
            return {"success": True, "launch_templates": templates, "count": len(templates)}
        except ClientError as e:
            return {"success": False, "error": str(e)}

    def describe_launch_template(
        self,
        launch_template_id: Optional[str] = None,
        launch_template_name: Optional[str] = None,
        versions: Optional[List[str]] = None,
    ) -> Dict:
        """Get details and selected versions of a launch template."""
        try:
            if not launch_template_id and not launch_template_name:
                return {"success": False, "error": "provide launch_template_id or launch_template_name"}

            lt_kwargs: Dict = {}
            if launch_template_id:
                lt_kwargs["LaunchTemplateIds"] = [launch_template_id]
            else:
                lt_kwargs["LaunchTemplateNames"] = [launch_template_name]

            lt_resp = self.ec2.describe_launch_templates(**lt_kwargs)
            lts = lt_resp.get("LaunchTemplates", [])
            if not lts:
                return {"success": False, "error": "launch template not found"}
            lt = lts[0]

            ver_kwargs: Dict = {"LaunchTemplateId": lt["LaunchTemplateId"]}
            ver_kwargs["Versions"] = versions if versions else ["$Default", "$Latest"]
            ver_resp = self.ec2.describe_launch_template_versions(**ver_kwargs)

            parsed_versions = []
            for v in ver_resp.get("LaunchTemplateVersions", []):
                data = v.get("LaunchTemplateData", {})
                parsed_versions.append({
                    "version_number": v["VersionNumber"],
                    "version_description": v.get("VersionDescription"),
                    "is_default_version": v.get("DefaultVersion", False),
                    "instance_type": data.get("InstanceType"),
                    "image_id": data.get("ImageId"),
                    "key_name": data.get("KeyName"),
                    "security_group_ids": data.get("SecurityGroupIds", []),
                    "user_data_present": bool(data.get("UserData")),
                })

            return {
                "success": True,
                "launch_template_id": lt["LaunchTemplateId"],
                "launch_template_name": lt["LaunchTemplateName"],
                "default_version_number": lt["DefaultVersionNumber"],
                "latest_version_number": lt["LatestVersionNumber"],
                "versions": parsed_versions,
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}

    def create_launch_template_version(
        self,
        launch_template_id: Optional[str] = None,
        launch_template_name: Optional[str] = None,
        source_version: str = "$Default",
        new_instance_type: Optional[str] = None,
        description: Optional[str] = None,
        set_as_default: bool = False,
    ) -> Dict:
        """
        Create a new launch template version, optionally overriding the instance type.
        The new version inherits all settings from source_version except the overrides.
        """
        try:
            if not launch_template_id and not launch_template_name:
                return {"success": False, "error": "provide launch_template_id or launch_template_name"}

            if not launch_template_id:
                resp = self.ec2.describe_launch_templates(LaunchTemplateNames=[launch_template_name])
                lts = resp.get("LaunchTemplates", [])
                if not lts:
                    return {"success": False, "error": f"launch template '{launch_template_name}' not found"}
                launch_template_id = lts[0]["LaunchTemplateId"]

            # Read source version data
            ver_resp = self.ec2.describe_launch_template_versions(
                LaunchTemplateId=launch_template_id,
                Versions=[source_version],
            )
            source_versions = ver_resp.get("LaunchTemplateVersions", [])
            if not source_versions:
                return {"success": False, "error": f"source version '{source_version}' not found"}

            source_data = dict(source_versions[0]["LaunchTemplateData"])

            overrides: Dict = {}
            if new_instance_type:
                source_data["InstanceType"] = new_instance_type
                overrides["instance_type"] = new_instance_type

            create_kwargs: Dict = {
                "LaunchTemplateId": launch_template_id,
                "SourceVersion": source_version,
                "LaunchTemplateData": source_data,
            }
            if description:
                create_kwargs["VersionDescription"] = description

            resp = self.ec2.create_launch_template_version(**create_kwargs)
            new_ver = resp["LaunchTemplateVersion"]
            new_version_number = int(new_ver["VersionNumber"])

            if set_as_default:
                self.ec2.modify_launch_template(
                    LaunchTemplateId=launch_template_id,
                    DefaultVersion=str(new_version_number),
                )

            return {
                "success": True,
                "launch_template_id": launch_template_id,
                "new_version_number": new_version_number,
                "source_version": source_version,
                "overrides_applied": overrides,
                "set_as_default": set_as_default,
                "description": description,
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}

    def set_launch_template_default_version(
        self,
        version: str,
        launch_template_id: Optional[str] = None,
        launch_template_name: Optional[str] = None,
    ) -> Dict:
        """Set the default version of a launch template."""
        try:
            if not launch_template_id and not launch_template_name:
                return {"success": False, "error": "provide launch_template_id or launch_template_name"}

            if not launch_template_id:
                resp = self.ec2.describe_launch_templates(LaunchTemplateNames=[launch_template_name])
                lts = resp.get("LaunchTemplates", [])
                if not lts:
                    return {"success": False, "error": f"launch template '{launch_template_name}' not found"}
                launch_template_id = lts[0]["LaunchTemplateId"]

            self.ec2.modify_launch_template(
                LaunchTemplateId=launch_template_id,
                DefaultVersion=str(version),
            )
            return {
                "success": True,
                "launch_template_id": launch_template_id,
                "new_default_version": version,
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}

    # ─────────────────────────── Auto Scaling Groups ──────────────────────────

    def list_asgs(self, asg_names: Optional[List[str]] = None) -> Dict:
        """List Auto Scaling Groups with key configuration details."""
        try:
            kwargs: Dict = {}
            if asg_names:
                kwargs["AutoScalingGroupNames"] = asg_names

            paginator = self.autoscaling.get_paginator("describe_auto_scaling_groups")
            groups = []
            for page in paginator.paginate(**kwargs):
                for asg in page.get("AutoScalingGroups", []):
                    lt_ref = asg.get("LaunchTemplate") or {}
                    groups.append({
                        "asg_name": asg["AutoScalingGroupName"],
                        "min_size": asg["MinSize"],
                        "max_size": asg["MaxSize"],
                        "desired_capacity": asg["DesiredCapacity"],
                        "instance_count": len(asg.get("Instances", [])),
                        "launch_template": {
                            "id": lt_ref.get("LaunchTemplateId"),
                            "name": lt_ref.get("LaunchTemplateName"),
                            "version": lt_ref.get("Version"),
                        } if lt_ref else None,
                        "uses_mixed_instances_policy": bool(asg.get("MixedInstancesPolicy")),
                        "availability_zones": asg.get("AvailabilityZones", []),
                        "status": asg.get("Status"),
                        "tags": {t["Key"]: t["Value"] for t in asg.get("Tags", [])},
                    })
            return {"success": True, "asgs": groups, "count": len(groups)}
        except ClientError as e:
            return {"success": False, "error": str(e)}

    def describe_asg(self, asg_name: str) -> Dict:
        """Get full details of a single Auto Scaling Group."""
        try:
            resp = self.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
            groups = resp.get("AutoScalingGroups", [])
            if not groups:
                return {"success": False, "error": f"ASG '{asg_name}' not found"}
            asg = groups[0]

            lt_ref = asg.get("LaunchTemplate") or {}
            instances = []
            for inst in asg.get("Instances", []):
                inst_lt = inst.get("LaunchTemplate") or {}
                instances.append({
                    "instance_id": inst["InstanceId"],
                    "instance_type": inst.get("InstanceType"),
                    "availability_zone": inst.get("AvailabilityZone"),
                    "lifecycle_state": inst.get("LifecycleState"),
                    "health_status": inst.get("HealthStatus"),
                    "launch_template_version": inst_lt.get("Version"),
                })

            return {
                "success": True,
                "asg_name": asg["AutoScalingGroupName"],
                "arn": asg.get("AutoScalingGroupARN"),
                "min_size": asg["MinSize"],
                "max_size": asg["MaxSize"],
                "desired_capacity": asg["DesiredCapacity"],
                "launch_template": {
                    "id": lt_ref.get("LaunchTemplateId"),
                    "name": lt_ref.get("LaunchTemplateName"),
                    "version": lt_ref.get("Version"),
                } if lt_ref else None,
                "mixed_instances_policy": asg.get("MixedInstancesPolicy"),
                "availability_zones": asg.get("AvailabilityZones", []),
                "load_balancer_names": asg.get("LoadBalancerNames", []),
                "target_group_arns": asg.get("TargetGroupARNs", []),
                "health_check_type": asg.get("HealthCheckType"),
                "instances": instances,
                "tags": {t["Key"]: t["Value"] for t in asg.get("Tags", [])},
                "created_time": (
                    asg["CreatedTime"].isoformat() if asg.get("CreatedTime") else None
                ),
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}

    def get_instance_asg(self, instance_id: str) -> Dict:
        """Find which ASG an instance belongs to (if any)."""
        try:
            resp = self.autoscaling.describe_auto_scaling_instances(InstanceIds=[instance_id])
            instances = resp.get("AutoScalingInstances", [])
            if not instances:
                return {
                    "success": True,
                    "instance_id": instance_id,
                    "in_asg": False,
                    "asg_name": None,
                }
            inst = instances[0]
            return {
                "success": True,
                "instance_id": instance_id,
                "in_asg": True,
                "asg_name": inst["AutoScalingGroupName"],
                "lifecycle_state": inst.get("LifecycleState"),
                "health_status": inst.get("HealthStatus"),
                "launch_template": inst.get("LaunchTemplate"),
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}

    def update_asg_launch_template(
        self,
        asg_name: str,
        launch_template_id: Optional[str] = None,
        launch_template_name: Optional[str] = None,
        version: str = "$Latest",
    ) -> Dict:
        """
        Update an ASG to use a specific launch template version.
        If no LT id/name is provided, keeps the current LT and only updates the version.
        """
        try:
            if not launch_template_id and not launch_template_name:
                asg_resp = self.autoscaling.describe_auto_scaling_groups(
                    AutoScalingGroupNames=[asg_name]
                )
                groups = asg_resp.get("AutoScalingGroups", [])
                if not groups:
                    return {"success": False, "error": f"ASG '{asg_name}' not found"}
                lt_ref = groups[0].get("LaunchTemplate") or {}
                if not lt_ref:
                    return {
                        "success": False,
                        "error": f"ASG '{asg_name}' does not use a launch template",
                    }
                launch_template_id = lt_ref.get("LaunchTemplateId")

            lt_spec: Dict = {"Version": version}
            if launch_template_id:
                lt_spec["LaunchTemplateId"] = launch_template_id
            else:
                lt_spec["LaunchTemplateName"] = launch_template_name

            self.autoscaling.update_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                LaunchTemplate=lt_spec,
            )
            return {
                "success": True,
                "asg_name": asg_name,
                "launch_template_id": launch_template_id,
                "launch_template_name": launch_template_name,
                "version_set": version,
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}

    def sync_asg_after_resize(
        self,
        instance_id: str,
        new_instance_type: str,
        set_lt_as_default: bool = True,
    ) -> Dict:
        """
        Propagate a vertical resize to the ASG's launch template so future scale-out
        events use the new instance type.

        Steps:
          1. Find the ASG this instance belongs to.
          2. Read the ASG's current launch template reference.
          3. Create a new LT version with new_instance_type (based on $Default).
          4. Optionally set the new version as LT default.
          5. Update the ASG to use $Latest.
        """
        try:
            asg_info = self.get_instance_asg(instance_id)
            if not asg_info.get("success"):
                return asg_info
            if not asg_info.get("in_asg"):
                return {
                    "success": True,
                    "synced": False,
                    "reason": "instance not in any ASG — no launch template update needed",
                    "instance_id": instance_id,
                }

            asg_name = asg_info["asg_name"]

            asg_resp = self.autoscaling.describe_auto_scaling_groups(
                AutoScalingGroupNames=[asg_name]
            )
            groups = asg_resp.get("AutoScalingGroups", [])
            if not groups:
                return {"success": False, "error": f"ASG '{asg_name}' not found"}

            lt_ref = groups[0].get("LaunchTemplate") or {}
            if not lt_ref:
                return {
                    "success": True,
                    "synced": False,
                    "reason": "ASG uses a launch configuration, not a launch template — update manually",
                    "asg_name": asg_name,
                }

            lt_id = lt_ref["LaunchTemplateId"]

            description = (
                f"Vertical scaling sync: instance_type={new_instance_type} "
                f"(source instance {instance_id})"
            )
            create_result = self.create_launch_template_version(
                launch_template_id=lt_id,
                source_version="$Default",
                new_instance_type=new_instance_type,
                description=description,
                set_as_default=set_lt_as_default,
            )
            if not create_result.get("success"):
                return create_result

            update_result = self.update_asg_launch_template(
                asg_name=asg_name,
                launch_template_id=lt_id,
                version="$Latest",
            )
            if not update_result.get("success"):
                return update_result

            return {
                "success": True,
                "synced": True,
                "instance_id": instance_id,
                "asg_name": asg_name,
                "launch_template_id": lt_id,
                "new_launch_template_version": create_result["new_version_number"],
                "new_instance_type": new_instance_type,
                "lt_default_updated": set_lt_as_default,
                "asg_now_uses_version": "$Latest",
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}

    # ─────────────────────────── Scaling Policies ─────────────────────────────

    def put_scaling_policy(
        self,
        asg_name: str,
        policy_name: str,
        policy_type: str = "TargetTrackingScaling",
        # Target tracking params
        target_value: Optional[float] = None,
        predefined_metric_type: Optional[str] = None,
        disable_scale_in: bool = False,
        # Simple / Step params
        adjustment_type: Optional[str] = None,
        scaling_adjustment: Optional[int] = None,
        cooldown: Optional[int] = None,
        step_adjustments: Optional[List[Dict]] = None,
        estimated_instance_warmup: Optional[int] = None,
        metric_aggregation_type: str = "Average",
        # Optional: attach a pre-existing CloudWatch alarm to this policy
        alarm_name: Optional[str] = None,
    ) -> Dict:
        """
        Create or update a scaling policy on an ASG.

        policy_type options:
          - TargetTrackingScaling: requires target_value; optionally predefined_metric_type
              predefined_metric_type values: ASGAverageCPUUtilization (default),
              ASGAverageNetworkIn, ASGAverageNetworkOut, ALBRequestCountPerTarget
          - SimpleScaling: requires adjustment_type + scaling_adjustment; optional cooldown
          - StepScaling: requires adjustment_type + step_adjustments list

        adjustment_type values: ChangeInCapacity | ExactCapacity | PercentChangeInCapacity
        step_adjustments format: [{"MetricIntervalLowerBound": 0, "MetricIntervalUpperBound": 10,
                                    "ScalingAdjustment": 1}, ...]

        alarm_name: optional name of a pre-existing CloudWatch alarm to attach to this policy.
          When provided, the policy ARN is added to the alarm's AlarmActions.
          Omitting it is valid — the policy is created without any alarm attachment.
        """
        try:
            kwargs: Dict = {
                "AutoScalingGroupName": asg_name,
                "PolicyName": policy_name,
                "PolicyType": policy_type,
            }

            if policy_type == "TargetTrackingScaling":
                if target_value is None:
                    return {
                        "success": False,
                        "error": "target_value is required for TargetTrackingScaling",
                    }
                kwargs["TargetTrackingConfiguration"] = {
                    "PredefinedMetricSpecification": {
                        "PredefinedMetricType": predefined_metric_type or "ASGAverageCPUUtilization",
                    },
                    "TargetValue": float(target_value),
                    "DisableScaleIn": disable_scale_in,
                }

            elif policy_type == "SimpleScaling":
                if adjustment_type is None or scaling_adjustment is None:
                    return {
                        "success": False,
                        "error": "adjustment_type and scaling_adjustment are required for SimpleScaling",
                    }
                kwargs["AdjustmentType"] = adjustment_type
                kwargs["ScalingAdjustment"] = int(scaling_adjustment)
                if cooldown is not None:
                    kwargs["Cooldown"] = int(cooldown)

            elif policy_type == "StepScaling":
                if adjustment_type is None or not step_adjustments:
                    return {
                        "success": False,
                        "error": "adjustment_type and step_adjustments are required for StepScaling",
                    }
                kwargs["AdjustmentType"] = adjustment_type
                kwargs["StepAdjustments"] = step_adjustments
                kwargs["MetricAggregationType"] = metric_aggregation_type
                if estimated_instance_warmup is not None:
                    kwargs["EstimatedInstanceWarmup"] = int(estimated_instance_warmup)

            else:
                return {"success": False, "error": f"unknown policy_type '{policy_type}'"}

            resp = self.autoscaling.put_scaling_policy(**kwargs)
            policy_arn = resp.get("PolicyARN")

            alarm_attach_result = None
            if alarm_name and policy_arn:
                alarm_attach_result = self._attach_alarm_to_policy(alarm_name, policy_arn)

            return {
                "success": True,
                "asg_name": asg_name,
                "policy_name": policy_name,
                "policy_type": policy_type,
                "policy_arn": policy_arn,
                "alarms": resp.get("Alarms", []),
                "alarm_attached": alarm_attach_result,
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}

    def _attach_alarm_to_policy(self, alarm_name: str, policy_arn: str) -> Dict:
        """Fetch an existing CloudWatch alarm and add policy_arn to its AlarmActions."""
        try:
            cw_resp = self.cloudwatch.describe_alarms(AlarmNames=[alarm_name])
            alarms = cw_resp.get("MetricAlarms", [])
            if not alarms:
                return {"success": False, "alarm_name": alarm_name, "error": "alarm not found"}

            alarm = alarms[0]
            existing_actions: List[str] = alarm.get("AlarmActions", [])
            if policy_arn in existing_actions:
                return {"success": True, "alarm_name": alarm_name, "already_attached": True}

            updated_actions = existing_actions + [policy_arn]

            put_kwargs: Dict = {
                "AlarmName": alarm["AlarmName"],
                "MetricName": alarm["MetricName"],
                "Namespace": alarm["Namespace"],
                "Dimensions": alarm.get("Dimensions", []),
                "Period": alarm["Period"],
                "EvaluationPeriods": alarm["EvaluationPeriods"],
                "Threshold": alarm["Threshold"],
                "ComparisonOperator": alarm["ComparisonOperator"],
                "AlarmActions": updated_actions,
            }
            if alarm.get("Statistic"):
                put_kwargs["Statistic"] = alarm["Statistic"]
            if alarm.get("ExtendedStatistic"):
                put_kwargs["ExtendedStatistic"] = alarm["ExtendedStatistic"]
            if alarm.get("AlarmDescription"):
                put_kwargs["AlarmDescription"] = alarm["AlarmDescription"]
            if alarm.get("OKActions"):
                put_kwargs["OKActions"] = alarm["OKActions"]
            if alarm.get("InsufficientDataActions"):
                put_kwargs["InsufficientDataActions"] = alarm["InsufficientDataActions"]
            if alarm.get("TreatMissingData"):
                put_kwargs["TreatMissingData"] = alarm["TreatMissingData"]
            if alarm.get("DatapointsToAlarm"):
                put_kwargs["DatapointsToAlarm"] = alarm["DatapointsToAlarm"]

            self.cloudwatch.put_metric_alarm(**put_kwargs)
            return {"success": True, "alarm_name": alarm_name, "attached": True}
        except ClientError as e:
            return {"success": False, "alarm_name": alarm_name, "error": str(e)}

    def describe_scaling_policies(self, asg_name: str) -> Dict:
        """List all scaling policies attached to an ASG, enriched with CloudWatch alarm details."""
        try:
            paginator = self.autoscaling.get_paginator("describe_policies")
            policies = []
            all_alarm_names: List[str] = []

            for page in paginator.paginate(AutoScalingGroupName=asg_name):
                for p in page.get("ScalingPolicies", []):
                    tt = p.get("TargetTrackingConfiguration") or {}
                    predefined = tt.get("PredefinedMetricSpecification") or {}
                    customized = tt.get("CustomizedMetricSpecification") or {}
                    alarm_names = [a.get("AlarmName") for a in p.get("Alarms", [])]
                    all_alarm_names.extend(alarm_names)
                    policies.append({
                        "policy_name": p["PolicyName"],
                        "policy_arn": p.get("PolicyARN"),
                        "policy_type": p.get("PolicyType"),
                        "adjustment_type": p.get("AdjustmentType"),
                        "scaling_adjustment": p.get("ScalingAdjustment"),
                        "cooldown": p.get("Cooldown"),
                        "metric_aggregation_type": p.get("MetricAggregationType"),
                        "estimated_instance_warmup": p.get("EstimatedInstanceWarmup"),
                        "target_tracking": {
                            "target_value": tt.get("TargetValue"),
                            "predefined_metric": predefined.get("PredefinedMetricType"),
                            "customized_metric": customized.get("MetricName"),
                            "disable_scale_in": tt.get("DisableScaleIn"),
                        } if tt else None,
                        "step_adjustments": p.get("StepAdjustments"),
                        "alarm_names": alarm_names,
                    })

            # Enrich with CloudWatch alarm details (threshold, operator, metric, state)
            alarm_details: Dict[str, Dict] = {}
            unique_alarms = list(set(all_alarm_names))
            for i in range(0, len(unique_alarms), 100):
                cw_resp = self.cloudwatch.describe_alarms(AlarmNames=unique_alarms[i:i + 100])
                for a in cw_resp.get("MetricAlarms", []):
                    alarm_details[a["AlarmName"]] = {
                        "threshold": a.get("Threshold"),
                        "comparison_operator": a.get("ComparisonOperator"),
                        "metric_name": a.get("MetricName"),
                        "namespace": a.get("Namespace"),
                        "period_seconds": a.get("Period"),
                        "evaluation_periods": a.get("EvaluationPeriods"),
                        "statistic": a.get("Statistic"),
                        "state": a.get("StateValue"),
                        "alarm_description": a.get("AlarmDescription"),
                    }

            for policy in policies:
                policy["alarms"] = [
                    {"alarm_name": name, **alarm_details.get(name, {})}
                    for name in policy.pop("alarm_names")
                ]

            return {
                "success": True,
                "asg_name": asg_name,
                "policies": policies,
                "count": len(policies),
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}

    def delete_scaling_policy(self, asg_name: str, policy_name: str) -> Dict:
        """Delete a scaling policy from an ASG."""
        try:
            self.autoscaling.delete_policy(
                AutoScalingGroupName=asg_name,
                PolicyName=policy_name,
            )
            return {
                "success": True,
                "asg_name": asg_name,
                "policy_name": policy_name,
                "deleted": True,
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}
