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
3. Agent uses GPT-4 + MCP to accomplish the goal
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

Example Goals:
- "List all EC2 instances"
- "Create 1 t3.micro instance for web server"
- "Ensure I have exactly 2 running instances"
- "Delete all stopped instances"

Configuration Required:
- .env file with AWS credentials (see .env.example)
- OpenAI API key for GPT-4
"""

from agent.core import run_agent_sync

if __name__ == "__main__":
    # Print banner and helpful information
    print("=" * 60)
    print("AI Agent Cloud Infrastructure - MCP Demo")
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
    # The agent will use GPT-4 to reason about how to achieve this goal
    # It will then call MCP tools (via AWS server) to take actions
    
   #  goal = """
   #  Create a production VPC infrastructure:
    
   #  1. Create a VPC with CIDR 10.0.0.0/16 named 'test2-vpc'
   #  2. Create 2 public subnets:
   #     - Public subnet 1: 10.0.1.0/24 in us-east-1a , name it 'test2-public-subnet-1'
   #     - Public subnet 2: 10.0.2.0/24 in us-east-1b, name it 'test2-public-subnet-2'
   #  3. Create 2 private subnets:
   #     - Private subnet 1: 10.0.10.0/24 in us-east-1a , name it 'test2-private-subnet-1'
   #     - Private subnet 2: 10.0.11.0/24 in us-east-1b , name it 'test2-private-subnet-2'
   #  4. Create an Internet Gateway and attach it to the VPC , name it 'test2-igw'
   #  5. Create a NAT Gateway in the first public subnet , name it 'test2-nat-gateway'
   #  6. Create route tables:
   #     - Public route table with route to Internet Gateway (0.0.0.0/0 -> IGW)
   #     - Private route table with route to NAT Gateway (0.0.0.0/0 -> NAT)
   #  7. Associate route tables with appropriate subnets
   #  """
    goal = """
     Delete VPC called 'test2-vpc' and all associated resources (subnets, gateways, route tables)."""
    # You can also test other goals:
   
    
    print("🚀 Starting AI Agent with MCP...\n")
    print(f"📝 Goal: {goal}\n")
    
    # ========================================
    # RUN THE AGENT
    # ========================================
    # This calls agent/core.py → run_agent_sync()
    # Which internally:
    # 1. Loads environment variables (.env)
    # 2. Spawns MCP servers (AWS, Azure, GCP)
    # 3. Discovers tools from servers
    # 4. Runs GPT-4 in a loop to accomplish goal
    # 5. Logs all actions to state/
    run_agent_sync(goal)
    
    print("\n✨ Agent execution completed!")
    print("\n💡 Next steps:")
    print("   - View state: python view_state.py")
    print("   - View logs: python view_state.py --log")
    print("   - Sync AWS: python sync_aws_state.py")

