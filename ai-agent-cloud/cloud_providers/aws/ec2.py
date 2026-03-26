"""
AWS EC2 Manager
Handles all EC2 instance operations using boto3.
"""

import sys
import boto3
from botocore.exceptions import ClientError
from typing import List, Dict, Optional
from .mapping import map_generic_to_instance_type


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
    
    def create_instance(self, name: str, cpu: int = 2, ram: int = 4, 
                       image_id: Optional[str] = None) -> Dict:
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
            # Map generic specs to instance type
            instance_type = map_generic_to_instance_type(cpu, ram)
            
            # Get latest Amazon Linux 2023 AMI if not specified
            if not image_id:
                image_id = self._get_latest_amazon_linux_ami()
            
            # Create instance
            instances = self.ec2_resource.create_instances(
                ImageId=image_id,
                InstanceType=instance_type,
                MinCount=1,
                MaxCount=1,
                TagSpecifications=[
                    {
                        'ResourceType': 'instance',
                        'Tags': [
                            {'Key': 'Name', 'Value': name},
                            {'Key': 'ManagedBy', 'Value': 'AIAgent'},
                            {'Key': 'CreatedBy', 'Value': 'MCP-AWS-Server'},
                        ]
                    }
                ]
            )
            
            instance = instances[0]
            
            print(f"[SUCCESS] Created instance {instance.id} with type {instance_type}", file=sys.stderr)
            
            return {
                'id': instance.id,
                'name': name,
                'type': instance_type,
                'state': 'pending',
                'cpu': cpu,
                'ram': ram
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
