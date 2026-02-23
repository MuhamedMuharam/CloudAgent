import json
import os
from dotenv import load_dotenv
from openai import OpenAI
from tools import list_vms, create_vm, delete_vm

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# Define Tools in LLM Format
tools = [
    {
        "type": "function",
        "function": {
            "name": "list_vms",
            "description": "List all virtual machines",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_vm",
            "description": "Create a new virtual machine",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "cpu": {"type": "number"},
                    "ram": {"type": "number"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_vm",
            "description": "Delete a virtual machine",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"}
                },
                "required": ["name"]
            }
        }
    }
]

# tool dispatcher
def execute_tool(tool_name, args):
    if tool_name == "list_vms":
        return list_vms()
    if tool_name == "create_vm":
        return create_vm(**args)
    if tool_name == "delete_vm":
        return delete_vm(**args)


# Main Agent Loop
def run_agent(goal):
    messages = [
        {
            "role": "system",
           "content": (
                "You are an autonomous cloud infrastructure agent. "
                "You manage virtual machines using available tools. "
                "Think step-by-step and call tools when needed. "
                "Make decisions independently without asking the user. "
                "Use sensible defaults: name VMs as 'vm-1', 'vm-2', etc., "
                "with 2 CPU cores and 4GB RAM unless specified otherwise. "
                "Complete tasks immediately and report what you did."
            )
        },
        {"role": "user", "content": goal}
    ]

    while True:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )

        msg = response.choices[0].message

        # If AI decides to call tool
        if msg.tool_calls:
            messages.append(msg)
            
            # Process ALL tool calls (AI can call multiple tools at once)
            for tool_call in msg.tool_calls:
                tool_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)

                print(f"\n🔧 Calling Tool: {tool_name} {args}")

                result = execute_tool(tool_name, args)

                print(f"📊 Tool Result: {result}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,   
                    "content": json.dumps(result)
                })
        else:
            print("\n🧠 Final Agent Output:\n", msg.content)
            break   


# example that runs the agent with a specific goal
if __name__ == "__main__":
    run_agent("Ensure that I always have at least 2 running servers.")        