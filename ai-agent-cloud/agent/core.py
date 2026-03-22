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
import re
from dotenv import load_dotenv  # Load .env file with credentials
from openai import OpenAI  # GPT-4 for planning and reasoning
from .mcp_client import MCPClientManager  # MCP client (connects to servers)
from .state_manager import StateManager  # State tracking and audit logs
from .policy_engine import PolicyEngine, PolicyViolation  # Policy validation

# Load environment variables from .env file
# This loads: OPENAI_API_KEY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
load_dotenv()


BASE_SYSTEM_PROMPT = (
    "You are an autonomous cloud infrastructure agent. "
    "Use available tools to complete the user's goal directly and safely. "
    "Think step-by-step, call tools when needed, and do not ask the user for operational steps unless blocked.\n\n"
    "GENERAL RULES:\n"
    "- Prefer cost-efficient resources unless explicitly asked otherwise\n"
    "- Validate assumptions by calling tools before concluding\n"
    "- If user asks for an MCP resource URI (aws://...), call read_mcp_resource instead of passing URI to AWS API tools\n"
    "- If user asks to use a named MCP prompt template, call get_mcp_prompt first\n"
    "- Report exactly what you changed/found\n"
)


INSTRUCTION_PACKS = {
    "security_groups": (
        "SECURITY GROUP RULES:\n"
        "- For IP-based access, use 'cidr'\n"
        "- For SG-to-SG access, use 'source_security_group_id' and resolve SG IDs first\n"
        "- Never use a security group name as a CIDR block\n"
    ),
    "vpc_deletion": (
        "VPC DELETION:\n"
        "- Always use aws_delete_vpc with force=true\n"
        "- Pass VPC ID or VPC name directly\n"
        "- Do not manually delete dependent resources first unless explicitly requested\n"
    ),
    "cloudwatch_alarms": (
        "CLOUDWATCH ALARM LOOKUP:\n"
        "- For alarms related to an EC2 instance, do not rely on alarm_name_prefix with instance Name\n"
        "- Prefer aws_list_ec2_alarms with instance_name or instance_id\n"
        "- If unavailable, resolve instance ID first, then filter aws_list_alarms by dimensions where name='InstanceId'\n"
        "- Do not conclude 'no alarms' without this dimension-based check\n"
        "- For CloudWatch metric queries, choose period_seconds by lookback window to avoid oversampling:\n"
        "  * up to 30 minutes -> 60 seconds\n"
        "  * 31 to 180 minutes -> 300 seconds\n"
        "  * more than 180 minutes -> 900 seconds or more\n"
        "- If user asks for last hour metrics, explicitly call aws_get_ec2_metrics with period_seconds=300\n"
    ),
}


def build_system_prompt(goal: str) -> str:
    """Build a compact, goal-aware system prompt to reduce token bloat."""
    goal_text = (goal or "").lower()
    parts = [BASE_SYSTEM_PROMPT]

    if any(k in goal_text for k in ["security group", "ingress", "egress", "cidr", "sg-"]):
        parts.append(INSTRUCTION_PACKS["security_groups"])

    if "vpc" in goal_text and any(k in goal_text for k in ["delete", "remove", "destroy"]):
        parts.append(INSTRUCTION_PACKS["vpc_deletion"])

    if any(k in goal_text for k in ["alarm", "cloudwatch", "dashboard", "log", "metric"]):
        parts.append(INSTRUCTION_PACKS["cloudwatch_alarms"])

    parts.append("Complete tasks immediately and report what you did.")
    return "\n\n".join(parts)


def build_capability_catalog(mcp_client: MCPClientManager) -> str:
    """Build a compact non-tool capability catalog for MCP resources/prompts."""
    catalog = {
        "resources": sorted(list(mcp_client.resources.keys())),
        "resource_templates": sorted(list(mcp_client.resource_templates.keys())),
        "prompts": sorted(list(mcp_client.prompts.keys())),
    }
    return (
        "MCP NON-TOOL CAPABILITY CATALOG (discoverable before planning):\n"
        f"{json.dumps(catalog, indent=2)}\n\n"
        "Note: MCP tools are provided separately via the API tools field. "
        "Use read_mcp_resource/get_mcp_prompt with the catalog above. "
        "For resource templates, instantiate URIs using real values."
    )


def extract_goal_resource_uris(goal: str) -> list:
    """Extract explicit MCP-style resource URIs from goal text."""
    if not goal:
        return []

    # Match scheme URIs such as aws://observability/snapshot
    uri_matches = re.findall(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+", goal)
    cleaned = []
    for uri in uri_matches:
        cleaned_uri = uri.rstrip(".,;)")
        if cleaned_uri not in cleaned:
            cleaned.append(cleaned_uri)
    return cleaned


def find_prompt_mentions(goal: str, prompt_names: list) -> list:
    """Find prompt names explicitly mentioned in user goal."""
    goal_lc = (goal or "").lower()
    return [name for name in prompt_names if name.lower() in goal_lc]


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
    
    # Initialize policy engine for safety validation
    # Validates all actions against security and compliance policies
    policy_engine = PolicyEngine()
    
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
        # STEP 4: Discover Capabilities from MCP Servers
        # ═══════════════════════════════════════════════════════════════
        
        # Discover tools/resources/prompts from all connected servers.
        print("\n🔍 Discovering MCP capabilities from servers...")
        await mcp_client.discover_capabilities()
        
        # Get tools in OpenAI function calling format
        # Converts MCP tool format to OpenAI's expected format
        tools = mcp_client.get_tools_for_openai()
        
        print(f"\n📋 Available tools (including MCP helpers): {len(tools)}")
        for tool in tools:
            print(f"   - {tool['function']['name']}")

        if mcp_client.resources:
            print(f"\n📚 Available resources: {len(mcp_client.resources)}")
            for uri in mcp_client.resources.keys():
                print(f"   - {uri}")

        if mcp_client.prompts:
            print(f"\n🧠 Available prompts: {len(mcp_client.prompts)}")
            for prompt_name in mcp_client.prompts.keys():
                print(f"   - {prompt_name}")

        if mcp_client.resource_templates:
            print(f"\n🧩 Available resource templates: {len(mcp_client.resource_templates)}")
            for uri_template in mcp_client.resource_templates.keys():
                print(f"   - {uri_template}")
        
        # Track actions for this goal
        actions_taken = []

        # Initialize conversation
        messages = [
            {
                "role": "system",
                "content": build_system_prompt(goal)
            },
            {
                "role": "system",
                "content": build_capability_catalog(mcp_client)
            },
            {"role": "user", "content": goal}
        ]

        # Deterministic pre-router: preload explicitly requested MCP resources/prompts
        # before the first model turn so the model starts with grounded context.
        preloaded_items = []

        explicit_uris = extract_goal_resource_uris(goal)
        for uri in explicit_uris:
            try:
                preloaded_result = await mcp_client.read_resource(uri)
                preloaded_items.append(
                    {
                        "kind": "resource",
                        "id": uri,
                        "result": preloaded_result,
                    }
                )
                actions_taken.append(f"read_mcp_resource({json.dumps({'uri': uri})})")
            except Exception as e:
                preloaded_items.append(
                    {
                        "kind": "resource",
                        "id": uri,
                        "error": str(e),
                    }
                )

        mentioned_prompts = find_prompt_mentions(goal, list(mcp_client.prompts.keys()))
        for prompt_name in mentioned_prompts:
            prompt_def = mcp_client.prompts.get(prompt_name, {})
            required_args = [
                arg.get("name") for arg in prompt_def.get("arguments", []) if arg.get("required")
            ]

            if required_args:
                preloaded_items.append(
                    {
                        "kind": "prompt",
                        "id": prompt_name,
                        "error": f"Skipped auto-render: missing required args {required_args}",
                    }
                )
                continue

            try:
                preloaded_prompt = await mcp_client.get_prompt(prompt_name, {})
                preloaded_items.append(
                    {
                        "kind": "prompt",
                        "id": prompt_name,
                        "result": preloaded_prompt,
                    }
                )
                actions_taken.append(f"get_mcp_prompt({json.dumps({'name': prompt_name, 'arguments': {}})})")
            except Exception as e:
                preloaded_items.append(
                    {
                        "kind": "prompt",
                        "id": prompt_name,
                        "error": str(e),
                    }
                )

        if preloaded_items:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "PRELOADED MCP CONTEXT (auto-fetched before planning):\n"
                        f"{json.dumps(preloaded_items, indent=2)}"
                    ),
                }
            )
        
        print(f"\n🎯 Agent Goal: {goal}\n")
        print("=" * 60)
        
        # Main agent loop
        iteration = 0
        max_iterations = 5  # Prevent infinite loops (reduced to avoid retries on transient errors)
        # actions_taken initialized before pre-router to include preloaded MCP reads
        
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
                        # STEP 1: Validate action against policies (BEFORE execution)
                        try:
                            policy_engine.validate_action(tool_name, args)
                        except PolicyViolation as e:
                            # Policy violation - don't execute the tool
                            print(f"❌ POLICY VIOLATION: {e}")
                            
                            # Return error to GPT so it knows and can try alternative
                            error_result = json.dumps({
                                "success": False,
                                "error": f"Policy violation: {str(e)}"
                            })
                            
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": error_result
                            })
                            
                            # Log the blocked action
                            state_manager.log_action(
                                action_type=f"{tool_name}_blocked",
                                details={"args": args, "reason": str(e)},
                                success=False,
                                error=f"Policy violation: {str(e)}"
                            )
                            
                            continue  # Skip to next tool call
                        
                        # STEP 2: Execute tool via MCP (only if policy allows)
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
