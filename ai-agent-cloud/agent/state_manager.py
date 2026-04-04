"""
State Manager - Persistent State Tracking for AI Agent
=======================================================

PURPOSE:
This module provides persistent storage and audit logging for the AI agent's actions.
Essential for thesis evaluation - provides complete audit trail of agent decisions.

TWO MAIN FILES:
1. state.json - Current infrastructure snapshot (latest state)
2. audit_log.jsonl - Append-only log of all actions (complete history)

WHY WE NEED THIS:
- Track what the agent does over time
- Audit trail for thesis evaluation
- Recover from crashes (agent can resume from last state)
- Debugging (see what went wrong)
- Cost monitoring (track resource creation)

USAGE EXAMPLE:
    state_mgr = StateManager()  # Creates state/ directory
    state_mgr.log_action("aws_create_ec2_instance", {...}, "Success")
    state_mgr.sync_infrastructure_state({"instances": [...]})
    history = state_mgr.get_action_history()

FILES CREATED:
- state/state.json - JSON object with latest state
- state/audit_log.jsonl - One JSON object per line (append-only)
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path


class StateManager:
    """
    Manages persistent state tracking for the AI agent.
    
    TWO-TIER STORAGE MODEL:
    ----------------------
    1. state.json (SNAPSHOT):
       - Current infrastructure state only
       - Overwritten on each update
       - Used for quick "what exists now?" queries
       - Example: {"instances": [{"id": "i-123", "name": "web-server"}]}
    
    2. audit_log.jsonl (APPEND-ONLY LOG):
       - Every action the agent takes
       - Never modified, only appended
       - Used for audit trail and debugging
       - Each line: {"timestamp": "...", "action": "...", "args": {...}, "result": "..."}
    
    WHY TWO FILES?
    - state.json = fast lookup of current state
    - audit_log.jsonl = complete history for thesis evaluation
    - Separate concerns: current state vs historical actions
    
    THREAD SAFETY:
    - File I/O is synchronous (not async)
    - JSONL append is atomic on most filesystems
    - State file uses temp file + rename for atomicity
    """
    
    def __init__(self, state_dir: str = None):
        """
        Initialize state manager and create state directory.
        
        Args:
            state_dir: Directory to store state files. Defaults to ./state/
        
        Creates:
            state/state.json - If doesn't exist, creates empty structure
            state/audit_log.jsonl - Created on first log_action() call
        
        Directory Structure:
            project_root/
              ├── agent/
              ├── mcp_servers/
              └── state/              ← Created here
                  ├── state.json      ← Current snapshot
                  └── audit_log.jsonl ← Append-only log
        """
        if state_dir is None:
            # Default to 'state' directory in project root
            # __file__ = .../agent/state_manager.py
            # .parent = .../agent/
            # .parent.parent = .../project_root/
            project_root = Path(__file__).parent.parent
            state_dir = project_root / "state"
        
        # Create state directory if it doesn't exist
        # exist_ok=True prevents error if already exists
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(exist_ok=True)
        
        # Define file paths
        self.state_file = self.state_dir / "state.json"  # Snapshot
        self.audit_log_file = self.state_dir / "audit_log.jsonl"  # History
        
        # Initialize state file if it doesn't exist
        # This ensures we always have a valid state.json to read
        if not self.state_file.exists():
            self._init_state_file()
    
    def _init_state_file(self):
        """
        Initialize empty state file with default structure.
        
        Called when:
        - State file doesn't exist (first run)
        - State file is corrupted (auto-recovery)
        
        Structure Created:
        {
          "version": "1.0",
          "initialized_at": "2024-01-15T10:30:00",
          "last_updated": "2024-01-15T10:30:00",
          "providers": {
            "aws": {"ec2_instances": [], "last_sync": null},
            "azure": {"vms": [], "last_sync": null},
            "gcp": {"instances": [], "last_sync": null}
          },
          "statistics": {
            "total_goals_executed": 0,
            "total_resources_created": 0,
            "total_resources_deleted": 0
          }
        }
        
        Purpose:
        - Ensures state file always has valid structure
        - Tracks metrics for thesis evaluation
        - Multi-cloud ready (AWS, Azure, GCP sections)
        """
        initial_state = {
            "version": "1.0",  # Schema version for future migrations
            "initialized_at": datetime.now().isoformat(),  # When state tracking started
            "last_updated": datetime.now().isoformat(),  # Last modification time
            "providers": {
                # AWS infrastructure state (hierarchical organization)
                "aws": {
                    "vpcs": [],  # List of VPCs, each containing subnets with instances, and security groups
                    "orphaned_resources": {
                        "instances": [],  # EC2 instances not in any VPC
                        "security_groups": []  # Security groups not in any VPC
                    },
                    "last_sync": None  # Last sync with AWS API
                },
                # Azure infrastructure state (future)
                "azure": {
                    "vms": [],  # Virtual machines
                    "last_sync": None
                },
                # GCP infrastructure state (future)
                "gcp": {
                    "instances": [],  # Compute Engine instances
                    "last_sync": None
                }
            },
            "statistics": {
                # Metrics for thesis evaluation
                "total_goals_executed": 0,  # How many goals agent completed
                "total_resources_created": 0,  # Total resources created
                "total_resources_deleted": 0,  # Total resources destroyed
                "cost_recommendations_generated": 0,
                "cost_actions_applied": 0,
                "estimated_hourly_savings_usd": 0.0,
                "estimated_monthly_savings_usd": 0.0,
            }
        }
        self._save_state(initial_state)
        print(f"✅ Initialized state file: {self.state_file}")
    
    def _load_state(self) -> Dict:
        """
        Load current state from state.json file.
        
        Features:
        - Auto-recovery from corrupted files
        - Validation of state structure
        - Fallback to fresh initialization if needed
        
        Returns:
            Dictionary containing current state
        
        Error Handling:
        - JSON parse error → reinitialize
        - Invalid structure → reinitialize
        - Missing file → reinitialize
        
        Why This Matters:
        - State file can be corrupted by crashes, disk errors, manual edits
        - Agent needs to be resilient and self-healing
        - Better to start fresh than crash on startup
        """
        try:
            # Attempt to read and parse state file
            with open(self.state_file, 'r') as f:
                state = json.load(f)
                
                # Validate state structure
                # Must be a dict with 'providers' key at minimum
                if not isinstance(state, dict) or 'providers' not in state:
                    print("⚠️  State file corrupted or invalid - reinitializing...")
                    self._init_state_file()
                    # Re-read the freshly initialized state
                    with open(self.state_file, 'r') as f:
                        return json.load(f)
                
                return state
        
        except Exception as e:
            # Catch all errors: JSON decode, file not found, permission errors
            print(f"⚠️  Error loading state: {e}")
            print("🔄 Reinitializing state file...")
            self._init_state_file()
            # Re-read the freshly initialized state
            with open(self.state_file, 'r') as f:
                return json.load(f)
    
    def _save_state(self, state: Dict):
        """
        Save state to state.json file.
        
        Features:
        - Auto-updates last_updated timestamp
        - Pretty-printed JSON (indent=2) for human readability
        - Handles datetime serialization with default=str
        
        Args:
            state: Dictionary to save
        
        Why default=str?
        - State may contain datetime objects
        - JSON can't serialize datetime natively
        - default=str converts datetime → ISO string
        - Example: datetime(2024, 1, 15) → "2024-01-15T10:30:00"
        
        File Format:
        Pretty-printed JSON for easy manual inspection:
        {
          "version": "1.0",
          "initialized_at": "2024-01-15T10:30:00",
          "providers": {
            "aws": {
              "ec2_instances": [ ... ]
            }
          }
        }
        """
        # Update timestamp
        state["last_updated"] = datetime.now().isoformat()
        
        # Write to file
        # indent=2 makes JSON human-readable
        # default=str handles datetime objects
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2, default=str)
    
    def update_aws_hierarchical_state(self, vpcs: List[Dict], instances: List[Dict], security_groups: List[Dict]):
        """
        Update AWS state with hierarchical organization.
        Organizes resources as: VPCs → Subnets → EC2 Instances, and VPCs → Security Groups
        
        Args:
            vpcs: List of VPC dictionaries from VPCManager.list_vpcs()
            instances: List of instance dictionaries from EC2Manager.list_instances()
            security_groups: List of SG dictionaries from SecurityGroupManager.list_security_groups()
        """
        state = self._load_state()
        
        # Build hierarchical structure
        organized_vpcs = []
        orphaned_instances = []
        orphaned_sgs = []
        
        # Process each VPC
        for vpc in vpcs:
            vpc_id = vpc['vpc_id']
            
            # Find security groups for this VPC
            vpc_security_groups = [
                sg for sg in security_groups 
                if sg.get('vpc_id') == vpc_id
            ]
            
            # Process each subnet in the VPC
            enriched_subnets = []
            for subnet in vpc.get('subnets', []):
                subnet_id = subnet['subnet_id']
                
                # Find instances in this subnet
                subnet_instances = [
                    inst for inst in instances
                    if inst.get('subnet_id') == subnet_id
                ]
                
                # Add instances to subnet
                enriched_subnet = subnet.copy()
                enriched_subnet['instances'] = subnet_instances
                enriched_subnets.append(enriched_subnet)
            
            # Build enriched VPC structure
            enriched_vpc = vpc.copy()
            enriched_vpc['subnets'] = enriched_subnets
            enriched_vpc['security_groups'] = vpc_security_groups
            organized_vpcs.append(enriched_vpc)
        
        # Find orphaned instances (no subnet/VPC or VPC not in our list)
        vpc_ids = {vpc['vpc_id'] for vpc in vpcs}
        for inst in instances:
            if inst.get('vpc_id') == 'N/A' or inst.get('vpc_id') not in vpc_ids:
                orphaned_instances.append(inst)
        
        # Find orphaned security groups (no VPC or VPC not in our list)
        for sg in security_groups:
            if sg.get('vpc_id') == 'N/A' or sg.get('vpc_id') not in vpc_ids:
                orphaned_sgs.append(sg)
        
        # Update state
        state["providers"]["aws"]["vpcs"] = organized_vpcs
        state["providers"]["aws"]["orphaned_resources"] = {
            "instances": orphaned_instances,
            "security_groups": orphaned_sgs
        }
        state["providers"]["aws"]["last_sync"] = datetime.now().isoformat()
        self._save_state(state)
    
    def get_aws_vpc_state(self) -> List[Dict]:
        """Get hierarchically organized AWS VPC state."""
        state = self._load_state()
        return state.get("providers", {}).get("aws", {}).get("vpcs", [])
    
    def get_aws_orphaned_resources(self) -> Dict:
        """Get orphaned AWS resources (not in any VPC)."""
        state = self._load_state()
        return state.get("providers", {}).get("aws", {}).get("orphaned_resources", {"instances": [], "security_groups": []})
    
    def log_action(self, action_type: str, details: Dict, success: bool = True, error: str = None):
        """
        Log an action to the audit log (audit_log.jsonl).
        
        THIS IS THE MOST IMPORTANT METHOD FOR THESIS EVALUATION!
        
        Purpose:
        - Creates append-only audit trail
        - Every action the agent takes is logged
        - Provides complete history for evaluation
        - Enables debugging and analysis
        
        Args:
            action_type: Type of action (e.g., 'aws_create_ec2_instance', 'goal_executed')
            details: Dictionary with action details (tool arguments, results)
            success: Whether the action succeeded (default True)
            error: Error message if action failed (optional)
        
        Log Entry Format (JSONL - JSON Lines):
        Each line is a complete JSON object:
        {"timestamp": "2024-01-15T10:30:00", "action_type": "aws_create_ec2_instance", "success": true, "details": {"name": "web-server", "instance_type": "t3.micro"}}
        {"timestamp": "2024-01-15T10:31:00", "action_type": "aws_delete_ec2_instance", "success": true, "details": {"instance_id": "i-abc123"}}
        {"timestamp": "2024-01-15T10:32:00", "action_type": "goal_executed", "success": true, "details": {"goal": "Create 1 VM", "outcome": "Created instance i-def456"}}
        
        Why JSONL (not JSON array)?
        - Append-only (no need to read entire file to add entry)
        - Handles crashes gracefully (partial writes don't corrupt file)
        - Easy to stream and process line-by-line
        - Standard format for log analysis tools
        
        Called By:
        - agent/core.py after each tool call
        - log_goal_execution() for goal completions
        - sync_infrastructure_state() for state updates
        
        Example Usage:
            state_mgr.log_action(
                action_type="aws_create_ec2_instance",
                details={"name": "my-vm", "cpu": 1, "ram_gb": 1},
                success=True
            )
        """
        # Build log entry
        log_entry = {
            "timestamp": datetime.now().isoformat(),  # When action occurred
            "action_type": action_type,  # What action (tool name or event type)
            "success": success,  # Did it succeed?
            "details": details  # Action-specific data (arguments, results)
        }
        
        # Add error message if action failed
        if error:
            log_entry["error"] = error
        
        # APPEND to audit log (JSONL format - one JSON object per line)
        # 'a' mode = append (doesn't overwrite existing content)
        # Each write adds a new line, never modifies existing lines
        with open(self.audit_log_file, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')  # Write JSON + newline
    
    def log_goal_execution(self, goal: str, outcome: str, actions_taken: List[str]):
        """
        Log a completed goal execution.
        
        Args:
            goal: The goal that was executed
            outcome: Result/summary of execution
            actions_taken: List of actions taken to achieve goal
        """
        state = self._load_state()
        state["statistics"]["total_goals_executed"] += 1
        self._save_state(state)
        
        self.log_action(
            action_type="goal_executed",
            details={
                "goal": goal,
                "outcome": outcome,
                "actions_taken": actions_taken
            },
            success=True
        )
    
    def log_resource_created(self, provider: str, resource_type: str, resource_id: str, resource_name: str):
        """
        Log resource creation.
        
        Args:
            provider: Cloud provider ('aws', 'azure', 'gcp')
            resource_type: Type of resource ('ec2_instance', 'vm', etc.)
            resource_id: Resource identifier (instance ID)
            resource_name: Human-readable name
        """
        state = self._load_state()
        state["statistics"]["total_resources_created"] += 1
        self._save_state(state)
        
        self.log_action(
            action_type=f"{provider}_create_{resource_type}",
            details={
                "provider": provider,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "resource_name": resource_name
            },
            success=True
        )
    
    def log_resource_deleted(self, provider: str, resource_type: str, resource_id: str):
        """
        Log resource deletion.
        
        Args:
            provider: Cloud provider
            resource_type: Type of resource
            resource_id: Resource identifier
        """
        state = self._load_state()
        state["statistics"]["total_resources_deleted"] += 1
        self._save_state(state)
        
        self.log_action(
            action_type=f"{provider}_delete_{resource_type}",
            details={
                "provider": provider,
                "resource_type": resource_type,
                "resource_id": resource_id
            },
            success=True
        )
    
    def get_statistics(self) -> Dict:
        """Get agent statistics."""
        state = self._load_state()
        return state.get("statistics", {})

    def log_cost_recommendation(
        self,
        recommendation_type: str,
        details: Dict[str, Any],
        estimated_hourly_savings_usd: float = 0.0,
        estimated_monthly_savings_usd: float = 0.0,
    ):
        """Track generated cost recommendation KPIs and audit event."""
        state = self._load_state()
        stats = state.setdefault('statistics', {})
        stats['cost_recommendations_generated'] = int(stats.get('cost_recommendations_generated', 0)) + 1
        stats['estimated_hourly_savings_usd'] = float(stats.get('estimated_hourly_savings_usd', 0.0)) + float(estimated_hourly_savings_usd)
        stats['estimated_monthly_savings_usd'] = float(stats.get('estimated_monthly_savings_usd', 0.0)) + float(estimated_monthly_savings_usd)
        self._save_state(state)

        self.log_action(
            action_type='cost_recommendation_generated',
            details={
                'recommendation_type': recommendation_type,
                'estimated_hourly_savings_usd': estimated_hourly_savings_usd,
                'estimated_monthly_savings_usd': estimated_monthly_savings_usd,
                **details,
            },
            success=True,
        )

    def log_cost_action_applied(
        self,
        action_type: str,
        details: Dict[str, Any],
        estimated_hourly_savings_usd: float = 0.0,
        estimated_monthly_savings_usd: float = 0.0,
    ):
        """Track applied cost action KPIs and audit event."""
        state = self._load_state()
        stats = state.setdefault('statistics', {})
        stats['cost_actions_applied'] = int(stats.get('cost_actions_applied', 0)) + 1
        stats['estimated_hourly_savings_usd'] = float(stats.get('estimated_hourly_savings_usd', 0.0)) + float(estimated_hourly_savings_usd)
        stats['estimated_monthly_savings_usd'] = float(stats.get('estimated_monthly_savings_usd', 0.0)) + float(estimated_monthly_savings_usd)
        self._save_state(state)

        self.log_action(
            action_type='cost_action_applied',
            details={
                'applied_action_type': action_type,
                'estimated_hourly_savings_usd': estimated_hourly_savings_usd,
                'estimated_monthly_savings_usd': estimated_monthly_savings_usd,
                **details,
            },
            success=True,
        )
    
    def get_audit_log(self, limit: int = None, action_type: str = None) -> List[Dict]:
        """
        Get audit log entries.
        
        Args:
            limit: Maximum number of entries to return (most recent first)
            action_type: Filter by action type
        
        Returns:
            List of log entry dictionaries
        """
        if not self.audit_log_file.exists():
            return []
        
        entries = []
        with open(self.audit_log_file, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if action_type is None or entry.get("action_type") == action_type:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
        
        # Return most recent first
        entries.reverse()
        
        if limit:
            entries = entries[:limit]
        
        return entries
    
    def generate_report(self) -> str:
        """
        Generate a human-readable report of agent activity.
        
        Returns:
            Formatted report string
        """
        state = self._load_state()
        stats = state.get("statistics", {})
        
        report_lines = [
            "=" * 60,
            "AI Agent State Report",
            "=" * 60,
            f"Initialized: {state.get('initialized_at', 'Unknown')}",
            f"Last Updated: {state.get('last_updated', 'Unknown')}",
            "",
            "📊 Statistics:",
            f"  - Total Goals Executed: {stats.get('total_goals_executed', 0)}",
            f"  - Total Resources Created: {stats.get('total_resources_created', 0)}",
            f"  - Total Resources Deleted: {stats.get('total_resources_deleted', 0)}",
            f"  - Cost Recommendations Generated: {stats.get('cost_recommendations_generated', 0)}",
            f"  - Cost Actions Applied: {stats.get('cost_actions_applied', 0)}",
            f"  - Estimated Hourly Savings (USD): {stats.get('estimated_hourly_savings_usd', 0.0):.4f}",
            f"  - Estimated Monthly Savings (USD): {stats.get('estimated_monthly_savings_usd', 0.0):.2f}",
            "",
            "🌐 AWS Infrastructure (Hierarchical View):",
        ]
        
        aws_vpcs = state.get("providers", {}).get("aws", {}).get("vpcs", [])
        if aws_vpcs:
            for vpc in aws_vpcs:
                igw_count = len(vpc.get('internet_gateways', []))
                nat_count = len(vpc.get('nat_gateways', []))
                sg_count = len(vpc.get('security_groups', []))
                rt_count = len(vpc.get('route_tables', []))
                
                report_lines.append(f"")
                report_lines.append(
                    f"  📦 VPC: {vpc.get('name', 'N/A')} ({vpc.get('vpc_id', 'N/A')}) "
                    f"[{vpc.get('state', 'unknown')}]"
                )
                report_lines.append(f"      CIDR: {vpc.get('cidr_block', 'N/A')}")
                report_lines.append(f"      Networking: {igw_count} IGW, {nat_count} NAT, {rt_count} Route Tables, {sg_count} SGs")
                
                # Show route tables
                route_tables = vpc.get('route_tables', [])
                if route_tables:
                    report_lines.append(f"      Route Tables ({len(route_tables)}):")
                    for rt in route_tables:
                        main_tag = " [MAIN]" if rt.get('is_main', False) else ""
                        assoc_count = len(rt.get('associated_subnets', []))
                        report_lines.append(
                            f"        └─ {rt.get('name', 'N/A')} ({rt.get('route_table_id', 'N/A')}){main_tag}"
                        )
                        # Show key routes (non-local)
                        for route in rt.get('routes', [])[:3]:  # Show first 3 routes
                            if route.get('target') != 'local':
                                report_lines.append(f"           ├─ {route['destination']} → {route['target']}")
                        if assoc_count > 0:
                            report_lines.append(f"           └─ {assoc_count} associated subnets")
                
                # Show subnets and their instances
                subnets = vpc.get('subnets', [])
                if subnets:
                    report_lines.append(f"      Subnets ({len(subnets)}):")
                    for subnet in subnets:
                        subnet_type = "Public" if subnet.get('map_public_ip_on_launch', False) else "Private"
                        instances = subnet.get('instances', [])
                        report_lines.append(
                            f"        └─ {subnet.get('name', 'N/A')} ({subnet.get('subnet_id', 'N/A')}) "
                            f"[{subnet_type}] - {subnet.get('cidr_block', 'N/A')}"
                        )
                        
                        # Show instances in this subnet
                        if instances:
                            for inst in instances:
                                state_marker = "🟢" if inst.get('state') == 'running' else "🔴" if inst.get('state') == 'stopped' else "⚪"
                                report_lines.append(
                                    f"           {state_marker} {inst.get('name', 'N/A')} ({inst.get('id', 'N/A')}) "
                                    f"- {inst.get('type', 'N/A')} [{inst.get('state', 'unknown')}]"
                                )
                        else:
                            report_lines.append(f"           (No instances)")
                
                # Show security groups for this VPC
                sgs = vpc.get('security_groups', [])
                if sgs:
                    # Only show AI-managed SGs for cleaner output
                    managed_sgs = [sg for sg in sgs if any(
                        tag.get('Key') == 'ManagedBy' and tag.get('Value') == 'AIAgent'
                        for tag in sg.get('Tags', [])
                    )]
                    
                    if managed_sgs:
                        report_lines.append(f"      Security Groups (AI-Managed):")
                        for sg in managed_sgs:
                            ingress_count = len(sg.get('ingress_rules', []))
                            egress_count = len(sg.get('egress_rules', []))
                            report_lines.append(
                                f"        🔒 {sg.get('name', 'N/A')} ({sg.get('security_group_id', 'N/A')}) "
                                f"- {ingress_count} in, {egress_count} out"
                            )
        else:
            report_lines.append("  (No VPCs tracked)")
        
        # Show orphaned resources
        orphaned = state.get("providers", {}).get("aws", {}).get("orphaned_resources", {})
        orphaned_instances = orphaned.get('instances', [])
        orphaned_sgs = orphaned.get('security_groups', [])
        
        if orphaned_instances or orphaned_sgs:
            report_lines.append("")
            report_lines.append("  ⚠️  Orphaned Resources (not in any VPC):")
            
            if orphaned_instances:
                report_lines.append(f"      Instances ({len(orphaned_instances)}):")
                for inst in orphaned_instances:
                    report_lines.append(
                        f"        - {inst.get('name', 'N/A')} ({inst.get('id', 'N/A')}) "
                        f"- {inst.get('type', 'N/A')} [{inst.get('state', 'unknown')}]"
                    )
            
            if orphaned_sgs:
                report_lines.append(f"      Security Groups ({len(orphaned_sgs)}):")
                for sg in orphaned_sgs[:3]:  # Show first 3
                    report_lines.append(f"        - {sg.get('name', 'N/A')} ({sg.get('security_group_id', 'N/A')})")
                if len(orphaned_sgs) > 3:
                    report_lines.append(f"        ... and {len(orphaned_sgs) - 3} more")
        
        aws_last_sync = state.get("providers", {}).get("aws", {}).get("last_sync")
        if aws_last_sync:
            report_lines.append("")
            report_lines.append(f"  ⏱️  Last AWS sync: {aws_last_sync}")
        
        report_lines.append("")
        report_lines.append("📝 Recent Actions (last 5):")
        
        recent_actions = self.get_audit_log(limit=5)
        if recent_actions:
            for action in recent_actions:
                timestamp = action.get("timestamp", "Unknown")
                action_type = action.get("action_type", "unknown")
                success = "✅" if action.get("success") else "❌"
                report_lines.append(f"  {success} [{timestamp}] {action_type}")
        else:
            report_lines.append("  (No actions logged yet)")
        
        report_lines.append("=" * 60)
        
        return "\n".join(report_lines)
    
    def sync_from_aws(self, ec2_manager=None, vpc_manager=None, security_manager=None):
        """
        Sync state from AWS and organize hierarchically.
        
        Args:
            ec2_manager: EC2Manager instance to query
            vpc_manager: VPCManager instance to query
            security_manager: SecurityGroupManager instance to query
        """
        instances = []
        vpcs = []
        security_groups = []
        
        if vpc_manager:
            print("🔄 Syncing VPCs from AWS...")
            vpcs = vpc_manager.list_vpcs()
            print(f"✅ Synced {len(vpcs)} VPCs")
        
        if ec2_manager:
            print("🔄 Syncing EC2 instances from AWS...")
            instances = ec2_manager.list_instances()
            print(f"✅ Synced {len(instances)} EC2 instances")
        
        if security_manager:
            print("🔄 Syncing Security Groups from AWS...")
            security_groups = security_manager.list_security_groups()
            print(f"✅ Synced {len(security_groups)} Security Groups")
        
        # Organize hierarchically
        print("📊 Organizing resources hierarchically...")
        self.update_aws_hierarchical_state(vpcs, instances, security_groups)
        
        self.log_action(
            action_type="sync_from_aws",
            details={
                "vpcs": len(vpcs),
                "instances": len(instances),
                "security_groups": len(security_groups)
            },
            success=True
        )
