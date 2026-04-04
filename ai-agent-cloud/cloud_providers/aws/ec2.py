"""
AWS EC2 Manager
Handles all EC2 instance operations using boto3.
"""

import sys
import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from .mapping import (
    map_generic_to_instance_type,
    get_estimated_hourly_cost,
    get_instance_type_specs,
    find_cheapest_instance_type_for_requirements,
    get_next_smaller_instance_type,
    get_instance_catalog,
)


class EC2Manager:
    """Manages AWS EC2 instances with simplified interface."""
    
    def __init__(self, region: str = 'us-east-1'):
        """
        Initialize EC2 Manager.
        
        Args:
            region: AWS region to opera te in (default: us-east-1)
        """
        self.region = region
        self.ec2_client = boto3.client('ec2', region_name=region)
        self.ec2_resource = boto3.resource('ec2', region_name=region)
        self.ssm_client = boto3.client('ssm', region_name=region)
        self.compute_optimizer_client = boto3.client('compute-optimizer', region_name=region)
    
    def list_instances(self, tag_filter: Optional[Dict[str, str]] = None) -> List[Dict]:
        """
        List all EC2 instances, optionally filtered by tags.
        
        Args:
            tag_filter: Optional dictionary of tags to filter by (e.g., {'ManagedBy': 'AIAgent'})
        
        Returns:
            List of instance dictionaries with id, name, type, state, etc.
        
        Example:
            >>> manager = EC2Manager()
            >>> instances = manager.list_instances({'ManagedBy': 'AIAgent'})
        """
        try:
            filters = []
            
            # Add tag filters if provided
            if tag_filter:
                for key, value in tag_filter.items():
                    filters.append({'Name': f'tag:{key}', 'Values': [value]})
            
            response = self.ec2_client.describe_instances(Filters=filters)
            
            instances = []
            for reservation in response['Reservations']:
                for instance in reservation['Instances']:
                    # Extract name from tags
                    name = 'N/A'
                    tags = instance.get('Tags', [])
                    for tag in tags:
                        if tag['Key'] == 'Name':
                            name = tag['Value']
                            break
                    
                    instances.append({
                        'id': instance['InstanceId'],
                        'name': name,
                        'type': instance['InstanceType'],
                        'state': instance['State']['Name'],
                        'launch_time': instance.get('LaunchTime', 'N/A'),
                        'public_ip': instance.get('PublicIpAddress', 'N/A'),
                        'private_ip': instance.get('PrivateIpAddress', 'N/A'),
                        'vpc_id': instance.get('VpcId', 'N/A'),
                        'subnet_id': instance.get('SubnetId', 'N/A'),
                        'tags': tags  # Include tags for reference
                    })
            
            return instances
        
        except ClientError as e:
            print(f"Error listing instances: {e}")
            return []
    
    def create_instance(
        self,
        name: str,
        cpu: int = 2,
        ram: int = 4,
        image_id: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        instance_type: Optional[str] = None,
    ) -> Dict:
        """
        Create a new EC2 instance.
        
        Args:
            name: Name for the instance (will be added as 'Name' tag)
            cpu: Number of virtual CPU cores (default: 2)
            ram: RAM in gigabytes (default: 4)
            image_id: AMI ID to use. If None, uses latest Amazon Linux 2023
        
        Returns:
            Dictionary with instance details (id, name, type, state)
        
        Raises:
            ClientError: If instance creation fails
        
        Example:
            >>> manager = EC2Manager()
            >>> instance = manager.create_instance('web-server-1', cpu=2, ram=4)
        """
        try:
            # Map generic specs to instance type unless explicitly provided.
            resolved_instance_type = instance_type or map_generic_to_instance_type(cpu, ram)
            
            # Get latest Amazon Linux 2023 AMI if not specified
            if not image_id:
                image_id = self._get_latest_amazon_linux_ami()
            
            # Create instance
            user_tags = tags or {}
            merged_tags = {
                'Name': name,
                'ManagedBy': 'AIAgent',
                'CreatedBy': 'MCP-AWS-Server',
            }
            merged_tags.update(user_tags)

            instances = self.ec2_resource.create_instances(
                ImageId=image_id,
                InstanceType=resolved_instance_type,
                MinCount=1,
                MaxCount=1,
                TagSpecifications=[
                    {
                        'ResourceType': 'instance',
                        'Tags': [{'Key': key, 'Value': str(value)} for key, value in merged_tags.items()]
                    }
                ]
            )
            
            instance = instances[0]
            
            print(f"[SUCCESS] Created instance {instance.id} with type {resolved_instance_type}", file=sys.stderr)
            
            return {
                'id': instance.id,
                'name': name,
                'type': resolved_instance_type,
                'state': 'pending',
                'cpu': cpu,
                'ram': ram,
                'estimated_hourly_cost': get_estimated_hourly_cost(resolved_instance_type),
            }
        
        except ClientError as e:
            error_msg = f"Failed to create instance: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def delete_instance(self, instance_id: str) -> Dict:
        """
        Terminate (delete) an EC2 instance.
        
        Args:
            instance_id: EC2 instance ID (e.g., 'i-1234567890abcdef0')
        
        Returns:
            Dictionary with termination status
        
        Example:
            >>> manager = EC2Manager()
            >>> result = manager.delete_instance('i-1234567890abcdef0')
        """
        try:
            response = self.ec2_client.terminate_instances(InstanceIds=[instance_id])
            
            current_state = response['TerminatingInstances'][0]['CurrentState']['Name']
            
            print(f"[SUCCESS] Instance {instance_id} is now {current_state}", file=sys.stderr)
            
            return {
                'id': instance_id,
                'state': current_state,
                'message': f'Instance {instance_id} termination initiated'
            }
        
        except ClientError as e:
            error_msg = f"Failed to delete instance: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def start_instance(self, instance_id: str) -> Dict:
        """
        Start a stopped EC2 instance.

        Args:
            instance_id: EC2 instance ID

        Returns:
            Dictionary with state transition details
        """
        try:
            response = self.ec2_client.start_instances(InstanceIds=[instance_id])
            transition = response['StartingInstances'][0]

            return {
                'id': instance_id,
                'previous_state': transition['PreviousState']['Name'],
                'current_state': transition['CurrentState']['Name'],
                'message': f'Instance {instance_id} start initiated'
            }

        except ClientError as e:
            error_msg = f"Failed to start instance: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def stop_instance(self, instance_id: str, force: bool = False) -> Dict:
        """
        Stop a running EC2 instance.

        Args:
            instance_id: EC2 instance ID
            force: Force stop if graceful shutdown fails

        Returns:
            Dictionary with state transition details
        """
        try:
            response = self.ec2_client.stop_instances(
                InstanceIds=[instance_id],
                Force=force,
            )
            transition = response['StoppingInstances'][0]

            return {
                'id': instance_id,
                'previous_state': transition['PreviousState']['Name'],
                'current_state': transition['CurrentState']['Name'],
                'force': force,
                'message': f'Instance {instance_id} stop initiated'
            }

        except ClientError as e:
            error_msg = f"Failed to stop instance: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def reboot_instance(self, instance_id: str) -> Dict:
        """
        Reboot a running EC2 instance.

        Args:
            instance_id: EC2 instance ID

        Returns:
            Dictionary with reboot request status
        """
        try:
            self.ec2_client.reboot_instances(InstanceIds=[instance_id])
            return {
                'id': instance_id,
                'message': f'Instance {instance_id} reboot initiated'
            }

        except ClientError as e:
            error_msg = f"Failed to reboot instance: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def get_instance_ssm_status(self, instance_id: str) -> Dict:
        """
        Get AWS Systems Manager (SSM) managed-instance status for an EC2 instance.

        Args:
            instance_id: EC2 instance ID

        Returns:
            Dictionary with SSM online/offline state and metadata
        """
        try:
            response = self.ssm_client.describe_instance_information(
                Filters=[
                    {
                        'Key': 'InstanceIds',
                        'Values': [instance_id],
                    }
                ],
                MaxResults=10,
            )

            info_list = response.get('InstanceInformationList', [])
            if not info_list:
                return {
                    'instance_id': instance_id,
                    'managed_by_ssm': False,
                    'ping_status': 'NotManaged',
                    'message': 'Instance not found in SSM managed instances. Check IAM role and SSM agent registration.'
                }

            info = info_list[0]
            return {
                'instance_id': instance_id,
                'managed_by_ssm': True,
                'ping_status': info.get('PingStatus', 'Unknown'),
                'platform_name': info.get('PlatformName', 'Unknown'),
                'platform_version': info.get('PlatformVersion', 'Unknown'),
                'agent_version': info.get('AgentVersion', 'Unknown'),
                'is_latest_version': info.get('IsLatestVersion', False),
                'last_ping_date_time': str(info.get('LastPingDateTime', 'Unknown')),
                'resource_type': info.get('ResourceType', 'Unknown')
            }

        except ClientError as e:
            error_msg = f"Failed to get SSM status: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def get_instance_status(self, instance_id: str) -> Dict:
        """
        Get detailed status of an EC2 instance.
        
        Args:
            instance_id: EC2 instance ID
        
        Returns:
            Dictionary with instance status details
        """
        try:
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            
            instance = response['Reservations'][0]['Instances'][0]
            
            # Extract name from tags
            name = 'N/A'
            for tag in instance.get('Tags', []):
                if tag['Key'] == 'Name':
                    name = tag['Value']
                    break
            
            return {
                'id': instance['InstanceId'],
                'name': name,
                'type': instance['InstanceType'],
                'state': instance['State']['Name'],
                'public_ip': instance.get('PublicIpAddress', 'N/A'),
                'private_ip': instance.get('PrivateIpAddress', 'N/A'),
                'launch_time': str(instance.get('LaunchTime', 'N/A'))
            }
        
        except ClientError as e:
            error_msg = f"Failed to get instance status: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def get_instance_type_compatibility(self, source_instance_type: str, target_instance_type: str) -> Dict:
        """
        Return compatibility checks for moving from one instance type to another.
        """
        try:
            source_desc = self.ec2_client.describe_instance_types(InstanceTypes=[source_instance_type])
            target_desc = self.ec2_client.describe_instance_types(InstanceTypes=[target_instance_type])

            source_info = source_desc['InstanceTypes'][0]
            target_info = target_desc['InstanceTypes'][0]

            source_arch = sorted(source_info.get('ProcessorInfo', {}).get('SupportedArchitectures', []))
            target_arch = sorted(target_info.get('ProcessorInfo', {}).get('SupportedArchitectures', []))
            common_arch = sorted(set(source_arch).intersection(set(target_arch)))

            return {
                'source_instance_type': source_instance_type,
                'target_instance_type': target_instance_type,
                'source_supported_architectures': source_arch,
                'target_supported_architectures': target_arch,
                'common_architectures': common_arch,
                'compatible': len(common_arch) > 0,
            }
        except ClientError as e:
            error_msg = f"Failed to evaluate instance type compatibility: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def create_instance_backup_ami(
        self,
        instance_id: str,
        name_prefix: str = "ai-agent-resize-backup",
        no_reboot: bool = True,
    ) -> Dict:
        """
        Create an AMI backup for an instance before risky changes like resizing.
        """
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        image_name = f"{name_prefix}-{instance_id}-{timestamp}"

        try:
            response = self.ec2_client.create_image(
                InstanceId=instance_id,
                Name=image_name,
                Description=f"Automated backup before resize for {instance_id}",
                NoReboot=no_reboot,
                TagSpecifications=[
                    {
                        'ResourceType': 'image',
                        'Tags': [
                            {'Key': 'ManagedBy', 'Value': 'AIAgent'},
                            {'Key': 'BackupType', 'Value': 'PreResize'},
                            {'Key': 'SourceInstanceId', 'Value': instance_id},
                            {'Key': 'CreatedAt', 'Value': datetime.now(timezone.utc).isoformat()},
                        ],
                    }
                ],
            )

            return {
                'instance_id': instance_id,
                'image_id': response.get('ImageId'),
                'image_name': image_name,
                'no_reboot': no_reboot,
                'status': 'pending',
                'message': 'AMI backup creation initiated',
            }
        except ClientError as e:
            error_msg = f"Failed to create backup AMI: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def wait_for_instance_state(self, instance_id: str, desired_state: str, timeout_seconds: int = 600):
        """Block until instance reaches desired state or timeout via boto waiter."""
        waiter_map = {
            'stopped': 'instance_stopped',
            'running': 'instance_running',
        }
        waiter_name = waiter_map.get(desired_state)
        if not waiter_name:
            raise Exception(f"Unsupported desired state wait: {desired_state}")

        try:
            waiter = self.ec2_client.get_waiter(waiter_name)
            delay = 15
            max_attempts = max(1, timeout_seconds // delay)
            waiter.wait(
                InstanceIds=[instance_id],
                WaiterConfig={'Delay': delay, 'MaxAttempts': max_attempts},
            )
        except ClientError as e:
            error_msg = f"Failed while waiting for instance state '{desired_state}': {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def get_cheapest_compatible_instance_type(
        self,
        instance_id: str,
        min_cpu: Optional[int] = None,
        min_ram_gb: Optional[int] = None,
        allowed_families: Optional[List[str]] = None,
        prefer_downsize_when_idle: bool = False,
    ) -> Dict:
        """
        Attribute-based selection: pick the cheapest available instance type that meets requirements.
        """
        current = self.get_instance_status(instance_id)
        current_type = current.get('type')

        current_specs = get_instance_type_specs(current_type) or {'vcpu': 2, 'ram_gb': 4}
        required_cpu = min_cpu if min_cpu is not None else int(current_specs['vcpu'])
        required_ram = min_ram_gb if min_ram_gb is not None else int(current_specs['ram_gb'])

        families_tuple: Optional[Tuple[str, ...]] = None
        if allowed_families:
            cleaned = [family.strip() for family in allowed_families if str(family).strip()]
            if cleaned:
                families_tuple = tuple(cleaned)

        # Downsize mode: if caller indicates the instance is idle and no explicit
        # capacity requirements were provided, choose the cheapest compatible downsize target.
        if prefer_downsize_when_idle and min_cpu is None and min_ram_gb is None:
            current_vcpu = float(current_specs['vcpu'])
            current_ram = float(current_specs['ram_gb'])
            current_hourly = get_estimated_hourly_cost(current_type)

            # Safety floor to avoid extreme downsize without explicit user constraints.
            floor_vcpu = 1.0
            floor_ram = 1.0

            candidates = []
            for item in get_instance_catalog():
                candidate_type = str(item['instance_type'])
                candidate_vcpu = float(item['vcpu'])
                candidate_ram = float(item['ram_gb'])
                candidate_hourly = float(item['hourly_cost'])

                if candidate_type == current_type:
                    continue
                if candidate_vcpu > current_vcpu or candidate_ram > current_ram:
                    continue
                if candidate_vcpu < floor_vcpu or candidate_ram < floor_ram:
                    continue
                if candidate_hourly >= current_hourly:
                    continue

                candidate_family = candidate_type.split('.')[0]
                if families_tuple and candidate_family not in families_tuple:
                    continue

                compat = self.get_instance_type_compatibility(current_type, candidate_type)
                if not compat.get('compatible'):
                    continue

                candidates.append((candidate_hourly, -candidate_ram, -candidate_vcpu, candidate_type, compat))

            # Fallback: if no cross-family option is suitable, try one-step smaller in same family.
            if not candidates:
                next_smaller = get_next_smaller_instance_type(current_type, allowed_families=families_tuple)
                if next_smaller:
                    next_compat = self.get_instance_type_compatibility(current_type, next_smaller)
                    if next_compat.get('compatible'):
                        target_hourly = get_estimated_hourly_cost(next_smaller)
                        return {
                            'instance_id': instance_id,
                            'current_instance_type': current_type,
                            'recommended_instance_type': next_smaller,
                            'required_cpu': int(current_specs['vcpu']),
                            'required_ram_gb': int(current_specs['ram_gb']),
                            'selection_strategy': 'idle_step_down_same_family_fallback',
                            'compatibility': next_compat,
                            'estimated_hourly_cost_current': current_hourly,
                            'estimated_hourly_cost_recommended': target_hourly,
                            'estimated_hourly_savings': max(0.0, current_hourly - target_hourly),
                            'estimated_monthly_savings': max(0.0, (current_hourly - target_hourly) * 24 * 30),
                        }
            else:
                candidates.sort(key=lambda item: (item[0], item[1], item[2]))
                best_hourly, _, _, best_type, best_compat = candidates[0]
                return {
                    'instance_id': instance_id,
                    'current_instance_type': current_type,
                    'recommended_instance_type': best_type,
                    'required_cpu': int(current_specs['vcpu']),
                    'required_ram_gb': int(current_specs['ram_gb']),
                    'selection_strategy': 'idle_smart_cheapest_compatible',
                    'compatibility': best_compat,
                    'estimated_hourly_cost_current': current_hourly,
                    'estimated_hourly_cost_recommended': best_hourly,
                    'estimated_hourly_savings': max(0.0, current_hourly - best_hourly),
                    'estimated_monthly_savings': max(0.0, (current_hourly - best_hourly) * 24 * 30),
                }

        cheapest = find_cheapest_instance_type_for_requirements(
            cpu=required_cpu,
            ram=required_ram,
            allowed_families=families_tuple,
        )

        compatibility = self.get_instance_type_compatibility(current_type, cheapest)
        current_hourly = get_estimated_hourly_cost(current_type)
        target_hourly = get_estimated_hourly_cost(cheapest)

        return {
            'instance_id': instance_id,
            'current_instance_type': current_type,
            'recommended_instance_type': cheapest,
            'required_cpu': required_cpu,
            'required_ram_gb': required_ram,
            'selection_strategy': 'min_capacity_cheapest_match',
            'compatibility': compatibility,
            'estimated_hourly_cost_current': current_hourly,
            'estimated_hourly_cost_recommended': target_hourly,
            'estimated_hourly_savings': max(0.0, current_hourly - target_hourly),
            'estimated_monthly_savings': max(0.0, (current_hourly - target_hourly) * 24 * 30),
        }

    def resize_instance_type(
        self,
        instance_id: str,
        target_instance_type: str,
        create_backup: bool = True,
        backup_name_prefix: str = 'ai-agent-resize-backup',
        wait_for_stop: bool = True,
        wait_for_start: bool = True,
        stop_timeout_seconds: int = 600,
        start_timeout_seconds: int = 600,
        no_reboot_backup: bool = True,
    ) -> Dict:
        """
        Safely resize an EC2 instance type with optional AMI backup and compatibility checks.
        """
        current_status = self.get_instance_status(instance_id)
        source_type = current_status.get('type')
        source_state = current_status.get('state')

        if source_type == target_instance_type:
            return {
                'instance_id': instance_id,
                'message': 'Instance already uses target instance type',
                'current_instance_type': source_type,
                'target_instance_type': target_instance_type,
                'changed': False,
            }

        compatibility = self.get_instance_type_compatibility(source_type, target_instance_type)
        if not compatibility.get('compatible'):
            raise Exception(
                f"Resize blocked: {source_type} -> {target_instance_type} has no common CPU architecture support"
            )

        backup_result = None
        if create_backup:
            backup_result = self.create_instance_backup_ami(
                instance_id=instance_id,
                name_prefix=backup_name_prefix,
                no_reboot=no_reboot_backup,
            )

        try:
            if source_state != 'stopped':
                self.stop_instance(instance_id)
                if wait_for_stop:
                    self.wait_for_instance_state(instance_id, 'stopped', timeout_seconds=stop_timeout_seconds)

            self.ec2_client.modify_instance_attribute(
                InstanceId=instance_id,
                InstanceType={'Value': target_instance_type},
            )

            self.start_instance(instance_id)
            if wait_for_start:
                self.wait_for_instance_state(instance_id, 'running', timeout_seconds=start_timeout_seconds)

            refreshed = self.get_instance_status(instance_id)
            source_hourly = get_estimated_hourly_cost(source_type)
            target_hourly = get_estimated_hourly_cost(target_instance_type)

            return {
                'instance_id': instance_id,
                'previous_instance_type': source_type,
                'target_instance_type': target_instance_type,
                'current_state': refreshed.get('state'),
                'backup': backup_result,
                'compatibility': compatibility,
                'estimated_hourly_cost_before': source_hourly,
                'estimated_hourly_cost_after': target_hourly,
                'estimated_hourly_savings': max(0.0, source_hourly - target_hourly),
                'estimated_monthly_savings': max(0.0, (source_hourly - target_hourly) * 24 * 30),
                'changed': True,
                'message': f"Resized {instance_id} from {source_type} to {target_instance_type}",
            }
        except ClientError as e:
            error_msg = f"Failed to resize instance type: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)

    def get_compute_optimizer_recommendations(self, instance_arns: Optional[List[str]] = None) -> Dict:
        """
        Fetch EC2 rightsizing recommendations from AWS Compute Optimizer.
        """
        try:
            kwargs = {}
            if instance_arns:
                kwargs['instanceArns'] = instance_arns

            response = self.compute_optimizer_client.get_ec2_instance_recommendations(**kwargs)
            recommendations = []
            for rec in response.get('instanceRecommendations', []):
                recommendation_options = []
                for option in rec.get('recommendationOptions', []):
                    recommendation_options.append(
                        {
                            'instance_type': option.get('instanceType'),
                            'performance_risk': option.get('performanceRisk'),
                            'projected_utilization_metrics': option.get('projectedUtilizationMetrics', []),
                            'savings_opportunity': option.get('savingsOpportunity', {}),
                            'savings_opportunity_after_discounts': option.get('savingsOpportunityAfterDiscounts', {}),
                        }
                    )

                recommendations.append(
                    {
                        'instance_arn': rec.get('instanceArn'),
                        'instance_name': rec.get('instanceName'),
                        'current_instance_type': rec.get('currentInstanceType'),
                        'finding': rec.get('finding'),
                        'finding_reason_codes': rec.get('findingReasonCodes', []),
                        'recommendation_options': recommendation_options,
                    }
                )

            return {
                'count': len(recommendations),
                'recommendations': recommendations,
                'next_token': response.get('nextToken'),
            }
        except ClientError as e:
            error_msg = f"Failed to get Compute Optimizer recommendations: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def _get_latest_amazon_linux_ami(self) -> str:
        """
        Get the latest Amazon Linux 2023 AMI ID for the current region.
        
        Returns:
            AMI ID string
        """
        try:
            response = self.ec2_client.describe_images(
                Owners=['amazon'],
                Filters=[
                    {'Name': 'name', 'Values': ['al2023-ami-2023.*-x86_64']},
                    {'Name': 'state', 'Values': ['available']},
                ],
                MaxResults=1
            )
            
            if response['Images']:
                return response['Images'][0]['ImageId']
            else:
                # Fallback to a known stable AMI (us-east-1)
                print("Warning: Could not find latest AMI, using fallback")
                return 'ami-0230bd60aa48260c6'  # Amazon Linux 2023 in us-east-1
        
        except ClientError as e:
            print(f"Warning: Error finding AMI, using fallback: {e}")
            return 'ami-0230bd60aa48260c6'
