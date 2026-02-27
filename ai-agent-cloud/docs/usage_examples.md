# Usage Examples - AI Agent with MCP

This guide provides practical examples of using the AI agent for various cloud infrastructure tasks.

---

## 🎯 Basic Usage Pattern

```python
from agent.core import run_agent_sync

# Define your goal in natural language
goal = "Your infrastructure requirement here"

# Run the agent
run_agent_sync(goal)
```

The agent will:

1. Connect to MCP servers (AWS, Azure, GCP)
2. Discover available cloud operations
3. Plan how to achieve the goal
4. Execute necessary actions
5. Report results

---

## 📝 Example Scenarios

### 1. List All Instances

**Goal**: See what's currently running

```python
from agent.core import run_agent_sync

goal = "List all EC2 instances in my AWS account and show their status"
run_agent_sync(goal)
```

**What the agent does:**

1. Calls `aws_list_ec2_instances`
2. Formats and displays results

**Expected output:**

```
🔧 Calling Tool: aws_list_ec2_instances
📊 Tool Result:
{
   "success": true,
   "count": 2,
   "instances": [
      {
         "id": "i-0abc123def456",
         "name": "web-server-1",
         "type": "t3.medium",
         "state": "running",
         "public_ip": "54.123.45.67"
      },
      ...
   ]
}

🧠 Final Agent Response:
You currently have 2 EC2 instances running:
- web-server-1 (t3.medium) - running at 54.123.45.67
- db-server-1 (t3.small) - running at 54.98.76.54
```

---

### 2. Create a Single Instance

**Goal**: Provision a new server

```python
goal = "Create a new EC2 instance named 'api-server' with 2 CPUs and 4GB RAM"
run_agent_sync(goal)
```

**What the agent does:**

1. Calls `aws_create_ec2_instance` with parameters:
   - name: "api-server"
   - cpu: 2
   - ram: 4
2. AWS maps this to `t3.medium` instance type
3. Instance is tagged with `ManagedBy: AIAgent`

**Expected AWS result:**

- New EC2 instance created
- Name: api-server
- Type: t3.medium (2 vCPU, 4GB)
- Status: pending → running (takes ~30 seconds)

---

### 3. Ensure Minimum Capacity

**Goal**: Maintain a fleet of servers (original thesis use case)

```python
goal = "Ensure I always have at least 3 running EC2 instances"
run_agent_sync(goal)
```

**What the agent does:**

1. Lists current instances
2. Counts how many are running
3. If < 3, creates new instances (with auto-generated names like "vm-1", "vm-2")
4. Reports final count

**Agent reasoning example:**

```
🔧 Calling Tool: aws_list_ec2_instances
📊 Tool Result: {"count": 1, "instances": [...]}

🧠 Agent thinks: "I found 1 running instance, but need 3.
                 I'll create 2 more instances."

🔧 Calling Tool: aws_create_ec2_instance
   Arguments: {"name": "vm-1", "cpu": 2, "ram": 4}

🔧 Calling Tool: aws_create_ec2_instance
   Arguments: {"name": "vm-2", "cpu": 2, "ram": 4}

🧠 Final Response: "I've ensured you have 3 running instances."
```

---

### 4. Create Different Instance Sizes

**Goal**: Provision servers with specific resources

```python
# Small instance (1 vCPU, 2GB) → t3.small
goal = "Create an instance named 'monitor' with 1 CPU and 2GB RAM"

# Large instance (4 vCPU, 16GB) → t3.xlarge
goal = "Create an instance named 'database' with 4 CPUs and 16GB RAM"

# Memory-optimized (2 vCPU, 16GB) → r5.large
goal = "Create an instance named 'cache' with 2 CPUs and 16GB RAM"
```

**Instance type mapping** (see [cloud_providers/aws/mapping.py](cloud_providers/aws/mapping.py)):

- 1 CPU, 1-2GB → `t3.micro` or `t3.small`
- 2 CPU, 4GB → `t3.medium`
- 2 CPU, 8GB → `t3.large`
- 4 CPU, 16GB → `t3.xlarge`
- High memory needs → `r5` family

---

### 5. Delete Specific Instance

**Goal**: Terminate a server

```python
# First, list to get ID
goal = "List all instances and show their IDs"
run_agent_sync(goal)

# Then delete by ID (copy from output)
goal = "Delete the EC2 instance with ID i-0abc123def456"
run_agent_sync(goal)
```

**What the agent does:**

1. Calls `aws_delete_ec2_instance` with instance ID
2. AWS initiates termination
3. Instance state changes: running → shutting-down → terminated

---

### 6. Check Instance Status

**Goal**: Get detailed info about a specific instance

```python
goal = "Get the status and details of EC2 instance i-0abc123def456"
run_agent_sync(goal)
```

**Agent returns:**

```json
{
  "id": "i-0abc123def456",
  "name": "web-server-1",
  "type": "t3.medium",
  "state": "running",
  "public_ip": "54.123.45.67",
  "private_ip": "172.31.10.25",
  "launch_time": "2026-02-26 10:30:00"
}
```

---

### 7. Conditional Operations

**Goal**: Smart decision-making

```python
# Create only if needed
goal = "If there are no running instances, create 2 new ones. Otherwise, do nothing."

# Scale based on count
goal = "Check how many instances are running. If more than 5, tell me. If less than 2, create more."

# Health check
goal = "List all instances. For any that are stopped or stopping, report their names."
```

**Agent uses reasoning:**

- Checks current state first
- Makes decisions based on conditions
- Only takes action when necessary

---

### 8. Cleanup All Managed Resources

**Goal**: Delete everything the agent created

```python
goal = "List all EC2 instances tagged with 'ManagedBy: AIAgent' and delete them"
run_agent_sync(goal)
```

**What the agent does:**

1. Calls `aws_list_ec2_instances` with tag filter
2. For each instance, calls `aws_delete_ec2_instance`
3. Confirms all deletions

**Use case**: Clean up after testing to avoid charges.

---

## 🔧 Advanced Usage

### Custom MCP Server Configuration

```python
from agent.core import run_agent

# Specify AWS region
mcp_servers = [
    {
        'name': 'aws',
        'command': 'python',
        'args': ['mcp_servers/aws_server.py'],
        'env': {
            'AWS_REGION': 'eu-west-1'  # Ireland region
        }
    }
]

await run_agent("Create instance in EU", mcp_servers=mcp_servers)
```

### Multi-Cloud (Future, when Azure/GCP implemented)

```python
mcp_servers = [
    {'name': 'aws', 'command': 'python', 'args': ['mcp_servers/aws_server.py']},
    {'name': 'azure', 'command': 'python', 'args': ['mcp_servers/azure_server.py']},
    {'name': 'gcp', 'command': 'python', 'args': ['mcp_servers/gcp_server.py']},
]

goal = "Create 1 instance on AWS, 1 VM on Azure, and 1 instance on GCP"
await run_agent(goal, mcp_servers=mcp_servers)
```

---

## 📊 Understanding Agent Output

### Tool Call Format

```
🔧 Calling Tool: aws_create_ec2_instance
   Arguments: {
      "name": "web-server-1",
      "cpu": 2,
      "ram": 4
   }
```

- **Tool name**: Which MCP tool is being called
- **Arguments**: Parameters passed to the tool

### Tool Result Format

```json
{
  "success": true,
  "message": "EC2 instance 'web-server-1' created successfully",
  "instance": {
    "id": "i-0abc123",
    "name": "web-server-1",
    "type": "t3.medium",
    "state": "pending"
  }
}
```

- **success**: Boolean - did it work?
- **message**: Human-readable summary
- **instance/details**: Returned data

### Final Agent Response

```
🧠 Final Agent Response:

I've successfully created a new EC2 instance named 'web-server-1'
with 2 CPUs and 4GB RAM. The instance type selected is t3.medium,
and it's currently starting up (status: pending).
The instance ID is i-0abc123.
```

The agent explains what it did in natural language.

---

## 🎓 Thesis Experiment Ideas

### Experiment 1: Task Completion Rate

Test various goals and measure success:

```python
test_goals = [
    "List all instances",                          # Simple
    "Create 1 instance",                           # Basic creation
    "Ensure 3 running instances",                  # Conditional logic
    "Create 5 instances with different names",     # Batch operation
    "Delete all stopped instances",                 # Conditional deletion
]

for goal in test_goals:
    print(f"\n{'='*60}\nTesting: {goal}\n{'='*60}")
    run_agent_sync(goal)
    # Record: success/failure, time taken, API calls made
```

**Metrics to collect:**

- Success rate (%)
- Average LLM calls per task
- Time to completion
- AWS API calls made

### Experiment 2: Cost Analysis

Track agent efficiency:

```python
goal = "Create 10 instances for a test environment"

# Measure:
# - Number of GPT-4 API calls
# - Token usage
# - AWS instance costs
# - Total cost vs manual provisioning
```

### Experiment 3: MCP vs Direct Implementation

Compare [`agent.py`](agent.py) (old, direct) vs [`agent/core.py`](agent/core.py) (new, MCP):

- Code complexity (lines of code)
- Extensibility (effort to add Azure)
- Maintainability (decoupling)
- Performance (latency)

---

## ⚠️ Important Notes

### 1. Instance States Take Time

EC2 instances aren't instant:

- **pending** (30-60 seconds) → **running**
- **shutting-down** (10-20 seconds) → **terminated**

The agent sees initial state ("pending"). Check AWS Console for final state.

### 2. Automatic Naming

If you don't specify a name, the agent uses defaults:

- "vm-1", "vm-2", etc.
- Based on system prompt defaults

Override by being specific: "Create an instance named 'my-api-server'"

### 3. All Instances Tagged

Every instance created by the agent has:

```
Tags:
  Name: <your-specified-name>
  ManagedBy: AIAgent
  CreatedBy: MCP-AWS-Server
```

Use this to track agent-created resources.

### 4. Default Instance Type

If not specified:

- CPU: 2 vCPUs
- RAM: 4 GB
- Result: `t3.medium` (~$0.04/hour)

---

## 🛠️ Debugging Tips

### Enable Verbose Logging

The agent already prints detailed logs. To see even more:

```python
import logging
logging.basicConfig(level=logging.DEBUG)

from agent.core import run_agent_sync
run_agent_sync("Your goal here")
```

### Test MCP Server Directly

Verify the server works independently:

```bash
python mcp_servers/aws_server.py
```

Then send MCP commands manually (advanced).

### Check AWS Console

Always verify in AWS Console:

- EC2 Dashboard: https://console.aws.amazon.com/ec2/
- View instances, their states, IPs, tags

---

## 🚀 Next Steps

1. Try the basic examples above
2. Experiment with complex goals
3. Monitor AWS costs
4. Document interesting behaviors for thesis
5. Start implementing advanced features (self-healing, optimization)

---

**Usage Guide Last Updated**: February 26, 2026
