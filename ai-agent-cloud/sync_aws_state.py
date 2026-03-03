"""
Sync AWS State Utility
Syncs current AWS infrastructure state into the agent's state tracking.

This is useful for:
- Initial setup (capture existing AWS resources)
- Periodic reconciliation (detect manual changes)
- After manual operations in AWS Console

Usage:
    python sync_aws_state.py
"""

import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from agent.state_manager import StateManager
from cloud_providers.aws import EC2Manager, VPCManager, SecurityGroupManager

def main():
    print("=" * 60)
    print("AWS State Sync Utility")
    print("=" * 60)
    print()
    
    # Load environment variables
    load_dotenv()
    
    # Initialize managers
    try:
        region = os.getenv('AWS_REGION', 'us-east-1')
        print(f"🔗 Connecting to AWS (region: {region})...")
        ec2_manager = EC2Manager(region=region)
        vpc_manager = VPCManager(region=region)
        security_manager = SecurityGroupManager(region=region)
        print("✅ Connected to AWS\n")
    except Exception as e:
        print(f"❌ Failed to connect to AWS: {e}")
        print("⚠️  Make sure your AWS credentials are configured in .env")
        sys.exit(1)
    
    state_manager = StateManager()
    
    # Sync all resources
    print("🔄 Fetching current EC2 instances from AWS...")
    instances = ec2_manager.list_instances()
    
    print(f"🔄 Fetching VPCs from AWS...")
    vpcs = vpc_manager.list_vpcs()
    
    print(f"🔄 Fetching Security Groups from AWS...")
    security_groups = security_manager.list_security_groups()
    
    print(f"\n📊 Organizing resources hierarchically...")
    state_manager.update_aws_hierarchical_state(vpcs, instances, security_groups)
    
    # Display summary
    print(f"\n📦 Resource Summary:")
    print(f"  VPCs: {len(vpcs)}")
    print(f"  EC2 Instances: {len(instances)}")
    print(f"  Security Groups: {len(security_groups)}")
    
    print(f"\n🗂️  Hierarchical Organization:")
    
    # Show organization by VPC
    for vpc in vpcs:
        vpc_id = vpc['vpc_id']
        subnet_count = len(vpc.get('subnets', []))
        igw_count = len(vpc.get('internet_gateways', []))
        nat_count = len(vpc.get('nat_gateways', []))
        rt_count = len(vpc.get('route_tables', []))
        
        # Count instances in this VPC
        vpc_instances = [inst for inst in instances if inst.get('vpc_id') == vpc_id]
        # Count security groups in this VPC
        vpc_sgs = [sg for sg in security_groups if sg.get('vpc_id') == vpc_id]
        
        print(f"\n  📦 VPC: {vpc['name']} ({vpc['vpc_id']}) - {vpc['cidr_block']}")
        print(f"       Resources: {subnet_count} subnets, {len(vpc_instances)} instances, {len(vpc_sgs)} security groups")
        print(f"       Networking: {igw_count} IGW, {nat_count} NAT Gateway, {rt_count} Route Tables")
        
        # Show Internet Gateways
        if igw_count > 0:
            print(f"\n       🌐 Internet Gateways:")
            for igw in vpc.get('internet_gateways', []):
                print(f"          └─ {igw['name']} ({igw['igw_id']})")
        
        # Show NAT Gateways
        if nat_count > 0:
            print(f"\n       🔀 NAT Gateways:")
            for nat in vpc.get('nat_gateways', []):
                print(f"          └─ {nat['name']} ({nat['nat_gateway_id']}) - {nat['state']}")
                print(f"             Public IP: {nat['public_ip']}, Subnet: {nat['subnet_id']}")
        
        # Show Route Tables
        if rt_count > 0:
            print(f"\n       🗺️  Route Tables:")
            for rt in vpc.get('route_tables', []):
                main_marker = " [MAIN]" if rt.get('is_main', False) else ""
                assoc_count = len(rt.get('associated_subnets', []))
                print(f"          └─ {rt['name']} ({rt['route_table_id']}){main_marker} - {assoc_count} associated subnets")
                
                # Show routes
                for route in rt.get('routes', []):
                    print(f"             ├─ {route['destination']} → {route['target']} [{route['state']}]")
                
                # Show associated subnets
                if assoc_count > 0:
                    print(f"             └─ Associated with:")
                    for assoc in rt.get('associated_subnets', []):
                        # Find subnet name
                        subnet_name = assoc['subnet_id']
                        for subnet in vpc.get('subnets', []):
                            if subnet['subnet_id'] == assoc['subnet_id']:
                                subnet_name = f"{subnet['name']} ({assoc['subnet_id']})"
                                break
                        print(f"                └─ {subnet_name}")
        
        # Show ALL subnets with their details
        print(f"\n       📍 Subnets:")
        for subnet in vpc.get('subnets', []):
            subnet_id = subnet['subnet_id']
            subnet_type = "Public" if subnet.get('map_public_ip_on_launch', False) else "Private"
            subnet_cidr = subnet.get('cidr_block', 'N/A')
            subnet_az = subnet.get('availability_zone', 'N/A')
            
            # Count instances in this subnet
            subnet_instances = [inst for inst in vpc_instances if inst.get('subnet_id') == subnet_id]
            
            print(f"          └─ {subnet['name']} ({subnet_id}) [{subnet_type}]")
            print(f"             CIDR: {subnet_cidr}, AZ: {subnet_az}, Instances: {len(subnet_instances)}")
            
            # Show instances in this subnet
            if subnet_instances:
                for inst in subnet_instances:
                    state_marker = "[RUNNING]" if inst['state'] == 'running' else f"[{inst['state'].upper()}]"
                    print(f"             └─ {state_marker} {inst['name']} ({inst['id']}) - {inst['type']}")
        
        # Show security groups for this VPC
        if vpc_sgs:
            print(f"\n       🔒 Security Groups:")
            for sg in vpc_sgs:
                ingress_count = len(sg.get('ingress_rules', []))
                egress_count = len(sg.get('egress_rules', []))
                print(f"          └─ {sg['name']} ({sg['security_group_id']}) - {ingress_count} ingress, {egress_count} egress rules")
    
    # Show orphaned resources
    vpc_ids = {vpc['vpc_id'] for vpc in vpcs}
    orphaned_instances = [inst for inst in instances if inst.get('vpc_id') == 'N/A' or inst.get('vpc_id') not in vpc_ids]
    orphaned_sgs = [sg for sg in security_groups if sg.get('vpc_id') == 'N/A' or sg.get('vpc_id') not in vpc_ids]
    
    if orphaned_instances or orphaned_sgs:
        print(f"\n  ⚠️  Orphaned Resources (not in tracked VPCs):")
        if orphaned_instances:
            print(f"       Instances: {len(orphaned_instances)}")
            for inst in orphaned_instances:
                print(f"       └─ {inst['name']} ({inst['id']})")
        if orphaned_sgs:
            print(f"       Security Groups: {len(orphaned_sgs)}")
    
    print(f"\n✅ State synced successfully!")
    print(f"📁 State saved to: {state_manager.state_file}")
    
    # Show note about terminated instances
    terminated_count = len([inst for inst in instances if inst['state'] in ['terminated', 'shutting-down']])
    if terminated_count > 0:
        print(f"\n💡 Note: {terminated_count} terminated/shutting-down instance(s) shown above.")
        print("   These will disappear from AWS after ~1 hour automatically.")
    
    # Show statistics
    print("\n📊 Current Statistics:")
    stats = state_manager.get_statistics()
    print(f"  Total Goals Executed: {stats.get('total_goals_executed', 0)}")
    print(f"  Total Resources Created: {stats.get('total_resources_created', 0)}")
    print(f"  Total Resources Deleted: {stats.get('total_resources_deleted', 0)}")
    
    print("\n💡 Tip: Run 'python view_state.py' to see full state report")
    print("=" * 60)

if __name__ == "__main__":
    main()
