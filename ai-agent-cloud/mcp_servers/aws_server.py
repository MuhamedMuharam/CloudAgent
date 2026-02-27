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

# Import AWS EC2 manager
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from cloud_providers.aws.ec2 import EC2Manager

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
except Exception as e:
    print(f"❌ Failed to initialize EC2 Manager: {e}", file=sys.stderr)
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


if __name__ == "__main__":
    # FastMCP handles the stdio server setup automatically
    print("📡 Starting MCP AWS Server...", file=sys.stderr)
    mcp.run()

