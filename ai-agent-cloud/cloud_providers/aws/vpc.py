"""
AWS VPC Manager
Handles VPC, Subnets, Internet Gateways, NAT Gateways, and Route Tables.
"""

import boto3
import sys
from botocore.exceptions import ClientError
from typing import List, Dict, Optional


class VPCManager:
    """Manages AWS VPC networking resources."""
    
    def __init__(self, region: str = 'us-east-1'):
        """
        Initialize VPC Manager.
        
        Args:
            region: AWS region (default: us-east-1)
        """
        self.region = region
        self.ec2_client = boto3.client('ec2', region_name=region)
        self.ec2_resource = boto3.resource('ec2', region_name=region)
    
    def create_vpc(self, cidr_block: str, name: str, tags: Optional[Dict[str, str]] = None) -> Dict:
        """
        Create a VPC with specified CIDR block.
        
        Args:
            cidr_block: CIDR block for VPC (e.g., '10.0.0.0/16')
            name: Name tag for the VPC
            tags: Additional tags
        
        Returns:
            Dictionary with VPC details
        """
        try:
            # Create VPC
            vpc = self.ec2_resource.create_vpc(CidrBlock=cidr_block)
            vpc_id = vpc.id
            
            # Wait for VPC to be available
            vpc.wait_until_available()
            
            # Enable DNS hostname support
            vpc.modify_attribute(EnableDnsHostnames={'Value': True})
            vpc.modify_attribute(EnableDnsSupport={'Value': True})
            
            # Create tags
            vpc_tags = [
                {'Key': 'Name', 'Value': name},
                {'Key': 'ManagedBy', 'Value': 'AIAgent'}
            ]
            
            if tags:
                for key, value in tags.items():
                    vpc_tags.append({'Key': key, 'Value': value})
            
            vpc.create_tags(Tags=vpc_tags)
            
            print(f"[SUCCESS] Created VPC {vpc_id} with CIDR {cidr_block}", file=sys.stderr)
            
            return {
                'vpc_id': vpc_id,
                'cidr_block': cidr_block,
                'name': name,
                'state': 'available'
            }
        
        except ClientError as e:
            error_msg = f"Failed to create VPC: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def create_subnet(self, vpc_id: str, cidr_block: str, availability_zone: str, 
                     name: str, is_public: bool = False, tags: Optional[Dict[str, str]] = None) -> Dict:
        """
        Create a subnet in a VPC.
        
        Args:
            vpc_id: VPC ID to create subnet in
            cidr_block: CIDR block for subnet (e.g., '10.0.1.0/24')
            availability_zone: AZ for subnet (e.g., 'us-east-1a')
            name: Name tag for the subnet
            is_public: Whether this is a public subnet
            tags: Additional tags
        
        Returns:
            Dictionary with subnet details
        """
        try:
            # Create subnet
            subnet = self.ec2_resource.create_subnet(
                VpcId=vpc_id,
                CidrBlock=cidr_block,
                AvailabilityZone=availability_zone
            )
            subnet_id = subnet.id
            
            # Enable auto-assign public IP for public subnets
            if is_public:
                subnet.meta.client.modify_subnet_attribute(
                    SubnetId=subnet_id,
                    MapPublicIpOnLaunch={'Value': True}
                )
            
            # Create tags
            subnet_tags = [
                {'Key': 'Name', 'Value': name},
                {'Key': 'Type', 'Value': 'Public' if is_public else 'Private'},
                {'Key': 'ManagedBy', 'Value': 'AIAgent'}
            ]
            
            if tags:
                for key, value in tags.items():
                    subnet_tags.append({'Key': key, 'Value': value})
            
            subnet.create_tags(Tags=subnet_tags)
            
            subnet_type = "public" if is_public else "private"
            print(f"[SUCCESS] Created {subnet_type} subnet {subnet_id} in {availability_zone}", file=sys.stderr)
            
            return {
                'subnet_id': subnet_id,
                'vpc_id': vpc_id,
                'cidr_block': cidr_block,
                'availability_zone': availability_zone,
                'is_public': is_public,
                'name': name
            }
        
        except ClientError as e:
            error_msg = f"Failed to create subnet: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def create_internet_gateway(self, vpc_id: str, name: str, tags: Optional[Dict[str, str]] = None) -> Dict:
        """
        Create and attach an Internet Gateway to a VPC.
        
        Args:
            vpc_id: VPC ID to attach IGW to
            name: Name tag for the IGW
            tags: Additional tags
        
        Returns:
            Dictionary with IGW details
        """
        try:
            # Create Internet Gateway
            igw = self.ec2_resource.create_internet_gateway()
            igw_id = igw.id
            
            # Attach to VPC
            igw.attach_to_vpc(VpcId=vpc_id)
            
            # Create tags
            igw_tags = [
                {'Key': 'Name', 'Value': name},
                {'Key': 'ManagedBy', 'Value': 'AIAgent'}
            ]
            
            if tags:
                for key, value in tags.items():
                    igw_tags.append({'Key': key, 'Value': value})
            
            igw.create_tags(Tags=igw_tags)
            
            print(f"[SUCCESS] Created Internet Gateway {igw_id} and attached to VPC {vpc_id}", file=sys.stderr)
            
            return {
                'igw_id': igw_id,
                'vpc_id': vpc_id,
                'name': name,
                'state': 'attached'
            }
        
        except ClientError as e:
            error_msg = f"Failed to create Internet Gateway: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def create_nat_gateway(self, subnet_id: str, name: str, tags: Optional[Dict[str, str]] = None) -> Dict:
        """
        Create a NAT Gateway in a public subnet.
        
        Args:
            subnet_id: Public subnet ID to place NAT Gateway in
            name: Name tag for the NAT Gateway
            tags: Additional tags
        
        Returns:
            Dictionary with NAT Gateway details
        """
        try:
            # Allocate Elastic IP for NAT Gateway
            allocation = self.ec2_client.allocate_address(Domain='vpc')
            allocation_id = allocation['AllocationId']
            
            # Create NAT Gateway
            response = self.ec2_client.create_nat_gateway(
                SubnetId=subnet_id,
                AllocationId=allocation_id,
                TagSpecifications=[{
                    'ResourceType': 'natgateway',
                    'Tags': [
                        {'Key': 'Name', 'Value': name},
                        {'Key': 'ManagedBy', 'Value': 'AIAgent'}
                    ] + ([{'Key': k, 'Value': v} for k, v in tags.items()] if tags else [])
                }]
            )
            
            nat_gateway_id = response['NatGateway']['NatGatewayId']
            
            # Wait for NAT Gateway to be available
            waiter = self.ec2_client.get_waiter('nat_gateway_available')
            print(f"[INFO] Waiting for NAT Gateway {nat_gateway_id} to be available...", file=sys.stderr)
            waiter.wait(NatGatewayIds=[nat_gateway_id])
            
            print(f"[SUCCESS] Created NAT Gateway {nat_gateway_id} with Elastic IP {allocation['PublicIp']}", file=sys.stderr)
            
            return {
                'nat_gateway_id': nat_gateway_id,
                'subnet_id': subnet_id,
                'allocation_id': allocation_id,
                'public_ip': allocation['PublicIp'],
                'name': name,
                'state': 'available'
            }
        
        except ClientError as e:
            error_msg = f"Failed to create NAT Gateway: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def create_route_table(self, vpc_id: str, name: str, routes: Optional[List[Dict]] = None, 
                          tags: Optional[Dict[str, str]] = None) -> Dict:
        """
        Create a route table and associate it with subnets.
        
        Args:
            vpc_id: VPC ID to create route table in
            name: Name tag for the route table
            routes: List of route dicts with 'destination' and 'target' (igw_id or nat_gateway_id)
                   Example: [{'destination': '0.0.0.0/0', 'gateway_id': 'igw-xxx'}]
            tags: Additional tags
        
        Returns:
            Dictionary with route table details
        """
        try:
            # Create route table
            route_table = self.ec2_resource.create_route_table(VpcId=vpc_id)
            route_table_id = route_table.id
            
            # Create tags
            rt_tags = [
                {'Key': 'Name', 'Value': name},
                {'Key': 'ManagedBy', 'Value': 'AIAgent'}
            ]
            
            if tags:
                for key, value in tags.items():
                    rt_tags.append({'Key': key, 'Value': value})
            
            route_table.create_tags(Tags=rt_tags)
            
            # Add routes if specified
            added_routes = []
            if routes:
                for route in routes:
                    destination = route.get('destination')
                    
                    # Determine target type
                    if 'gateway_id' in route:
                        # Internet Gateway
                        route_table.create_route(
                            DestinationCidrBlock=destination,
                            GatewayId=route['gateway_id']
                        )
                        added_routes.append({
                            'destination': destination,
                            'target': route['gateway_id'],
                            'target_type': 'internet_gateway'
                        })
                    elif 'nat_gateway_id' in route:
                        # NAT Gateway
                        route_table.create_route(
                            DestinationCidrBlock=destination,
                            NatGatewayId=route['nat_gateway_id']
                        )
                        added_routes.append({
                            'destination': destination,
                            'target': route['nat_gateway_id'],
                            'target_type': 'nat_gateway'
                        })
            
            print(f"[SUCCESS] Created route table {route_table_id} with {len(added_routes)} routes", file=sys.stderr)
            
            return {
                'route_table_id': route_table_id,
                'vpc_id': vpc_id,
                'name': name,
                'routes': added_routes
            }
        
        except ClientError as e:
            error_msg = f"Failed to create route table: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def associate_route_table(self, route_table_id: str, subnet_id: str) -> Dict:
        """
        Associate a route table with a subnet.
        
        Args:
            route_table_id: Route table ID
            subnet_id: Subnet ID to associate with
        
        Returns:
            Dictionary with association details
        """
        try:
            response = self.ec2_client.associate_route_table(
                RouteTableId=route_table_id,
                SubnetId=subnet_id
            )
            
            association_id = response['AssociationId']
            
            print(f"[SUCCESS] Associated route table {route_table_id} with subnet {subnet_id}", file=sys.stderr)
            
            return {
                'association_id': association_id,
                'route_table_id': route_table_id,
                'subnet_id': subnet_id
            }
        
        except ClientError as e:
            error_msg = f"Failed to associate route table: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def list_vpcs(self, tag_filter: Optional[Dict[str, str]] = None) -> List[Dict]:
        """
        List all VPCs with their subnets and gateways.
        
        Args:
            tag_filter: Optional tag filter (e.g., {'ManagedBy': 'AIAgent'})
        
        Returns:
            List of VPC details
        """
        try:
            filters = []
            if tag_filter:
                for key, value in tag_filter.items():
                    filters.append({'Name': f'tag:{key}', 'Values': [value]})
            
            vpcs = list(self.ec2_resource.vpcs.filter(Filters=filters))
            
            vpc_list = []
            for vpc in vpcs:
                # Get VPC name from tags
                vpc_name = 'N/A'
                if vpc.tags:
                    for tag in vpc.tags:
                        if tag['Key'] == 'Name':
                            vpc_name = tag['Value']
                            break
                
                # Get subnets
                subnets = []
                for subnet in vpc.subnets.all():
                    subnet_name = 'N/A'
                    subnet_type = 'Private'
                    if subnet.tags:
                        for tag in subnet.tags:
                            if tag['Key'] == 'Name':
                                subnet_name = tag['Value']
                            elif tag['Key'] == 'Type':
                                subnet_type = tag['Value']
                    
                    subnets.append({
                        'subnet_id': subnet.id,
                        'cidr_block': subnet.cidr_block,
                        'availability_zone': subnet.availability_zone,
                        'name': subnet_name,
                        'type': subnet_type,
                        'map_public_ip_on_launch': subnet.map_public_ip_on_launch
                    })
                
                # Get Internet Gateways
                igws = []
                for igw in vpc.internet_gateways.all():
                    igw_name = 'N/A'
                    if igw.tags:
                        for tag in igw.tags:
                            if tag['Key'] == 'Name':
                                igw_name = tag['Value']
                                break
                    igws.append({'igw_id': igw.id, 'name': igw_name})
                
                # Get NAT Gateways
                nat_gateways = []
                nat_response = self.ec2_client.describe_nat_gateways(
                    Filters=[{'Name': 'vpc-id', 'Values': [vpc.id]}]
                )
                for nat in nat_response.get('NatGateways', []):
                    if nat['State'] not in ['deleted', 'deleting']:
                        nat_name = 'N/A'
                        for tag in nat.get('Tags', []):
                            if tag['Key'] == 'Name':
                                nat_name = tag['Value']
                                break
                        
                        # Get public IP
                        public_ip = 'N/A'
                        for addr in nat.get('NatGatewayAddresses', []):
                            if 'PublicIp' in addr:
                                public_ip = addr['PublicIp']
                                break
                        
                        nat_gateways.append({
                            'nat_gateway_id': nat['NatGatewayId'],
                            'name': nat_name,
                            'subnet_id': nat['SubnetId'],
                            'state': nat['State'],
                            'public_ip': public_ip
                        })
                
                # Get Route Tables
                route_tables = []
                route_table_response = self.ec2_client.describe_route_tables(
                    Filters=[{'Name': 'vpc-id', 'Values': [vpc.id]}]
                )
                for rt in route_table_response.get('RouteTables', []):
                    rt_name = 'N/A'
                    is_main = False
                    for tag in rt.get('Tags', []):
                        if tag['Key'] == 'Name':
                            rt_name = tag['Value']
                    
                    # Check if this is the main route table
                    for assoc in rt.get('Associations', []):
                        if assoc.get('Main', False):
                            is_main = True
                            if rt_name == 'N/A':
                                rt_name = 'Main'
                            break
                    
                    # Get associated subnets
                    associated_subnets = []
                    for assoc in rt.get('Associations', []):
                        if 'SubnetId' in assoc:
                            associated_subnets.append({
                                'subnet_id': assoc['SubnetId'],
                                'association_id': assoc['RouteTableAssociationId']
                            })
                    
                    # Get routes
                    routes = []
                    for route in rt.get('Routes', []):
                        route_entry = {
                            'destination': route.get('DestinationCidrBlock', route.get('DestinationIpv6CidrBlock', 'N/A')),
                            'target': 'local' if route.get('GatewayId') == 'local' else 
                                     route.get('GatewayId', route.get('NatGatewayId', route.get('InstanceId', 'N/A'))),
                            'state': route.get('State', 'N/A')
                        }
                        routes.append(route_entry)
                    
                    route_tables.append({
                        'route_table_id': rt['RouteTableId'],
                        'name': rt_name,
                        'is_main': is_main,
                        'associated_subnets': associated_subnets,
                        'routes': routes
                    })
                
                vpc_list.append({
                    'vpc_id': vpc.id,
                    'cidr_block': vpc.cidr_block,
                    'name': vpc_name,
                    'state': vpc.state,
                    'subnets': subnets,
                    'internet_gateways': igws,
                    'nat_gateways': nat_gateways,
                    'route_tables': route_tables
                })
            
            return vpc_list
        
        except ClientError as e:
            error_msg = f"Failed to list VPCs: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
    
    def delete_vpc(self, vpc_id: str, force: bool = False) -> Dict:
        """
        Delete a VPC and optionally all its dependencies.
        
        Args:
            vpc_id: VPC ID to delete
            force: If True, delete all dependencies first
        
        Returns:
            Dictionary with deletion status
        """
        try:
            vpc = self.ec2_resource.Vpc(vpc_id)
            
            if force:
                # Delete all dependencies
                print(f"[INFO] Force deleting VPC {vpc_id} and all dependencies...", file=sys.stderr)
                
                # Delete NAT Gateways
                nat_gateways = self.ec2_client.describe_nat_gateways(
                    Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}]
                )
                for nat in nat_gateways['NatGateways']:
                    if nat['State'] != 'deleted':
                        print(f"[INFO] Deleting NAT Gateway {nat['NatGatewayId']}", file=sys.stderr)
                        self.ec2_client.delete_nat_gateway(NatGatewayId=nat['NatGatewayId'])
                
                # Delete subnets
                for subnet in vpc.subnets.all():
                    print(f"[INFO] Deleting subnet {subnet.id}", file=sys.stderr)
                    subnet.delete()
                
                # Detach and delete Internet Gateways
                for igw in vpc.internet_gateways.all():
                    print(f"[INFO] Detaching and deleting IGW {igw.id}", file=sys.stderr)
                    igw.detach_from_vpc(VpcId=vpc_id)
                    igw.delete()
                
                # Delete route tables (except main)
                for rt in vpc.route_tables.all():
                    if not any(assoc.get('Main') for assoc in rt.associations_attribute or []):
                        print(f"[INFO] Deleting route table {rt.id}", file=sys.stderr)
                        rt.delete()
            
            # Delete VPC
            vpc.delete()
            
            print(f"[SUCCESS] Deleted VPC {vpc_id}", file=sys.stderr)
            
            return {
                'vpc_id': vpc_id,
                'status': 'deleted'
            }
        
        except ClientError as e:
            error_msg = f"Failed to delete VPC: {e}"
            print(f"[ERROR] {error_msg}", file=sys.stderr)
            raise Exception(error_msg)
