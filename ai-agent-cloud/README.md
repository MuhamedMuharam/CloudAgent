# 🤖 AI Agent Cloud Infrastructure with MCP

> **Autonomous cloud resource management using AI agents and the Model Context Protocol**

A bachelor thesis project exploring how AI agents can autonomously manage cloud infrastructure (AWS, Azure, GCP) using natural language goals instead of traditional Infrastructure as Code.

---

## 🎯 What is This?

This project demonstrates an **autonomous AI agent** that manages cloud infrastructure by:

- **Understanding goals in natural language**: "Ensure I have 2 running web servers"
- **Making intelligent decisions**: Checks current state, plans actions, executes operations
- **Using multiple cloud providers**: AWS (implemented), Azure & GCP (architecture ready)
- **Operating safely**: Tagged resources, state tracking, future guardrails

**Key Innovation**: Uses **Model Context Protocol (MCP)** to decouple the AI agent from cloud provider implementations, enabling true multi-cloud support.

---

## 🏗️ Architecture Overview

```
User: "Create 2 EC2 instances"
         ↓
    AI Agent (GPT-4o-mini + MCP Client)
         ↓
    MCP AWS Server ←→ Boto3 ←→ AWS Cloud
    MCP Azure Server (future)
    MCP GCP Server (future)
```

**Traditional IaC**: Write YAML/HCL → Run terraform apply → Hope it works  
**This Project**: Describe goal → Agent plans & executes → Done

---

## ✨ Features

### Currently Implemented

- ✅ **MCP-based architecture** - Decoupled agent and cloud providers
- ✅ **Observability helper agent** - Dedicated sub-agent for logs/metrics/traces summarization before controller planning
- ✅ **AWS EC2 management** - Create, list, delete, monitor instances
- ✅ **Scheduled cost optimization worker** - Deployed on ECS Fargate and triggered weekly by EventBridge Scheduler
- ✅ **Dynamic tool discovery** - Agent discovers capabilities from MCP servers
- ✅ **Intelligent resource mapping** - Generic specs (2 CPU, 4GB RAM) → AWS instance types
- ✅ **Multi-server support** - Architecture supports multiple clouds simultaneously
- ✅ **Resource tagging** - All resources tagged for tracking and cleanup
- ✅ **Persistent state tracking** - Audit logs and infrastructure snapshots

### Future Implementations (Thesis Scope)

- 🚧 **Azure VM management** - Parallel to AWS
- 🚧 **GCP Compute Engine** - Complete multi-cloud
- 🚧 **Self-healing** - Detect and recover from failures
- 🚧 **Security guardrails** - Policy engine to prevent dangerous operations

---

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- AWS account with credentials
- OpenAI API key

### Installation

1. **Clone and navigate:**

   ```bash
   cd "your-project-directory"
   ```

2. **Install dependencies:**

   ```bash
   python -m venv venv
   venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   ```

3. **Configure credentials:**

   ```bash
   copy .env.example .env
   # Edit .env with your AWS and OpenAI credentials
   ```

4. **Run the agent:**
   ```bash
   python main.py
   ```

**Detailed setup instructions**: See [docs/setup_guide.md](docs/setup_guide.md)

---

## 📖 Usage Examples

### List All Instances

```python
from agent.core import run_agent_sync

run_agent_sync("List all EC2 instances and their status")
```

### Create Instances

```python
# Create specific instance
run_agent_sync("Create an EC2 instance named 'web-server-1' with 2 CPUs and 4GB RAM")

# Ensure minimum capacity
run_agent_sync("Ensure I have at least 3 running EC2 instances")
```

### Delete Instances

```python
run_agent_sync("Delete EC2 instance i-0abc123def456")
```

**More examples**: See [docs/usage_examples.md](docs/usage_examples.md)

---

## 📁 Project Structure

```
ai-agent-cloud/
├── agent/                      # AI Agent implementation
│   ├── core.py                 # Main agent loop (MCP client)
│   ├── mcp_client.py           # MCP client manager
│   └── state_manager.py        # State tracking & audit logs
├── mcp_servers/                # MCP Server implementations
│   ├── aws_server.py           # AWS EC2 operations (✅ implemented)
│   ├── azure_server.py         # Azure VMs (🚧 stub)
│   └── gcp_server.py           # GCP Compute (🚧 stub)
├── cloud_providers/            # Cloud SDK wrappers
│   ├── aws/
│   │   ├── ec2.py              # EC2Manager (boto3 wrapper)
│   │   └── mapping.py          # Resource mapping logic
│   ├── azure/                  # Future
│   └── gcp/                    # Future
├── docs/                       # Documentation
│   ├── architecture.md         # System design
│   ├── setup_guide.md          # Installation instructions
│   ├── usage_examples.md       # How to use
│   ├── state_management.md     # State tracking guide
│   └── CostOptimizationServiceGuide.md # ECS Fargate + EventBridge Scheduler cost optimization guide
├── config/
│   └── cost_optimization/
│       └── cost-optimization.worker.env # ECS environment file template for worker task
├── state/                      # State tracking (git-ignored)
│   ├── state.json              # Infrastructure snapshot
│   └── audit_log.jsonl         # Action history
├── main.py                     # Entry point
├── cost_optimization_worker.py     # Scheduled cost-optimization one-shot worker (ECS Fargate task)
├── view_state.py               # View state/statistics
├── sync_aws_state.py           # Sync from AWS
├── requirements.txt            # Dependencies
└── .env.example                # Configuration template
```

---

## 🔬 Research Questions (Thesis)

This project investigates:

1. **Can AI agents autonomously manage cloud infrastructure?**
   - Hypothesis: Yes, using MCP for tool access and LLMs for reasoning

2. **How does MCP compare to traditional approaches?**
   - Metrics: Code complexity, extensibility, maintainability

3. **What guardrails are needed for safe autonomous operation?**
   - Exploring: Policy engines, cost limits, security validation

4. **How scalable is multi-cloud agent architecture?**
   - Testing: Effort to add Azure/GCP, cross-cloud orchestration

---

## 🛠️ Technology Stack

| Component    | Technology                   | Purpose                     |
| ------------ | ---------------------------- | --------------------------- |
| AI Agent     | GPT-5.4-mini (OpenAI)        | Planning & reasoning        |
| Helper Agent | GPT-4.1-mini (OpenAI)        | Observability summarization |
| Protocol     | MCP (Model Context Protocol) | Tool communication          |
| AWS SDK      | Boto3                        | EC2 management              |
| Language     | Python 3.8+                  | Implementation              |
| Async        | asyncio                      | MCP communication           |

---

## 📊 How It Works

### 1. Agent Starts

```python
run_agent_sync("Ensure 2 running EC2 instances")
```

### 2. MCP Connection

- Agent spawns `aws_server.py` as subprocess
- Connects via stdio (standard input/output)
- Server exposes tools: `aws_create_ec2_instance`, `aws_list_ec2_instances`, etc.

### 3. Tool Discovery

```
Agent: "What tools do you have?"
Server: "I have 4 tools: list, create, delete, status"
Agent: *Registers tools with GPT-4o-mini*
```

### 4. AI Planning

```
GPT-4o-mini thinks:
  "User wants 2 instances. Let me first check current count."
  → Calls aws_list_ec2_instances

Result: 0 instances currently

GPT-4o-mini thinks:
  "Need to create 2. I'll call aws_create_ec2_instance twice."
  → Calls aws_create_ec2_instance (name: vm-1)
  → Calls aws_create_ec2_instance (name: vm-2)

GPT-4o-mini: "Done! You now have 2 instances running."
```

### 5. Cloud Execution

- MCP server receives `create_ec2_instance` call
- Calls `EC2Manager.create_instance()` → Boto3 → AWS API
- Returns result to agent
- Agent reports to user

---

## 🎓 Academic Contributions

1. **Novel MCP-based multi-cloud architecture**
2. **Practical autonomous agent implementation** (not just simulation)
3. **Comparative analysis** of MCP vs alternatives
4. **Safety framework** for autonomous cloud operations
5. **Extensibility study** - effort to add new cloud providers

---

## 🔐 Security & Safety

### Current Measures

- ✅ AWS credentials via environment variables (not hardcoded)
- ✅ All resources tagged for tracking (`ManagedBy: AIAgent`)
- ✅ Separate cloud provider layer (boto3 isolated from agent)
- ✅ Audit logging (track all actions in state/audit_log.jsonl)
- ✅ State persistence (infrastructure snapshots and action history)

### Planned (Future Work)

- 🚧 Policy engine (validate actions before execution)
- 🚧 Budget limits (prevent runaway costs)
- 🚧 Dry-run mode (test without real API calls)

---

## 💰 Cost Considerations

### AWS Pricing (us-east-1)

- t3.micro: $0.0104/hour (~$7.50/month) - **Free tier eligible**
- t3.medium: $0.0416/hour (~$30/month)
- t3.large: $0.0832/hour (~$60/month)

### Recommendations

1. Use **t3.micro** for testing (free tier)
2. Set up **AWS Budget Alerts** ($10-20 threshold)
3. Always **terminate instances** after testing
4. Use `ManagedBy: AIAgent` tag to find agent-created resources

**Cleanup command:**

```python
run_agent_sync("List all instances tagged 'ManagedBy: AIAgent' and delete them")
```

---

## 📚 Documentation

- **[Architecture](docs/architecture.md)** - System design and MCP explanation
- **[Setup Guide](docs/setup_guide.md)** - Installation and AWS configuration
- **[Usage Examples](docs/usage_examples.md)** - How to use the agent
- **[State Management](docs/state_management.md)** - State tracking and audit logs
- **[Code Walkthrough](docs/CODE_WALKTHROUGH.md)** - Comprehensive code explanation
- **[Cost Optimization Service Guide](docs/CostOptimizationServiceGuide.md)** - Deploy weekly cost optimization on ECS Fargate with EventBridge Scheduler
- **[Quick Reference](docs/QUICK_REFERENCE.md)** - Quick lookup guide

---

## 🙏 Acknowledgments

This project was made possible by:

- **[Model Context Protocol (MCP)](https://modelcontextprotocol.io/)** - For the foundational protocol enabling agent-tool communication
- **[Anthropic](https://www.anthropic.com/)** - For developing the MCP specification
- **[FastMCP](https://github.com/jlowin/fastmcp)** - Python framework that simplified MCP server implementation
- **[OpenAI](https://openai.com/)** - GPT-4o-mini API for agent reasoning capabilities
- **[AWS](https://aws.amazon.com/)** - Cloud infrastructure and generous free tier for development
- **[Boto3](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html)** - Python SDK for AWS integration

---

---

## 🎬 Quick Demo

```bash
# Install
pip install -r requirements.txt

# Configure (Windows)
copy .env.example .env
# Add your AWS and OpenAI credentials to .env

# Run
python main.py
```

**Output:**

```
🚀 Starting AI Agent with MCP...

🔗 Connecting to MCP servers...
✅ Connected to MCP server: aws

🔍 Discovering tools from MCP servers...
📦 Discovered 4 tools from aws

🎯 Agent Goal: Ensure at least 2 running instances

🔧 Calling Tool: aws_list_ec2_instances
📊 Tool Result: {"count": 0, "instances": []}

🔧 Calling Tool: aws_create_ec2_instance
   Arguments: {"name": "vm-1", "cpu": 2, "ram": 4}
✅ Created instance i-0abc123 with type t3.medium

🔧 Calling Tool: aws_create_ec2_instance
   Arguments: {"name": "vm-2", "cpu": 2, "ram": 4}
✅ Created instance i-0def456 with type t3.medium

🧠 Final Agent Response:
I've successfully ensured you have 2 running EC2 instances:
vm-1 and vm-2, both with 2 CPUs and 4GB RAM (t3.medium).
📊 Session Statistics:
   Total goals executed: 1
   Total resources created: 2
   Total resources deleted: 0

✨ Agent execution completed!
```

**View state after execution:**

```bash
python view_state.py
✨ Agent execution completed!
```

---

**Ready to revolutionize cloud infrastructure management? Let's go! 🚀**
