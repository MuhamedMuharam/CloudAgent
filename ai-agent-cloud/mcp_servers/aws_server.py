"""
MCP AWS Server
Exposes AWS EC2 operations as MCP tools using FastMCP.

This server implements the Model Context Protocol (MCP) to provide
cloud infrastructure management capabilities to AI agents.

Run this server with: python mcp_servers/aws_server.py
"""

import asyncio
import os
import sys

# Import AWS managers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from cloud_providers.aws.ec2 import EC2Manager
from cloud_providers.aws.vpc import VPCManager
from cloud_providers.aws.security import SecurityGroupManager

# MCP FastMCP import
from fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("mcp-aws-server")

# Initialize EC2 manager with region from environment
region = os.getenv('AWS_REGION', 'us-east-1')
print(f"🚀 Initializing MCP AWS Server (region: {region})", file=sys.stderr)

try:
    ec2_manager = EC2Manager(region=region)
    print("✅ AWS EC2 Manager initialized", file=sys.stderr)
    
    vpc_manager = VPCManager(region=region)
    print("✅ AWS VPC Manager initialized", file=sys.stderr)
    
    security_manager = SecurityGroupManager(region=region)
    print("✅ AWS Security Group Manager initialized", file=sys.stderr)
except Exception as e:
    print(f"❌ Failed to initialize AWS Managers: {e}", file=sys.stderr)
    print("⚠️  Make sure AWS credentials are configured", file=sys.stderr)
    sys.exit(1)


@mcp.tool()
async def aws_list_ec2_instances(tag_filter: dict = None) -> dict:
    """
    List all EC2 instances in the AWS account.
    Returns instance ID, name, type, state, and IP addresses.
    Optionally filter by tags (e.g., only instances managed by AI Agent).
    
    Args:
        tag_filter: Optional tags to filter instances (e.g., {'ManagedBy': 'AIAgent'})
    
    Returns:
        Dictionary with success status, count, and list of instances
    """
    try:
        instances = ec2_manager.list_instances(tag_filter=tag_filter)
        return {
            "success": True,
            "count": len(instances),
            "instances": instances
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_ec2_instance(name: str, cpu: int = 2, ram: int = 4) -> dict:
    """
    Create a new EC2 instance with specified resources.
    Automatically selects appropriate instance type based on CPU/RAM.
    Instances are tagged as 'ManagedBy: AIAgent' for tracking.
    
    Args:
        name: Name for the EC2 instance
        cpu: Number of virtual CPU cores (default: 2)
        ram: RAM in gigabytes (default: 4)
    
    Returns:
        Dictionary with success status, message, and instance details
    """
    try:
        instance = ec2_manager.create_instance(name=name, cpu=cpu, ram=ram)
        return {
            "success": True,
            "message": f"EC2 instance '{name}' created successfully",
            "instance": instance
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_ec2_instance(instance_id: str) -> dict:
    """
    Terminate (delete) an EC2 instance by ID.
    This action is irreversible. The instance will be stopped and deleted.
    
    Args:
        instance_id: EC2 instance ID (e.g., 'i-1234567890abcdef0')
    
    Returns:
        Dictionary with success status and termination details
    """
    try:
        result_data = ec2_manager.delete_instance(instance_id)
        return {
            "success": True,
            "message": f"Instance {instance_id} termination initiated",
            "details": result_data
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_get_ec2_instance_status(instance_id: str) -> dict:
    """
    Get detailed status and information about a specific EC2 instance.
    Returns current state, IP addresses, instance type, and launch time.
    
    Args:
        instance_id: EC2 instance ID to query
    
    Returns:
        Dictionary with success status and instance details
    """
    try:
        status = ec2_manager.get_instance_status(instance_id)
        return {
            "success": True,
            "instance": status
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# ========================================
# VPC MANAGEMENT TOOLS
# ========================================

@mcp.tool()
async def aws_create_vpc(cidr_block: str, name: str, tags: dict = None) -> dict:
    """
    Create a VPC with specified CIDR block.
    
    Args:
        cidr_block: CIDR block for VPC (e.g., '10.0.0.0/16')
        name: Name tag for the VPC
        tags: Optional additional tags as dict
    
    Returns:
        Dictionary with VPC details including vpc_id
    """
    try:
        vpc = vpc_manager.create_vpc(cidr_block, name, tags)
        return {
            "success": True,
            "message": f"VPC '{name}' created successfully",
            "vpc": vpc
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_subnet(vpc_id: str, cidr_block: str, availability_zone: str, 
                           name: str, is_public: bool = False, tags: dict = None) -> dict:
    """
    Create a subnet in a VPC.
    
    Args:
        vpc_id: VPC ID to create subnet in
        cidr_block: CIDR block for subnet (e.g., '10.0.1.0/24')
        availability_zone: AZ for subnet (e.g., 'us-east-1a')
        name: Name tag for the subnet
        is_public: Whether this is a public subnet (will auto-assign public IPs)
        tags: Optional additional tags
    
    Returns:
        Dictionary with subnet details including subnet_id
    """
    try:
        subnet = vpc_manager.create_subnet(vpc_id, cidr_block, availability_zone, name, is_public, tags)
        return {
            "success": True,
            "message": f"Subnet '{name}' created successfully",
            "subnet": subnet
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_internet_gateway(vpc_id: str, name: str, tags: dict = None) -> dict:
    """
    Create and attach an Internet Gateway to a VPC.
    Internet Gateways enable internet access for public subnets.
    
    Args:
        vpc_id: VPC ID to attach IGW to
        name: Name tag for the IGW
        tags: Optional additional tags
    
    Returns:
        Dictionary with IGW details including igw_id
    """
    try:
        igw = vpc_manager.create_internet_gateway(vpc_id, name, tags)
        return {
            "success": True,
            "message": f"Internet Gateway '{name}' created and attached to VPC",
            "internet_gateway": igw
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_nat_gateway(subnet_id: str, name: str, tags: dict = None) -> dict:
    """
    Create a NAT Gateway in a public subnet.
    NAT Gateways enable internet access for instances in private subnets.
    Note: This allocates an Elastic IP which costs money.
    
    Args:
        subnet_id: Public subnet ID to place NAT Gateway in
        name: Name tag for the NAT Gateway
        tags: Optional additional tags
    
    Returns:
        Dictionary with NAT Gateway details including nat_gateway_id and public_ip
    """
    try:
        nat = vpc_manager.create_nat_gateway(subnet_id, name, tags)
        return {
            "success": True,
            "message": f"NAT Gateway '{name}' created with public IP {nat['public_ip']}",
            "nat_gateway": nat
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_create_route_table(vpc_id: str, name: str, routes: list = None, tags: dict = None) -> dict:
    """
    Create a route table with optional routes.
    
    Args:
        vpc_id: VPC ID to create route table in
        name: Name tag for the route table
        routes: List of route dicts:
                [{'destination': '0.0.0.0/0', 'gateway_id': 'igw-xxx'}]
                or
                [{'destination': '0.0.0.0/0', 'nat_gateway_id': 'nat-xxx'}]
        tags: Optional additional tags
    
    Returns:
        Dictionary with route table details including route_table_id
    """
    try:
        route_table = vpc_manager.create_route_table(vpc_id, name, routes, tags)
        return {
            "success": True,
            "message": f"Route table '{name}' created successfully",
            "route_table": route_table
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_associate_route_table(route_table_id: str, subnet_id: str) -> dict:
    """
    Associate a route table with a subnet.
    This determines how traffic from the subnet is routed.
    
    Args:
        route_table_id: Route table ID to associate
        subnet_id: Subnet ID to associate with
    
    Returns:
        Dictionary with association details
    """
    try:
        association = vpc_manager.associate_route_table(route_table_id, subnet_id)
        return {
            "success": True,
            "message": f"Route table associated with subnet successfully",
            "association": association
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_list_route_tables(vpc_id: str) -> dict:
    """
    List all route tables for a specific VPC with their routes and subnet associations.
    Use this to check what route tables already exist before creating new ones.
    
    Args:
        vpc_id: VPC ID to list route tables for
    
    Returns:
        Dictionary with list of route tables including:
        - Route table ID and name
        - Whether it's the main route table
        - All routes (destination -> target)
        - Associated subnets
    """
    try:
        # Get VPC details which includes route tables
        all_vpcs = vpc_manager.list_vpcs()
        target_vpc = next((vpc for vpc in all_vpcs if vpc['vpc_id'] == vpc_id), None)
        
        if not target_vpc:
            return {
                "success": False,
                "error": f"VPC not found: {vpc_id}"
            }
        
        route_tables = target_vpc.get('route_tables', [])
        
        return {
            "success": True,
            "vpc_id": vpc_id,
            "vpc_name": target_vpc['name'],
            "count": len(route_tables),
            "route_tables": route_tables
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_list_vpcs(tag_filter: dict = None) -> dict:
    """
    List all VPCs with their subnets, internet gateways, NAT gateways, route tables, and details.
    This returns comprehensive information about VPC networking topology.
    
    Args:
        tag_filter: Optional tag filter (e.g., {'ManagedBy': 'AIAgent'})
    
    Returns:
        Dictionary with list of VPCs and their complete network configuration including:
        - Subnets (with CIDR, AZ, type)
        - Internet Gateways
        - NAT Gateways (with public IPs)
        - Route Tables (with routes and subnet associations)
    """
    try:
        vpcs = vpc_manager.list_vpcs(tag_filter)
        return {
            "success": True,
            "count": len(vpcs),
            "vpcs": vpcs
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_get_vpc_details(vpc_id: str = None, vpc_name: str = None) -> dict:
    """
    Get detailed information about a specific VPC by ID or name.
    Use this to check if route tables, IGW, NAT gateways already exist before creating them.
    
    Args:
        vpc_id: VPC ID to query (e.g., 'vpc-123abc')
        vpc_name: VPC name to search for (alternative to vpc_id)
    
    Returns:
        Dictionary with detailed VPC information including:
        - All subnets with their CIDR blocks and availability zones
        - All route tables with their routes and associated subnets
        - Internet Gateways
        - NAT Gateways with public IPs and states
        - Security groups count
    """
    try:
        # Get all VPCs
        all_vpcs = vpc_manager.list_vpcs()
        
        # Find the requested VPC
        target_vpc = None
        if vpc_id:
            target_vpc = next((vpc for vpc in all_vpcs if vpc['vpc_id'] == vpc_id), None)
        elif vpc_name:
            target_vpc = next((vpc for vpc in all_vpcs if vpc['name'] == vpc_name), None)
        
        if not target_vpc:
            return {
                "success": False,
                "error": f"VPC not found with {'ID: ' + vpc_id if vpc_id else 'name: ' + vpc_name}"
            }
        
        # Return detailed information
        return {
            "success": True,
            "vpc": target_vpc,
            "summary": {
                "vpc_id": target_vpc['vpc_id'],
                "name": target_vpc['name'],
                "cidr_block": target_vpc['cidr_block'],
                "subnet_count": len(target_vpc.get('subnets', [])),
                "route_table_count": len(target_vpc.get('route_tables', [])),
                "internet_gateway_count": len(target_vpc.get('internet_gateways', [])),
                "nat_gateway_count": len(target_vpc.get('nat_gateways', [])),
                "has_internet_gateway": len(target_vpc.get('internet_gateways', [])) > 0,
                "has_nat_gateway": len(target_vpc.get('nat_gateways', [])) > 0
            }
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_vpc(vpc_id: str, force: bool = False) -> dict:
    """
    Delete a VPC and optionally all its dependencies.
    
    Args:
        vpc_id: VPC ID (vpc-xxx) or VPC name to delete
        force: If True, automatically delete all dependencies in the correct order:
               - Terminate EC2 instances
               - Disassociate and delete route tables
               - Delete NAT Gateways (waits for full deletion)
               - Release Elastic IPs
               - Detach and delete Internet Gateways
               - Delete subnets
               - Delete security groups
               - Delete VPC
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = vpc_manager.delete_vpc(vpc_id, force)
        return {
            "success": True,
            "message": f"VPC deleted successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_subnet(subnet_id: str) -> dict:
    """
    Delete a subnet.
    Note: Subnet must not have any dependencies (instances, ENIs, etc.).
    
    Args:
        subnet_id: Subnet ID to delete
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = vpc_manager.delete_subnet(subnet_id)
        return {
            "success": True,
            "message": f"Subnet {subnet_id} deleted successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_internet_gateway(igw_id: str, vpc_id: str = None) -> dict:
    """
    Detach and delete an Internet Gateway.
    
    Args:
        igw_id: Internet Gateway ID to delete
        vpc_id: Optional VPC ID to detach from (if not provided, will detect automatically)
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = vpc_manager.delete_internet_gateway(igw_id, vpc_id)
        return {
            "success": True,
            "message": f"Internet Gateway {igw_id} deleted successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_nat_gateway(nat_gateway_id: str) -> dict:
    """
    Delete a NAT Gateway.
    Note: NAT Gateway deletion is asynchronous and can take several minutes.
    
    Args:
        nat_gateway_id: NAT Gateway ID to delete
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = vpc_manager.delete_nat_gateway(nat_gateway_id)
        return {
            "success": True,
            "message": f"NAT Gateway {nat_gateway_id} deletion initiated",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_route_table(route_table_id: str) -> dict:
    """
    Delete a route table.
    Note: Cannot delete the main route table or route tables with subnet associations.
    
    Args:
        route_table_id: Route table ID to delete
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = vpc_manager.delete_route_table(route_table_id)
        return {
            "success": True,
            "message": f"Route table {route_table_id} deleted successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# ========================================
# SECURITY GROUP TOOLS
# ========================================

@mcp.tool()
async def aws_create_security_group(vpc_id: str, name: str, description: str, 
                                   rules: list = None, tags: dict = None) -> dict:
    """
    Create a security group with inbound/outbound rules.
    
    Args:
        vpc_id: VPC ID to create security group in
        name: Name for the security group
        description: Description of the security group's purpose
        rules: List of rule dicts. Each rule must have:
               - 'type': 'ingress' or 'egress'
               - 'protocol': 'tcp', 'udp', 'icmp', or '-1' (all)
               - 'port': 80 (or use 'from_port' and 'to_port' for ranges)
               - Source/Destination (choose ONE):
                 * 'cidr': '0.0.0.0/0' (for IP-based access)
                 * 'source_security_group_id': 'sg-xxx' (for SG-to-SG access)
               
               Examples:
               [
                   # Allow HTTP from anywhere
                   {'type': 'ingress', 'protocol': 'tcp', 'port': 80, 'cidr': '0.0.0.0/0'},
                   
                   # Allow port 8080 from another security group
                   {'type': 'ingress', 'protocol': 'tcp', 'port': 8080, 
                    'source_security_group_id': 'sg-1234567890abcdef0'}
               ]
        tags: Optional additional tags
    
    Returns:
        Dictionary with security group details including security_group_id
    """
    try:
        sg = security_manager.create_security_group(vpc_id, name, description, rules, tags)
        return {
            "success": True,
            "message": f"Security group '{name}' created successfully",
            "security_group": sg
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_add_security_group_rule(security_group_id: str, rule: dict) -> dict:
    """
    Add a single rule to an existing security group.
    
    Args:
        security_group_id: Security group ID to add rule to
        rule: Rule dict with:
              - 'type': 'ingress' or 'egress'
              - 'protocol': 'tcp', 'udp', 'icmp', or '-1'
              - 'port': port number or 'from_port' and 'to_port'
              - Source (choose ONE):
                * 'cidr': '10.0.0.0/16' (for IP-based access)
                * 'source_security_group_id': 'sg-xxx' (for security group access)
              
              Example allowing database access from app tier:
              {
                  'type': 'ingress',
                  'protocol': 'tcp', 
                  'port': 3306,
                  'source_security_group_id': 'sg-app-tier-id'
              }
    
    Returns:
        Dictionary with rule addition status
    """
    try:
        result = security_manager.add_security_group_rule(security_group_id, rule)
        return {
            "success": True,
            "message": f"Rule added to security group successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_list_security_groups(vpc_id: str = None, tag_filter: dict = None) -> dict:
    """
    List security groups with their rules.
    
    Args:
        vpc_id: Optional VPC ID to filter by
        tag_filter: Optional tag filter (e.g., {'ManagedBy': 'AIAgent'})
    
    Returns:
        Dictionary with list of security groups and their rules
    """
    try:
        sgs = security_manager.list_security_groups(vpc_id, tag_filter)
        return {
            "success": True,
            "count": len(sgs),
            "security_groups": sgs
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def aws_delete_security_group(security_group_id: str) -> dict:
    """
    Delete a security group.
    Note: Cannot delete security groups that are in use by instances.
    
    Args:
        security_group_id: Security group ID to delete
    
    Returns:
        Dictionary with deletion status
    """
    try:
        result = security_manager.delete_security_group(security_group_id)
        return {
            "success": True,
            "message": f"Security group {security_group_id} deleted successfully",
            "details": result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


if __name__ == "__main__":
    # FastMCP handles the stdio server setup automatically
    print("📡 Starting MCP AWS Server...", file=sys.stderr)
    mcp.run()

