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
import sys
from typing import Dict, List, Set
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
    "- For cost-optimization tasks, default to recommendation-only analysis unless the user explicitly asks to execute changes\n"
    "- Validate assumptions by calling tools before concluding\n"
    "- If user asks for an MCP resource URI (aws://...), call read_mcp_resource instead of passing URI to AWS API tools\n"
    "- If user asks to use a named MCP prompt template, call get_mcp_prompt first\n"
    "- For host-level operations on EC2 instances, prefer SSM tools over ad-hoc shell approaches\n"
    "- Report exactly what you changed/found\n"
     "- Only execute mutating optimization actions (resize/stop/delete/apply) when user explicitly asks to take actions\n"
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
        "- For incident/alarm-response tasks, call aws_poll_alarm_notifications first\n"
        "- If the goal already contains explicit alarm context from an SQS worker, do not call aws_poll_alarm_notifications again unless context is missing\n"
        "- Prioritize newest notifications where alarm.new_state is ALARM\n"
        "- For alarms related to an EC2 instance, do not rely on alarm_name_prefix with instance Name\n"
        "- Prefer aws_list_ec2_alarms with instance_name or instance_id\n"
        "- If unavailable, resolve instance ID first, then filter aws_list_alarms by dimensions where name='InstanceId'\n"
        "- Do not conclude 'no alarms' without this dimension-based check\n"
        "- For CloudWatch metric queries, choose period_seconds by lookback window to avoid oversampling:\n"
        "  * up to 30 minutes -> 60 seconds\n"
        "  * 31 to 180 minutes -> 300 seconds\n"
        "  * more than 180 minutes -> 900 seconds or more\n"
        "- If user asks for last hour metrics, explicitly call aws_get_ec2_metrics with period_seconds=300\n"
        "- Log group routing for incident triage:\n"
        "  * /ai-agent/app -> real-api request flow, input validation, order submission errors\n"
        "  * /ai-agent/worker -> Celery task execution, retries/failures, processing latency\n"
        "  * /ai-agent/otel -> OpenTelemetry collector pipeline/export issues to X-Ray\n"
        "  * /ai-agent/system -> OS/systemd/network/runtime host-level issues\n"
        "  * /ai-agent/agent -> alarm_worker and agent orchestration/decision logs\n"
        "- For root-cause analysis, correlate timestamps across app + worker + otel logs before concluding\n"
        "- For host-level triage, prefer aws_collect_ec2_health_snapshot and aws_ssm_collect_host_diagnostics before proposing mitigations\n"
        "- For disk pressure scenarios, run aws_ssm_safe_disk_cleanup with dry_run=true first and only apply cleanup with explicit execution intent\n"
    ),
    "ssm_execution": (
        "SSM COMMAND EXECUTION:\n"
        "- Prefer aws_ssm_run_command as the default for remote command execution\n"
        "- Include target_os whenever known (amazon-linux, ubuntu, rhel, etc.) so policy checks can enforce OS-safe commands\n"
        "- Never run destructive host commands (shutdown/reboot/rm -rf/mkfs) unless user explicitly requests and policy allows\n"
        "- For normal operations, call aws_ssm_run_command with wait_for_completion=true to get stdout/stderr inline\n"
        "- Use aws_ssm_get_command_output only when:\n"
        "  * a command was submitted with wait_for_completion=false, or\n"
        "  * a previous command timed out client-side and you need a later/follow-up fetch, or\n"
        "  * you are checking progress of a long-running command\n"
        "- If goal references an instance by name only, resolve it first with aws_list_ec2_instances to obtain instance_id\n"
    ),
    "xray_tracing": (
        "X-RAY TRACE ANALYSIS:\n"
        "- Prefer aws_get_xray_trace_summaries first, then drill down with aws_get_xray_trace_details when needed\n"
        "- If the goal includes a service name, pass it via service_names (example: [\"real-api\"])\n"
        "- Never pass EC2 instance names or instance IDs as service_names\n"
        "- If goal mentions instance but not service: call aws_get_xray_trace_summaries with exclude_loopback_only=true and no service_names, then use analysis.top_service_names to choose service_names for a second query\n"
        "- For generic trace requests with no service specified, set exclude_loopback_only=true to avoid localhost-only noise\n"
        "- Do not assume traces can be filtered by EC2 instance ID directly; X-Ray filtering is service/trace based\n"
        "- If summaries indicate loopback-only or mostly zero-duration traces, call aws_get_xray_service_graph and/or aws_get_xray_trace_details before concluding\n"
        "- During mitigation analysis, cross-check X-Ray findings with log groups /ai-agent/app, /ai-agent/worker, and /ai-agent/otel\n"
        "- In final answers, summarize analysis fields first (fault/error/throttle counts, top services, warnings) before listing raw trace IDs\n"
    ),
    "cost_optimization": (
        "COST OPTIMIZATION MODE:\n"
        "- If no explicit execution intent is present, provide recommendations, rationale, and projected savings only\n"
        "- Only execute mutating optimization actions (resize/stop/delete/apply) when user explicitly asks to take actions\n"
        "- In execution mode, run aws_resize_ec2_instance with dry_run=true first to validate compatibility and target choice\n"
        "- Apply with aws_apply_ec2_rightsizing only when compatibility is true and estimated_hourly_savings is positive\n"
        "- For resize actions, require compatibility checks and backup plan before changes\n"
        "- When calling aws_apply_ec2_rightsizing, do not set min_cpu/min_ram_gb unless the user explicitly asked for hard minimum capacity\n"
    ),
}


POLICY_DOMAIN_KEYWORDS = {
    "security_groups": ["security group", "ingress", "egress", "cidr", "firewall", "port"],
    "ssm": ["ssm", "systemctl", "service", "daemon", "command", "package", "dnf", "yum", "apt"],
    "ec2": ["ec2", "instance", "autoscaling", "scale", "rightsiz", "cost", "budget", "compute optimizer", "cheapest"],
    "vpc": ["vpc", "subnet", "route table", "internet gateway", "igw", "cidr", "network"],
    "nat_gateway": ["nat", "nat gateway"],
}

POLICY_DOMAIN_TO_SECTIONS = {
    "security_groups": ["security_groups"],
    "ssm": ["ssm"],
    "ec2": ["ec2", "general", "cost_optimization"],
    "vpc": ["vpc", "general"],
    "nat_gateway": ["nat_gateway", "general", "cost_optimization"],
}


def infer_policy_domains_from_goal(goal: str) -> List[str]:
    """Infer which policy domains are relevant for this goal."""
    goal_text = (goal or "").lower()
    matched: List[str] = []
    for domain, keywords in POLICY_DOMAIN_KEYWORDS.items():
        if any(keyword in goal_text for keyword in keywords):
            matched.append(domain)
    return matched


def infer_policy_domains_from_tool(tool_name: str) -> Set[str]:
    """Infer policy domains based on the tool currently being called."""
    name = (tool_name or "").lower()
    domains: Set[str] = set()

    if name.startswith("aws_ssm_"):
        domains.add("ssm")
    if "security_group" in name:
        domains.add("security_groups")
    if "nat_gateway" in name:
        domains.add("nat_gateway")
    if any(k in name for k in ["vpc", "subnet", "route", "internet_gateway", "igw"]):
        domains.add("vpc")
    if "ec2" in name or "instance" in name:
        domains.add("ec2")

    return domains


def build_policy_discovery_hint(policies: dict) -> str:
    """Build a tiny policy hint that avoids loading full policy content every run."""
    if not policies:
        return "POLICY ENFORCEMENT: No policy file loaded at runtime."

    available_domains = [
        domain for domain, sections in POLICY_DOMAIN_TO_SECTIONS.items()
        if any(section in policies for section in sections)
    ]

    return (
        "POLICY ENFORCEMENT IS ACTIVE. "
        "To reduce token usage, full policy content is not preloaded by default. "
        "Relevant policy sections will be injected only when goal/tool intent requires them.\n"
        f"Available policy domains: {', '.join(sorted(available_domains))}"
    )


def build_policy_context_for_domains(policies: dict, domains: List[str], reason: str) -> str:
    """Build a compact policy context for selected domains only."""
    if not policies:
        return "No policy content available to inject."

    selected_sections: Dict[str, dict] = {}
    for domain in domains:
        for section_name in POLICY_DOMAIN_TO_SECTIONS.get(domain, []):
            if section_name in policies:
                selected_sections[section_name] = policies.get(section_name, {})

    if not selected_sections:
        return (
            f"POLICY CONTEXT ({reason}): no matching sections found for domains {domains}."
        )

    return (
        f"POLICY CONTEXT ({reason}) - selected domains only:\n"
        f"{json.dumps(selected_sections, indent=2)}\n\n"
        "Plan and execute actions that comply with these constraints."
    )


def build_system_prompt(goal: str) -> str:
    """Build a compact, goal-aware system prompt to reduce token bloat."""
    goal_text = (goal or "").lower()
    parts = [BASE_SYSTEM_PROMPT]

    if any(k in goal_text for k in ["security group", "ingress", "egress", "cidr", "sg-"]):
        parts.append(INSTRUCTION_PACKS["security_groups"])

    if "vpc" in goal_text and any(k in goal_text for k in ["delete", "remove", "destroy"]):
        parts.append(INSTRUCTION_PACKS["vpc_deletion"])

    if any(k in goal_text for k in ["alarm", "cloudwatch", "dashboard", "log", "metric", "incident", "root cause", "mitigation"]):
        parts.append(INSTRUCTION_PACKS["cloudwatch_alarms"])

    if any(k in goal_text for k in [
        "ssm", "systemctl", "service", "daemon", "start", "stop", "restart", "host", "command" ,
    ]):
        parts.append(INSTRUCTION_PACKS["ssm_execution"])

    if any(k in goal_text for k in ["xray", "trace", "tracing", "latency", "fault", "error rate"]):
        parts.append(INSTRUCTION_PACKS["xray_tracing"])

    if any(k in goal_text for k in ["cost", "optimiz", "rightsiz", "saving", "cheapest", "compute optimizer"]):
        parts.append(INSTRUCTION_PACKS["cost_optimization"])

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


def is_cost_optimization_goal(goal: str) -> bool:
    goal_lc = (goal or "").lower()
    return any(
        keyword in goal_lc
        for keyword in [
            'cost optimization',
            'cost',
            'rightsiz',
            'optimiz',
            'cheapest',
            'compute optimizer',
            'save money',
            'savings',
        ]
    )


def has_explicit_execution_intent(goal: str) -> bool:
    goal_lc = (goal or "").lower()
    return any(
        token in goal_lc
        for token in [
            'take action',
            'apply',
            'execute',
            'do it',
            'perform',
            'implement',
            'resize now',
            'fix it now',
            'change it',
        ]
    )


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
                'command': sys.executable,  # Use current interpreter/venv
                'args': [os.path.join(os.path.dirname(__file__), '..', 'mcp_servers', 'aws_server.py')],
                'env': aws_env  # Environment variables (AWS credentials)
            }
        ]
    
    actions_taken = []
    execution_result = {
        "success": False,
        "goal": goal,
        "outcome": "not_started",
        "reason": "not_started",
        "actions_taken": actions_taken,
    }

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
        cost_goal = is_cost_optimization_goal(goal)
        explicit_execution_intent = has_explicit_execution_intent(goal)
        recommendation_only_cost_mode = cost_goal and not explicit_execution_intent

        # Initialize conversation
        goal_policy_domains = infer_policy_domains_from_goal(goal)
        injected_policy_domains: Set[str] = set(goal_policy_domains)

        messages = [
            {
                "role": "system",
                "content": build_system_prompt(goal)
            },
            {
                "role": "system",
                "content": build_policy_discovery_hint(policy_engine.policies)
            },
            {
                "role": "system",
                "content": build_capability_catalog(mcp_client)
            },
            {"role": "user", "content": goal}
        ]

        if recommendation_only_cost_mode:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "EXECUTION INTENT: recommendation-only for cost optimization. "
                        "Do not execute mutating optimization actions unless the user explicitly asks."
                    ),
                }
            )
        elif cost_goal and explicit_execution_intent:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "EXECUTION INTENT: user explicitly requested applying optimization actions. "
                        "If analysis indicates safe savings, execute the action flow and report outcome."
                    ),
                }
            )

        if goal_policy_domains:
            messages.append(
                {
                    "role": "system",
                    "content": build_policy_context_for_domains(
                        policy_engine.policies,
                        goal_policy_domains,
                        reason="goal_intent",
                    ),
                }
            )

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
        max_iterations = 10  # Prevent infinite loops (reduced to avoid retries on transient errors)
        # actions_taken initialized before pre-router to include preloaded MCP reads
        
        while iteration < max_iterations:
            iteration += 1
            
            # Call OpenAI with available tools
            response = client.chat.completions.create(
                model="gpt-4.1-mini",
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
                        if recommendation_only_cost_mode and tool_name in {
                            'aws_resize_ec2_instance',
                            'aws_apply_ec2_rightsizing',
                            'aws_stop_ec2_instance',
                            'aws_delete_ec2_instance',
                        }:
                            blocked_msg = (
                                "Recommendation-only mode is active for this cost optimization goal. "
                                "User did not explicitly request execution of mutating actions."
                            )
                            print(f"⚠️  {blocked_msg}")
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps({"success": False, "error": blocked_msg}),
                            })
                            continue

                        # STEP 1: Validate action against policies (BEFORE execution)
                        try:
                            policy_engine.validate_action(tool_name, args)

                            policy_recommendations = policy_engine.pop_policy_recommendations()
                            if policy_recommendations:
                                messages.append(
                                    {
                                        "role": "system",
                                        "content": (
                                            "POLICY RECOMMENDATIONS (non-blocking):\n"
                                            f"{json.dumps(policy_recommendations, indent=2)}\n"
                                            "Prefer the most cost-safe compliant path unless user explicitly asks otherwise."
                                        ),
                                    }
                                )
                                state_manager.log_action(
                                    action_type='policy_recommendations_emitted',
                                    details={'tool_name': tool_name, 'recommendations': policy_recommendations},
                                    success=True,
                                )
                        except PolicyViolation as e:
                            # Policy violation - don't execute the tool
                            print(f"❌ POLICY VIOLATION: {e}")

                            violation_domains = infer_policy_domains_from_tool(tool_name)
                            new_domains = [
                                domain for domain in sorted(violation_domains)
                                if domain not in injected_policy_domains
                            ]
                            if new_domains:
                                messages.append(
                                    {
                                        "role": "system",
                                        "content": build_policy_context_for_domains(
                                            policy_engine.policies,
                                            new_domains,
                                            reason=f"policy_violation:{tool_name}",
                                        ),
                                    }
                                )
                                injected_policy_domains.update(new_domains)
                            
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

                                # Track cost-optimization recommendation/action KPIs when tool returns savings metadata.
                                estimated_hourly_savings = float(result_obj.get('estimated_hourly_savings', 0.0) or 0.0)
                                estimated_monthly_savings = float(result_obj.get('estimated_monthly_savings', 0.0) or 0.0)
                                if tool_name in {
                                    'aws_analyze_ec2_cost_optimization',
                                    'aws_analyze_ec2_fleet_cost_optimization',
                                    'aws_get_compute_optimizer_recommendations',
                                    'aws_detect_idle_cost_leaks',
                                }:
                                    state_manager.log_cost_recommendation(
                                        recommendation_type=tool_name,
                                        details={'args': args, 'result_summary': result_obj},
                                        estimated_hourly_savings_usd=estimated_hourly_savings,
                                        estimated_monthly_savings_usd=estimated_monthly_savings,
                                    )
                                if tool_name in {'aws_resize_ec2_instance', 'aws_apply_ec2_rightsizing'}:
                                    state_manager.log_cost_action_applied(
                                        action_type=tool_name,
                                        details={'args': args, 'result_summary': result_obj},
                                        estimated_hourly_savings_usd=estimated_hourly_savings,
                                        estimated_monthly_savings_usd=estimated_monthly_savings,
                                    )
                                
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

                execution_result = {
                    "success": True,
                    "goal": goal,
                    "outcome": msg.content if msg.content else "Completed",
                    "reason": "completed",
                    "actions_taken": actions_taken,
                    "iterations": iteration,
                }
                
                # Show statistics
                stats = state_manager.get_statistics()
                print(f"\n📊 Session Statistics:")
                print(f"   Total goals executed: {stats.get('total_goals_executed', 0)}")
                print(f"   Total resources created: {stats.get('total_resources_created', 0)}")
                print(f"   Total resources deleted: {stats.get('total_resources_deleted', 0)}")
                
                break
        
        if iteration >= max_iterations:
            print("\n⚠️  Agent reached maximum iterations")
            timeout_outcome = (
                f"Agent reached maximum iterations ({max_iterations}) before producing a final response."
            )
            state_manager.log_goal_execution(
                goal=goal,
                outcome=timeout_outcome,
                actions_taken=actions_taken + ["max_iterations_reached"],
            )
            execution_result = {
                "success": False,
                "goal": goal,
                "outcome": timeout_outcome,
                "reason": "max_iterations_reached",
                "actions_taken": actions_taken,
                "iterations": iteration,
                "max_iterations": max_iterations,
            }
    
    finally:
        # Clean up MCP connections
        await mcp_client.close()

    return execution_result


# Synchronous wrapper for convenience
def run_agent_sync(goal: str, mcp_servers: list = None):
    """
    Synchronous wrapper for run_agent.
    
    Args:
        goal: The objective for the agent to accomplish
        mcp_servers: Optional list of MCP server configurations
    """
    return asyncio.run(run_agent(goal, mcp_servers))


if __name__ == "__main__":
    # Example usage
    run_agent_sync("List all EC2 instances and create one if there are none")
