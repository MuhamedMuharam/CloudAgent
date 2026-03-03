"""
AWS Security Group Manager
Handles security groups and their rules.
"""

import boto3
import sys
from botocore.exceptions import ClientError
from typing import List, Dict, Optional


class SecurityGroupManager:
    """Manages AWS Security Groups."""
    
    def __init__(self, region: str = 'us-east-1'):
        """
        Initialize Security Group Manager.
        
        Args:
            region: AWS region (default: us-east-1)
        """
        self.region = region
        self.ec2_client = boto3.client('ec2', region_name=region)
        self.ec2_resource = boto3.resource('ec2', region_name=region)
    
    def create_security_group(self, vpc_id: str, name: str, description: str, 
                             rules: Optional[List[Dict]] = None,
                             tags: Optional[Dict[str, str]] = None) -> Dict:
        """
        Create a security group with inbound/outbound rules.
        
        Args:
            vpc_id: VPC ID to create security group in
            name: Name for the security group
            description: Description of the security group
            rules: List of rule dicts:
                   {
                       'type': 'ingress' or 'egress',
                       'protocol': 'tcp', 'udp', 'icmp', or '-1' (all),
                       'from_port': int,
                       'to_port': int,
                       'cidr': 'x.x.x.x/x' or None,
                       'source_security_group_id': 'sg-xxx' or None
                   }
            tags: Additional tags
        
        Returns:
            Dictionary with security group details
        """
        try:
            # Create security group
            response = self.ec2_client.create_security_group(
                GroupName=name,
                Description=description,
                VpcId=vpc_id
            )
            
            security_group_id = response['GroupId']
            
            # Add tags
            sg_tags = [
                {'Key': 'Name', 'Value': name},
                {'Key': 'ManagedBy', 'Value': 'AIAgent'}
            ]
            
            if tags:
                for key, value in tags.items():
                    sg_tags.append({'Key': key, 'Value': value})
            
            self.ec2_client.create_tags(
                Resources=[security_group_id],
                Tags=sg_tags
            )
            
            # Add rules if specified
            added_rules = {'ingress': [], 'egress': []}
            if rules:
                for rule in rules:
                    rule_type = rule.get('type', 'ingress')
                    protocol = rule.get('protocol', 'tcp')
                    from_port = rule.get('from_port', rule.get('port', 80))
                    to_port = rule.get('to_port', rule.get('port', 80))
                    
                    # Build IP permissions
                    ip_permission = {
                        'IpProtocol': protocol,
                    }
                    
                    # Add ports for TCP/UDP
                    if protocol in ['tcp', 'udp']:
                        ip_permission['FromPort'] = from_port
                        ip_permission['ToPort'] = to_port
                    
                    # Add source (CIDR or security group)
                    if 'cidr' in rule and rule['cidr']:
                        ip_permission['IpRanges'] = [{'CidrIp': rule['cidr']}]
                    elif 'source_security_group_id' in rule and rule['source_security_group_id']:
                        ip_permission['UserIdGroupPairs'] = [{
                            'GroupId': rule['source_security_group_id']
                        }]
                    else:
                        # Default to 0.0.0.0/0 if no source specified
                        ip_permission['IpRanges'] = [{'CidrIp': '0.0.0.0/0'}]
                    
                    # Add rule
                    if rule_type == 'ingress':
                        self.ec2_client.authorize_security_group_ingress(
                            GroupId=security_group_id,
                            IpPermissions=[ip_permission]
                        )
                        added_rules['ingress'].append(rule)
                    else:  # egress
                        # Remove default egress rule first (allow all outbound)
                        if len(added_rules['egress']) == 0:
                            try:
                                self.ec2_client.revoke_security_group_egress(
                                    GroupId=security_group_id,
                                    IpPermissions=[{
                                        'IpProtocol': '-1',
                                        'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
                                    }]
                                )
                            except:
                                pass  # Default rule might not exist
                        
                        self.ec2_client.authorize_security_group_egress(
                            GroupId=security_group_id,
                            IpPermissions=[ip_permission]
                        )
                        added_rules['egress'].append(rule)
            
            total_rules = len(added_rules['ingress']) + len(added_rules['egress'])
            print(f"[SUCCESS] Created security group {security_group_id} with {total_rules} rules", file=sys.stderr)
            
            return {
                'security_group_id': security_group_id,
                'vpc_id': vpc_id,
                'name': name,
                'description': description,
                'rules': added_rules
            }
        
        except ClientError as e:
            error_msg = f"Failed to create security group: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def add_security_group_rule(self, security_group_id: str, rule: Dict) -> Dict:
        """
        Add a single rule to an existing security group.
        
        Args:
            security_group_id: Security group ID
            rule: Rule dict (same format as create_security_group)
        
        Returns:
            Dictionary with rule details
        """
        try:
            rule_type = rule.get('type', 'ingress')
            protocol = rule.get('protocol', 'tcp')
            from_port = rule.get('from_port', rule.get('port', 80))
            to_port = rule.get('to_port', rule.get('port', 80))
            
            # Build IP permissions
            ip_permission = {
                'IpProtocol': protocol,
            }
            
            # Add ports for TCP/UDP
            if protocol in ['tcp', 'udp']:
                ip_permission['FromPort'] = from_port
                ip_permission['ToPort'] = to_port
            
            # Add source
            if 'cidr' in rule and rule['cidr']:
                ip_permission['IpRanges'] = [{'CidrIp': rule['cidr']}]
            elif 'source_security_group_id' in rule and rule['source_security_group_id']:
                ip_permission['UserIdGroupPairs'] = [{
                    'GroupId': rule['source_security_group_id']
                }]
            else:
                ip_permission['IpRanges'] = [{'CidrIp': '0.0.0.0/0'}]
            
            # Add rule
            if rule_type == 'ingress':
                self.ec2_client.authorize_security_group_ingress(
                    GroupId=security_group_id,
                    IpPermissions=[ip_permission]
                )
            else:
                self.ec2_client.authorize_security_group_egress(
                    GroupId=security_group_id,
                    IpPermissions=[ip_permission]
                )
            
            print(f"[SUCCESS] Added {rule_type} rule to security group {security_group_id}", file=sys.stderr)
            
            return {
                'security_group_id': security_group_id,
                'rule_type': rule_type,
                'rule': rule
            }
        
        except ClientError as e:
            error_msg = f"Failed to add security group rule: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def list_security_groups(self, vpc_id: Optional[str] = None, 
                           tag_filter: Optional[Dict[str, str]] = None) -> List[Dict]:
        """
        List security groups with their rules.
        
        Args:
            vpc_id: Optional VPC ID to filter by
            tag_filter: Optional tag filter
        
        Returns:
            List of security group details
        """
        try:
            filters = []
            
            if vpc_id:
                filters.append({'Name': 'vpc-id', 'Values': [vpc_id]})
            
            if tag_filter:
                for key, value in tag_filter.items():
                    filters.append({'Name': f'tag:{key}', 'Values': [value]})
            
            response = self.ec2_client.describe_security_groups(Filters=filters)
            
            security_groups = []
            for sg in response['SecurityGroups']:
                # Get name from tags
                sg_name = sg['GroupName']
                if 'Tags' in sg:
                    for tag in sg['Tags']:
                        if tag['Key'] == 'Name':
                            sg_name = tag['Value']
                            break
                
                # Parse ingress rules
                ingress_rules = []
                for rule in sg['IpPermissions']:
                    protocol = rule['IpProtocol']
                    from_port = rule.get('FromPort', 'N/A')
                    to_port = rule.get('ToPort', 'N/A')
                    
                    # Get sources
                    sources = []
                    if 'IpRanges' in rule:
                        sources.extend([ip['CidrIp'] for ip in rule['IpRanges']])
                    if 'UserIdGroupPairs' in rule:
                        sources.extend([pair['GroupId'] for pair in rule['UserIdGroupPairs']])
                    
                    ingress_rules.append({
                        'protocol': protocol,
                        'from_port': from_port,
                        'to_port': to_port,
                        'sources': sources
                    })
                
                # Parse egress rules
                egress_rules = []
                for rule in sg['IpPermissionsEgress']:
                    protocol = rule['IpProtocol']
                    from_port = rule.get('FromPort', 'N/A')
                    to_port = rule.get('ToPort', 'N/A')
                    
                    # Get destinations
                    destinations = []
                    if 'IpRanges' in rule:
                        destinations.extend([ip['CidrIp'] for ip in rule['IpRanges']])
                    if 'UserIdGroupPairs' in rule:
                        destinations.extend([pair['GroupId'] for pair in rule['UserIdGroupPairs']])
                    
                    egress_rules.append({
                        'protocol': protocol,
                        'from_port': from_port,
                        'to_port': to_port,
                        'destinations': destinations
                    })
                
                security_groups.append({
                    'security_group_id': sg['GroupId'],
                    'vpc_id': sg.get('VpcId', 'N/A'),
                    'name': sg_name,
                    'description': sg['Description'],
                    'ingress_rules': ingress_rules,
                    'egress_rules': egress_rules
                })
            
            return security_groups
        
        except ClientError as e:
            error_msg = f"Failed to list security groups: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def delete_security_group(self, security_group_id: str) -> Dict:
        """
        Delete a security group.
        
        Args:
            security_group_id: Security group ID to delete
        
        Returns:
            Dictionary with deletion status
        """
        try:
            self.ec2_client.delete_security_group(GroupId=security_group_id)
            
            print(f"[SUCCESS] Deleted security group {security_group_id}", file=sys.stderr)
            
            return {
                'security_group_id': security_group_id,
                'status': 'deleted'
            }
        
        except ClientError as e:
            error_msg = f"Failed to delete security group: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
