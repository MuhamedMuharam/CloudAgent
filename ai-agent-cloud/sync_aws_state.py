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
from cloud_providers.aws import EC2Manager

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
        print("✅ Connected to AWS\n")
    except Exception as e:
        print(f"❌ Failed to connect to AWS: {e}")
        print("⚠️  Make sure your AWS credentials are configured in .env")
        sys.exit(1)
    
    state_manager = StateManager()
    
    # Sync current state
    print("🔄 Fetching current EC2 instances from AWS...")
    instances = ec2_manager.list_instances()
    
    # Filter out terminated instances for cleaner display
    active_instances = [inst for inst in instances if inst['state'] not in ['terminated', 'shutting-down']]
    
    print(f"\n📦 Found {len(instances)} EC2 instances ({len(active_instances)} active):")
    for inst in instances:
        # Mark instance state
        if inst['state'] == 'running':
            marker = "[RUNNING]"
        elif inst['state'] in ['terminated', 'shutting-down']:
            marker = "[TERMINATED]"
        elif inst['state'] == 'pending':
            marker = "[PENDING]"
        elif inst['state'] == 'stopping':
            marker = "[STOPPING]"
        elif inst['state'] == 'stopped':
            marker = "[STOPPED]"
        else:
            marker = f"[{inst['state'].upper()}]"
        print(f"  {marker} {inst['name']} ({inst['id']}) - {inst['type']}")
    
    # Update state
    state_manager.update_aws_ec2_state(instances)
    
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
