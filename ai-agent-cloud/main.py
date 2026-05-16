"""
Main Entry Point - AI Agent Cloud Infrastructure Management
===========================================================

THIS IS WHERE EVERYTHING STARTS!

Purpose:
- Entry point for running the AI agent
- Demonstrates autonomous cloud infrastructure management
- Uses Model Context Protocol (MCP) to interact with AWS

How It Works:
1. Define a goal in natural language (e.g., "Create 1 VM")
2. Call run_agent_sync(goal) from agent/core.py
3. Agent uses the configured LLM + MCP to accomplish the goal
4. State is tracked in state/ directory

Usage:
    python main.py

Flow:
    main.py → agent/core.py → agent/mcp_client.py → mcp_servers/aws_server.py → cloud_providers/aws/ec2.py → boto3 → AWS API

Files Created During Execution:
- state/state.json - Current infrastructure snapshot
- state/audit_log.jsonl - Complete history of agent actions

After Running:
- View state: python view_state.py
- View logs: python view_state.py --log
- Sync AWS: python sync_aws_state.py

Configuration Required:
- .env file with AWS credentials (see .env.example)
- LLM API key (OpenAI or Anthropic, see .env.example)
"""

from agent.core import run_agent_sync

if __name__ == "__main__":
    # Print banner and helpful information
    print("=" * 60)
    print("AI Agent Cloud Infrastructure - MCP")
    print("=" * 60)
    print()
    print("💡 Tip: Before first run, capture baseline state:")
    print("   python sync_aws_state.py")
    print()
    print("💡 After running, view state and statistics:")
    print("   python view_state.py")
    print()
    print("=" * 60)
    print()
    
    # ========================================
    # DEFINE YOUR GOAL HERE
    # ========================================
    # This is what you want the AI agent to accomplish
    # It will then call MCP tools (via AWS server) to take actions
 
    goal = """
    Investigate instance named TestWebServer. Check alarms, metrics, and logs. Give me its current state
   """
    # You can also test other goals:
    # detect idle cost leaks in my AWS account and recommend actions to fix them.
    # Analyze and apply rightsizing action(if needed) for instance named SrcInstance and recommend savings.
    # look if there is any policy violation in the security group named Testing1 and fix them by yourself
    #summarize to me the xray map in the last 20 min and check if there is any anomalies for the instance named TestWebServer """

    #summarize to me the diskio section of the metrics last hour for the instance named TestWebServer.
    # Investigate instance named TestWebServer. Check alarms, metrics, and logs. Give me the current state , Identify root cause (if there is a problem) and propose immediate mitigation.
    
    print("🚀 Starting AI Agent with MCP...\n")
    print(f"📝 Goal: {goal}\n")

    result = run_agent_sync(goal)

    print("\n✨ Agent execution completed!")

    if isinstance(result, dict):
        print("\n📏 Evaluation Metrics:")
        print(f"   Trajectory Length (TL)  : {result.get('trajectory_length', 'N/A')} tool call(s)")
        e2e = result.get("task_completion_time_seconds")
        if e2e is not None:
            print(f"   End-to-End Time (E2E)   : {e2e:.1f}s  ({e2e / 60:.1f} min)")
        success = result.get("success", False)
        print(f"   Task Success (TSR)      : {'SUCCESS' if success else 'FAILED'} ({result.get('reason', 'N/A')})")

    print("\n💡 Next steps:")
    print("   - View state: python view_state.py")
    print("   - View logs: python view_state.py --log")
    print("   - Sync AWS: python sync_aws_state.py")

