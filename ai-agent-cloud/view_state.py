"""
State Viewer Utility
View current agent state, statistics, and audit logs.

Usage:
    python view_state.py              # Show full report
    python view_state.py --stats      # Show statistics only
    python view_state.py --log        # Show recent actions
    python view_state.py --sync       # Sync from AWS and show report
"""

import sys
import os
import argparse
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from agent.state_manager import StateManager

def main():
    parser = argparse.ArgumentParser(description='View AI Agent state and statistics')
    parser.add_argument('--stats', action='store_true', help='Show statistics only')
    parser.add_argument('--log', action='store_true', help='Show audit log')
    parser.add_argument('--log-limit', type=int, default=10, help='Number of log entries to show')
    parser.add_argument('--sync', action='store_true', help='Sync from AWS before showing report')
    
    args = parser.parse_args()
    
    # Initialize state manager
    state_manager = StateManager()
    
    # Sync from AWS if requested
    if args.sync:
        print("🔄 Syncing state from AWS...\n")
        try:
            from cloud_providers.aws import EC2Manager
            from dotenv import load_dotenv
            load_dotenv()
            
            ec2_manager = EC2Manager(region=os.getenv('AWS_REGION', 'us-east-1'))
            state_manager.sync_from_aws(ec2_manager)
            print()
        except Exception as e:
            print(f"⚠️  Could not sync from AWS: {e}\n")
    
    # Show statistics only
    if args.stats:
        stats = state_manager.get_statistics()
        print("📊 Agent Statistics:")
        print(f"  Total Goals Executed: {stats.get('total_goals_executed', 0)}")
        print(f"  Total Resources Created: {stats.get('total_resources_created', 0)}")
        print(f"  Total Resources Deleted: {stats.get('total_resources_deleted', 0)}")
        return
    
    # Show audit log only
    if args.log:
        print(f"📝 Audit Log (last {args.log_limit} actions):\n")
        entries = state_manager.get_audit_log(limit=args.log_limit)
        
        if not entries:
            print("  (No actions logged yet)")
            return
        
        for entry in entries:
            timestamp = entry.get("timestamp", "Unknown")
            action_type = entry.get("action_type", "unknown")
            success = "✅" if entry.get("success") else "❌"
            
            print(f"{success} [{timestamp}] {action_type}")
            
            # Show details
            details = entry.get("details", {})
            if details:
                for key, value in details.items():
                    if isinstance(value, (str, int, float)):
                        print(f"      {key}: {value}")
                    elif isinstance(value, list) and len(value) <= 3:
                        print(f"      {key}: {value}")
            
            if entry.get("error"):
                print(f"      ❌ Error: {entry['error']}")
            print()
        
        return
    
    # Show full report (default)
    report = state_manager.generate_report()
    print(report)
    
    print("\n💡 Tip: Use --log to see detailed audit log, --sync to refresh from AWS")

if __name__ == "__main__":
    main()
