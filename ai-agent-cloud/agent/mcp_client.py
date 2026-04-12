"""
MCP Client Manager
Handles connections to MCP servers and tool discovery.

This module implements the MCP CLIENT side:
- Spawns MCP servers as subprocesses (e.g., aws_server.py)
- Connects to servers via stdio (standard input/output)
- Discovers available tools from servers
- Calls tools on servers via MCP protocol
- Converts MCP tools to OpenAI function calling format

MCP Protocol:
- Uses JSON-RPC over stdio (stdin/stdout)
- Client sends requests like: {"method": "tools/list"}
-  Server responds with: {"result": {"tools": [...]}}

Why stdio?
- Simple: No network configuration needed
- Secure: No ports to expose
- Portable: Works on Windows/Linux/Mac
"""

import asyncio
import json
import os
from contextlib import AsyncExitStack  # Manages multiple async resources
from typing import List, Dict, Any

# Official MCP Python SDK - provides CLIENT functionality
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPClientManager:
    """
    Manages connections to multiple MCP servers and provides unified tool access.
    
    This client can connect to multiple MCP servers (AWS, Azure, GCP) simultaneously
    and aggregate their tools for the AI agent to use.
    """
    
    def __init__(self):
        """Initialize the MCP client manager."""
        self.sessions: Dict[str, ClientSession] = {}
        self.tools: Dict[str, Dict] = {}  # tool_name -> tool_definition
        self.tool_server_mapping: Dict[str, str] = {}  # tool_name -> server_name
        self.prompts: Dict[str, Dict] = {}  # prompt_name -> prompt definition
        self.prompt_server_mapping: Dict[str, str] = {}  # prompt_name -> server_name
        self.exit_stack = AsyncExitStack()
    
    async def connect_to_server(self, server_name: str, command: str, args: List[str] = None, env: Dict[str, str] = None):
        """
        Connect to an MCP server by spawning it as a subprocess.
        
        Process:
        1. Create subprocess parameters (command, args, env vars)
        2. Spawn server process with stdio_client (captures stdin/stdout)
        3. Create MCP session (ClientSession) for communication
        4. Send "initialize" message to server
        5. Store session for later use
        
        Args:
            server_name: Identifier for this server (e.g., 'aws', 'azure')
            command: Command to run the server (e.g., 'python')
            args: Arguments for the command (e.g., ['mcp_servers/aws_server.py'])
            env: Optional environment variables for the server process
                 (e.g., {'AWS_REGION': 'us-east-1', 'AWS_ACCESS_KEY_ID': '...'})
        
        Example:
            await client.connect_to_server(
                'aws',
                'python',
                ['mcp_servers/aws_server.py'],
                env={'AWS_REGION': 'us-east-1'}
            )
        
        Technical Details:
        - Server runs as separate process (your Python spawns another Python)
        - Communication via stdio: Client writes to server's stdin, reads from stdout
        - MCP protocol: JSON-RPC messages like {"jsonrpc": "2.0", "method": "tools/list"}
        """
        if args is None:
            args = []
        
        # Merge custom env with parent env so PATH/PYTHONPATH and other
        # required variables are preserved for MCP server subprocess startup.
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)

        # Configure subprocess parameters
        # This tells MCP SDK how to start the server process
        server_params = StdioServerParameters(
            command=command,  # e.g., 'python'
            args=args,  # e.g., ['mcp_servers/aws_server.py']
            env=merged_env  # includes inherited env + overrides
        )
        
        # Spawn server as subprocess and get stdio streams
        # stdio_client runs: subprocess.Popen([command] + args, stdin=PIPE, stdout=PIPE, env=env)
        # Returns (read_stream, write_stream) for communication
        stdio_transport = await self.exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        read_stream, write_stream = stdio_transport  # Streams for reading/writing to subprocess
        
        # Create and initialize MCP session
        # ClientSession handles MCP protocol (JSON-RPC over stdio)
        session = await self.exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        
        # Send "initialize" message to server
        # Server responds with its capabilities (name, version, supported features)
        await session.initialize()
        
        # Store session for later tool calls
        self.sessions[server_name] = session
        
        print(f"✅ Connected to MCP server: {server_name}")
    
    async def discover_tools(self):
        """
        Discover all tools from all connected MCP servers.
        
        Process:
        1. Loop through all connected servers
        2. Send MCP "list_tools" request to each server
        3. Server responds with tool definitions (name, description, schema)
        4. Store tools and track which server provides each tool
        
        Populates:
        - self.tools: {tool_name: {name, description, inputSchema}}
        - self.tool_server_mapping: {tool_name: server_name}
        
        MCP Protocol:
        - Request: {"jsonrpc": "2.0", "method": "tools/list", "id": 1}
        - Response: {
            "result": {
              "tools": [
                {
                  "name": "aws_list_ec2_instances",
                  "description": "List all EC2 instances...",
                  "inputSchema": {
                    "type": "object",
                    "properties": {"tag_filter": {...}}
                  }
                }
              ]
            }
          }
        """
        self.tools = {}
        self.tool_server_mapping = {}
        
        for server_name, session in self.sessions.items():
            try:
                # Send MCP "list_tools" request to server
                # Server's @mcp.tool() decorated functions are returned here
                tools_result = await session.list_tools()
                
                # Store each tool
                for tool in tools_result.tools:
                    tool_name = tool.name
                    
                    # Store tool definition
                    # This is what GPT-4 will see when deciding which tools to call
                    self.tools[tool_name] = {
                        'name': tool_name,  # e.g., "aws_list_ec2_instances"
                        'description': tool.description,  # From function docstring
                        'inputSchema': tool.inputSchema  # JSON schema from type hints
                    }
                    
                    # Track which server provides this tool
                    # Needed later to route tool calls to correct server
                    self.tool_server_mapping[tool_name] = server_name
                
                print(f"📦 Discovered {len(tools_result.tools)} tools from {server_name}")
            
            except Exception as e:
                print(f"❌ Error discovering tools from {server_name}: {e}")

    async def discover_prompts(self):
        """
        Discover prompt templates from all connected MCP servers.

        Populates:
        - self.prompts: {prompt_name: {name, description, arguments}}
        - self.prompt_server_mapping: {prompt_name: server_name}
        """
        self.prompts = {}
        self.prompt_server_mapping = {}

        for server_name, session in self.sessions.items():
            try:
                prompts_result = await session.list_prompts()

                for prompt in prompts_result.prompts:
                    prompt_name = prompt.name
                    self.prompts[prompt_name] = {
                        "name": prompt_name,
                        "description": getattr(prompt, "description", ""),
                        "arguments": [
                            {
                                "name": arg.name,
                                "description": getattr(arg, "description", ""),
                                "required": getattr(arg, "required", False),
                            }
                            for arg in getattr(prompt, "arguments", [])
                        ],
                    }
                    self.prompt_server_mapping[prompt_name] = server_name

                print(f"🧠 Discovered {len(prompts_result.prompts)} prompts from {server_name}")

            except Exception as e:
                print(f"❌ Error discovering prompts from {server_name}: {e}")

    async def discover_capabilities(self):
        """Discover tools and prompts from connected MCP servers."""
        await self.discover_tools()
        await self.discover_prompts()

    async def get_prompt(self, prompt_name: str, arguments: Dict[str, str] = None) -> str:
        """
        Render an MCP prompt template with optional string arguments.

        Returns:
            JSON string containing the rendered prompt message sequence.
        """
        if prompt_name not in self.prompt_server_mapping:
            raise ValueError(f"Unknown MCP prompt: {prompt_name}")

        server_name = self.prompt_server_mapping[prompt_name]
        session = self.sessions[server_name]
        result = await session.get_prompt(prompt_name, arguments or {})

        normalized_messages = []
        for msg in result.messages:
            content = msg.content
            content_payload = {
                "type": getattr(content, "type", content.__class__.__name__),
            }

            if hasattr(content, "text"):
                content_payload["text"] = content.text
            if hasattr(content, "data"):
                content_payload["data"] = content.data
            if hasattr(content, "mimeType"):
                content_payload["mime_type"] = content.mimeType
            if hasattr(content, "resource"):
                content_payload["resource"] = str(content.resource)

            normalized_messages.append(
                {
                    "role": msg.role,
                    "content": content_payload,
                }
            )

        return json.dumps(
            {
                "success": True,
                "name": prompt_name,
                "server": server_name,
                "description": result.description,
                "messages": normalized_messages,
            }
        )
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """
        Call a tool on the appropriate MCP server.
        
        Flow:
        1. GPT-4 decides to call a tool (e.g., "aws_create_ec2_instance")
        2. Agent calls this method with tool name + arguments
        3. Look up which server provides this tool
        4. Send MCP "call_tool" request to that server
        5. Server executes the tool (calls boto3, etc.)
        6. Server returns result
        7. Result goes back to agent → state manager → GPT-4
        
        Args:
            tool_name: Name of tool to call (e.g., "aws_create_ec2_instance")
            arguments: Dict of arguments (e.g., {"name": "my-vm", "cpu": 1, "ram_gb": 1})
        
        Returns:
            Tool execution result text from the server
        
        Raises:
            ValueError: If tool not found
            Exception: If tool execution fails
        
        MCP Protocol:
        - Request: {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
              "name": "aws_create_ec2_instance",
              "arguments": {"name": "my-vm", "cpu": 1, "ram_gb": 1}
            },
            "id": 2
          }
        - Response: {
            "result": {
              "content": [{"type": "text", "text": "[SUCCESS] Created instance i-abc123"}]
            }
          }
        
        Example:
            result = await client.call_tool(
                "aws_create_ec2_instance",
                {"name": "web-server", "cpu": 2, "ram_gb": 4}
            )
        """
        if tool_name == "get_mcp_prompt":
            prompt_name = arguments.get("name")
            if not prompt_name:
                raise ValueError("get_mcp_prompt requires 'name'")

            prompt_args = arguments.get("arguments", {})
            if prompt_args is None:
                prompt_args = {}
            if not isinstance(prompt_args, dict):
                raise ValueError("get_mcp_prompt 'arguments' must be an object")

            normalized_prompt_args = {str(k): str(v) for k, v in prompt_args.items()}
            return await self.get_prompt(prompt_name, normalized_prompt_args)

        # STEP 1: Find which server provides this tool
        # (from mapping built during discover_tools())
        if tool_name not in self.tool_server_mapping:
            raise ValueError(f"Unknown tool: {tool_name}")
        
        server_name = self.tool_server_mapping[tool_name]
        session = self.sessions[server_name]
        
        # STEP 2: Send MCP "call_tool" request to the server
        # This is where the actual boto3 code executes on the server side
        # The server's @mcp.tool() decorated function is invoked
        result = await session.call_tool(tool_name, arguments)
        
        # STEP 3: Extract text content from MCP result
        # MCP returns a structured result with content items
        if result.content and len(result.content) > 0:
            # MCP returns list of content items
            content_item = result.content[0]
            if hasattr(content_item, 'text'):
                # Extract the text response (e.g., "[SUCCESS] Created instance i-abc123")
                return content_item.text
        
        # Fallback: convert entire result to string
        return str(result)
    
    def get_tools_for_openai(self) -> List[Dict]:
        """
        Convert MCP tools to OpenAI function calling format.
        
        Purpose:
        - MCP tools have one format (name, description, inputSchema)
        - OpenAI expects a different format (type, function with name/description/parameters)
        - This method converts MCP tools → OpenAI function calling format
        
        Process:
        1. Loop through all discovered MCP tools
        2. Wrap each in OpenAI's expected structure
        3. Return list that can be passed to OpenAI API
        
        OpenAI Function Calling Format:
        {
          "type": "function",
          "function": {
            "name": "aws_create_ec2_instance",
            "description": "Create a new EC2 instance...",
            "parameters": {
              "type": "object",
              "properties": {
                "name": {"type": "string", "description": "Instance name"},
                "cpu": {"type": "integer", "description": "Number of CPU cores"}
              },
              "required": ["name", "cpu", "ram_gb"]
            }
          }
        }
        
        Returns:
            List of tool definitions in OpenAI's expected format
        
        Usage:
            tools = mcp_client.get_tools_for_openai()
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[...],
                tools=tools  # ← This is where converted tools are used
            )
        """
        openai_tools = []
        
        # Convert each MCP tool to OpenAI format
        for tool_name, tool_def in self.tools.items():
            openai_tools.append({
                'type': 'function',  # OpenAI requires this field
                'function': {
                    'name': tool_name,  # e.g., "aws_list_ec2_instances"
                    'description': tool_def['description'],  # From @mcp.tool() docstring
                    'parameters': tool_def['inputSchema']  # JSON schema from type hints
                }
            })

        # Synthetic helper for MCP prompt templates.
        openai_tools.append({
            "type": "function",
            "function": {
                "name": "get_mcp_prompt",
                "description": "Render an MCP prompt template by name with optional arguments.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "MCP prompt name",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Prompt arguments as string key/value pairs",
                            "additionalProperties": {
                                "type": "string",
                            },
                        },
                    },
                    "required": ["name"],
                },
            },
        })
        
        return openai_tools
    
    async def close(self):
        """
        Close all MCP server connections and clean up resources.
        
        Process:
        1. Close all MCP sessions (cleanly disconnect from servers)
        2. Terminate server subprocesses (python aws_server.py, etc.)
        3. Close stdio streams
        4. Release all resources
        
        Called when:
        - Agent completes its task
        - Error occurs and cleanup is needed
        - Application is shutting down
        
        Uses AsyncExitStack:
        - Automatically manages cleanup of all resources
        - Ensures proper shutdown even if errors occur
        - Similar to context managers (with statement)
        """
        # Close all sessions and subprocesses
        # AsyncExitStack handles cleanup in reverse order of creation
        await self.exit_stack.aclose()
        print("🔌 All MCP connections closed")
