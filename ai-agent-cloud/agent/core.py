"""
AI Agent Core with MCP Integration
Main agent loop that uses MCP servers for cloud infrastructure management.

This module orchestrates the entire agent workflow:
1. Load environment variables (API keys, AWS credentials)
2. Connect to MCP servers (spawn aws_server.py as subprocess)
3. Discover available tools from MCP servers
4. Run agent loop: GPT-4 plans → calls tools → evaluates results
5. Log actions to state files for audit trail
"""

import asyncio  # For async MCP communication
import json
import os
from dotenv import load_dotenv  # Load .env file with credentials
from openai import OpenAI  # GPT-4 for planning and reasoning
from .mcp_client import MCPClientManager  # MCP client (connects to servers)
from .state_manager import StateManager  # State tracking and audit logs

# Load environment variables from .env file
# This loads: OPENAI_API_KEY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
load_dotenv()


async def run_agent(goal: str, mcp_servers: list = None):
    """
    Run the AI agent with specified goal using MCP servers.
    
    This is the main async function that:
    1. Initializes OpenAI, MCP client, and state manager
    2. Spawns MCP servers as subprocesses (e.g., aws_server.py)
    3. Discovers tools from servers via MCP protocol
    4. Runs iterative loop: GPT-4 thinks → calls tools → evaluates
    5. Logs all actions for audit trail
    
    Args:
        goal: Natural language description of what to accomplish
              Example: "Create 2 EC2 instances for web servers"
        mcp_servers: List of MCP server configurations. If None, uses AWS by default.
                     Format: [{'name': 'aws', 'command': 'python', 'args': ['path/to/server.py']}]
    
    Example:
        await run_agent("Create 2 EC2 instances for web servers")
        
        # Or with custom server config:
        await run_agent(
            "List all cloud resources",
            mcp_servers=[
                {'name': 'aws', 'command': 'python', 'args': ['mcp_servers/aws_server.py']},
                {'name': 'azure', 'command': 'python', 'args': ['mcp_servers/azure_server.py']}
            ]
        )
    """
    # ═══════════════════════════════════════════════════════════════
    # STEP 1: Initialize Components
    # ═══════════════════════════════════════════════════════════════
    
    # Initialize OpenAI client for GPT-4 reasoning
    # GPT-4 will plan actions, decide which tools to call, and evaluate results
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    
    # Initialize MCP client manager
    # This will spawn MCP servers as subprocesses and communicate via stdio
    mcp_client = MCPClientManager()
    
    # Initialize state manager for audit logging
    # Tracks all actions to state.json and audit_log.jsonl
    state_manager = StateManager()
    
    # ═══════════════════════════════════════════════════════════════
    # STEP 2: Configure MCP Servers
    # ═══════════════════════════════════════════════════════════════
    
    # Default to AWS server if not specified
    if mcp_servers is None:
        # Prepare AWS environment variables to pass to subprocess
        # The MCP server will run as a separate process, so it needs AWS credentials
        aws_env = {
            'AWS_REGION': os.getenv('AWS_REGION', 'us-east-1'),
            'AWS_ACCESS_KEY_ID': os.getenv('AWS_ACCESS_KEY_ID', ''),
            'AWS_SECRET_ACCESS_KEY': os.getenv('AWS_SECRET_ACCESS_KEY', ''),
        }
        # If using AWS CLI profiles instead of access keys
        if os.getenv('AWS_PROFILE'):
            aws_env['AWS_PROFILE'] = os.getenv('AWS_PROFILE')
        
        # Configure server to spawn: python mcp_servers/aws_server.py
        mcp_servers = [
            {
                'name': 'aws',  # Identifier for this server
                'command': 'python',  # Command to run
                'args': [os.path.join(os.path.dirname(__file__), '..', 'mcp_servers', 'aws_server.py')],
                'env': aws_env  # Environment variables (AWS credentials)
            }
        ]
    
    try:
        # ═══════════════════════════════════════════════════════════════
        # STEP 3: Connect to MCP Servers
        # ═══════════════════════════════════════════════════════════════
        
        # Connect to all configured MCP servers
        # This spawns each server as a subprocess and connects via stdio (MCP protocol)
        print("🔗 Connecting to MCP servers...")
        for server_config in mcp_servers:
            await mcp_client.connect_to_server(
                server_config['name'],  # e.g., 'aws'
                server_config['command'],  # e.g., 'python'
                server_config.get('args', []),  # e.g., ['mcp_servers/aws_server.py']
                server_config.get('env')  # e.g., {'AWS_REGION': 'us-east-1', ...}
            )
        
        # ═══════════════════════════════════════════════════════════════
        # STEP 4: Discover Tools from MCP Servers
        # ═══════════════════════════════════════════════════════════════
        
        # Discover tools from all connected servers
        # Sends MCP "list_tools" request to each server
        # Servers respond with tool names, descriptions, and parameter schemas
        print("\n🔍 Discovering tools from MCP servers...")
        await mcp_client.discover_tools()
        
        # Get tools in OpenAI function calling format
        # Converts MCP tool format to OpenAI's expected format
        tools = mcp_client.get_tools_for_openai()
        
        print(f"\n📋 Available tools: {len(tools)}")
        for tool in tools:
            print(f"   - {tool['function']['name']}")
        
        # Initialize conversation
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an autonomous cloud infrastructure agent. "
                    "You manage cloud resources (EC2 instances, VMs) using available tools. "
                    "Think step-by-step and call tools when needed. "
                    "Make decisions independently without asking the user. "
                    "For cost efficiency, prefer t3.micro instances (1 CPU, 1GB RAM) as they are free-tier eligible. "
                    "Only use larger instances if specifically requested by the user. "
                    "When creating instances without specific requirements, use t3.micro to minimize costs. "
                    "Complete tasks immediately and report what you did."
                )
            },
            {"role": "user", "content": goal}
        ]
        
        print(f"\n🎯 Agent Goal: {goal}\n")
        print("=" * 60)
        
        # Main agent loop
        iteration = 0
        max_iterations = 3  # Prevent infinite loops (reduced to avoid retries on transient errors)
        actions_taken = []  # Track actions for this goal
        
        while iteration < max_iterations:
            iteration += 1
            
            # Call OpenAI with available tools
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=tools,
                tool_choice="auto"
            )
            
            msg = response.choices[0].message
            
            # If AI decides to call tools
            if msg.tool_calls:
                messages.append(msg)
                
                # Process all tool calls
                for tool_call in msg.tool_calls:
                    tool_name = tool_call.function.name
                    args = json.loads(tool_call.function.arguments)
                    
                    print(f"\n🔧 Calling Tool: {tool_name}")
                    print(f"   Arguments: {json.dumps(args, indent=6)}")
                    
                    try:
                        # Execute tool via MCP
                        result = await mcp_client.call_tool(tool_name, args)
                        
                        print(f"📊 Tool Result:")
                        # Pretty print JSON results
                        try:
                            result_obj = json.loads(result)
                            print(json.dumps(result_obj, indent=3))
                            
                            # Log action to state manager
                            if result_obj.get("success"):
                                actions_taken.append(f"{tool_name}({json.dumps(args)})")
                                
                                # Log specific resource operations
                                if tool_name == "aws_create_ec2_instance" and "instance" in result_obj:
                                    inst = result_obj["instance"]
                                    state_manager.log_resource_created(
                                        provider="aws",
                                        resource_type="ec2_instance",
                                        resource_id=inst.get("id", "unknown"),
                                        resource_name=inst.get("name", "unknown")
                                    )
                                elif tool_name == "aws_delete_ec2_instance":
                                    state_manager.log_resource_deleted(
                                        provider="aws",
                                        resource_type="ec2_instance",
                                        resource_id=args.get("instance_id", "unknown")
                                    )
                        except:
                            print(f"   {result}")
                        
                        # Add tool result to conversation
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result
                        })
                    
                    except Exception as e:
                        error_result = json.dumps({
                            "success": False,
                            "error": str(e)
                        })
                        
                        print(f"❌ Tool Error: {e}")
                        
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": error_result
                        })
            
            else:
                # Agent has finished - no more tool calls
                print("\n" + "=" * 60)
                print("🧠 Final Agent Response:\n")
                print(msg.content)
                print("\n" + "=" * 60)
                
                # Log goal execution
                state_manager.log_goal_execution(
                    goal=goal,
                    outcome=msg.content if msg.content else "Completed",
                    actions_taken=actions_taken
                )
                
                # Show statistics
                stats = state_manager.get_statistics()
                print(f"\n📊 Session Statistics:")
                print(f"   Total goals executed: {stats.get('total_goals_executed', 0)}")
                print(f"   Total resources created: {stats.get('total_resources_created', 0)}")
                print(f"   Total resources deleted: {stats.get('total_resources_deleted', 0)}")
                
                break
        
        if iteration >= max_iterations:
            print("\n⚠️  Agent reached maximum iterations")
    
    finally:
        # Clean up MCP connections
        await mcp_client.close()


# Synchronous wrapper for convenience
def run_agent_sync(goal: str, mcp_servers: list = None):
    """
    Synchronous wrapper for run_agent.
    
    Args:
        goal: The objective for the agent to accomplish
        mcp_servers: Optional list of MCP server configurations
    """
    asyncio.run(run_agent(goal, mcp_servers))


if __name__ == "__main__":
    # Example usage
    run_agent_sync("List all EC2 instances and create one if there are none")
