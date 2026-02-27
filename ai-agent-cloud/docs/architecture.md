# AI Agent Cloud Infrastructure - MCP Architecture

## 🎯 Project Overview

This project implements an **autonomous AI agent** that manages cloud infrastructure using the **Model Context Protocol (MCP)**. The agent can create, list, delete, and monitor cloud resources (EC2 instances, VMs) across multiple cloud providers without manual human intervention.

**Thesis Topic**: _Autonomous Configuration and Control for Cloud Infrastructure with AI Agents and MCP_

**Author**: Bachelor Thesis Project  
**Date**: February 2026

---

## 🏗️ System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ User Goal: "Ensure at least 2 running EC2 instances"        │
└─────────────────────┬───────────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ AI Agent (agent/core.py)                                    │
│  ├─ GPT-4 for planning & reasoning                          │
│  ├─ MCP Client Manager (mcp_client.py)                      │
│  └─ Tool orchestration & decision-making                    │
└────────┬──────────────────────────┬─────────────────────────┘
         │                          │
         ▼                          ▼
┌──────────────────┐    ┌──────────────────┐    ┌────────────┐
│ MCP AWS Server   │    │ MCP Azure Server │    │ MCP GCP    │
│ (aws_server.py)  │    │ (stub)           │    │ (stub)     │
│                  │    │                  │    │            │
│ ✅ Implemented   │    │ 🚧 Future        │    │ 🚧 Future  │
└────────┬─────────┘    └──────────────────┘    └────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Cloud Provider Layer (cloud_providers/aws/)              │
│  ├─ EC2Manager: boto3 wrapper for EC2 operations         │
│  └─ Mapping: CPU/RAM → AWS instance types                │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ AWS Cloud (Real Infrastructure)                          │
│  └─ EC2 Instances, VPCs, Security Groups, etc.           │
└──────────────────────────────────────────────────────────┘
```

---

## 📁 Project Structure

```
ai-agent-cloud/
├── agent/                          # AI Agent implementation
│   ├── __init__.py
│   ├── core.py                     # Main agent loop with OpenAI + MCP
│   └── mcp_client.py               # MCP client manager (multi-server)
│
├── mcp_servers/                    # MCP Server implementations
│   ├── aws_server.py               # ✅ AWS EC2 MCP server
│   ├── azure_server.py             # 🚧 Azure VM (stub)
│   └── gcp_server.py               # 🚧 GCP Compute (stub)
│
├── cloud_providers/                # Cloud SDK wrappers
│   ├── aws/
│   │   ├── __init__.py
│   │   ├── ec2.py                  # EC2Manager class (boto3)
│   │   └── mapping.py              # Generic specs → instance types
│   ├── azure/                      # 🚧 Future implementation
│   └── gcp/                        # 🚧 Future implementation
│
├── docs/                           # Documentation
│   ├── architecture.md             # This file
│   ├── setup_guide.md              # Installation & AWS setup
│   └── usage_examples.md           # How to use the agent
│
├── config/                         # Configuration files (future)
│
├── agent.py                        # 🔄 Legacy - original direct implementation
├── tools.py                        # 🔄 Legacy - original mock tools
├── main.py                         # Entry point for running agent
├── requirements.txt                # Python dependencies
├── .env.example                    # Environment variables template
└── README.md                       # Project README
```

---

## 🔑 Key Components

### 1. **AI Agent** (`agent/core.py`)

The autonomous decision-making component that:

- Receives high-level goals from users ("Create 2 web servers")
- Uses GPT-4 for planning and reasoning
- Discovers and calls tools via MCP protocol
- Iteratively executes actions until goal is achieved

**Key Feature**: No hardcoded tools! All tools are discovered dynamically from connected MCP servers.

### 2. **MCP Client Manager** (`agent/mcp_client.py`)

Manages connections to multiple MCP servers:

- Can connect to AWS, Azure, GCP servers simultaneously
- Discovers all available tools from each server
- Routes tool calls to the appropriate server
- Converts MCP tool definitions to OpenAI function format

**Multi-Cloud Support**: The agent doesn't need to know which cloud provider it's using - it just calls tools like `aws_create_ec2_instance` or `azure_create_vm`.

### 3. **MCP AWS Server** (`mcp_servers/aws_server.py`)

An MCP-compliant server that exposes AWS operations:

- **Protocol**: Communicates via stdio using MCP JSON-RPC
- **Tools Exposed**:
  - `aws_list_ec2_instances` - List all instances
  - `aws_create_ec2_instance` - Create new instance
  - `aws_delete_ec2_instance` - Terminate instance
  - `aws_get_ec2_instance_status` - Get instance details

**Runs as separate process**: The agent spawns the server and communicates via MCP protocol.

### 4. **Cloud Provider Layer** (`cloud_providers/aws/`)

Abstraction over cloud SDKs:

- **EC2Manager** (`ec2.py`): Boto3 wrapper with simplified interface
- **Mapping** (`mapping.py`): Translates generic specs (2 CPU, 4GB RAM) to AWS instance types (t3.medium)

**Why separate**: Keeps MCP server clean - it handles protocol, provider layer handles cloud APIs.

---

## 🔄 How MCP Works

### What is MCP?

**Model Context Protocol (MCP)** is a standardized way for AI applications to access external tools and data sources. Think of it as a "USB port for AI tools."

### Benefits of MCP

1. **Decoupling**: Agent doesn't need to know implementation details
2. **Multi-Server**: Connect to multiple tool providers simultaneously
3. **Standardized**: Same protocol works for any tool (cloud, databases, APIs)
4. **Language-Agnostic**: Servers can be written in any language

### MCP Flow in This Project

```
1. Agent starts → Spawns MCP AWS Server as subprocess
2. Agent connects via stdio (standard input/output)
3. Agent: "list_tools" → Server: "Here are my tools: aws_create_ec2_instance, ..."
4. Agent calls GPT-4 with discovered tools
5. GPT-4: "I should call aws_create_ec2_instance"
6. Agent: "call_tool(aws_create_ec2_instance, {name: 'server-1'})" → Server
7. Server executes boto3 call → Returns result
8. Agent receives result → Adds to conversation → Continues
```

---

## 🌐 Cloud-Agnostic Design

### Why One MCP Server Per Provider?

Each cloud provider (AWS, Azure, GCP) has unique:

- Authentication methods
- API structures
- Resource naming conventions
- Capabilities

**Solution**: Separate MCP server for each provider.

### Multi-Cloud Strategy

**Current (AWS only)**:

```python
await mcp_client.connect_to_server(
    'aws', 'python', ['mcp_servers/aws_server.py']
)
```

**Future (All three clouds)**:

```python
await mcp_client.connect_to_server('aws', 'python', ['mcp_servers/aws_server.py'])
await mcp_client.connect_to_server('azure', 'python', ['mcp_servers/azure_server.py'])
await mcp_client.connect_to_server('gcp', 'python', ['mcp_servers/gcp_server.py'])

# Agent discovers all tools from all servers!
# Can orchestrate: "Create 1 EC2 on AWS, 1 VM on Azure, 1 instance on GCP"
```

**Abstraction Layer (Optional)**: Could add `mcp_servers/unified_server.py` that provides cloud-agnostic tools (`create_vm(provider, ...)`) and internally routes to provider servers.

---

## 🔬 Research Areas for Thesis

### 1. **Self-Healing** (Future)

- Agent monitors instance health
- Detects failures (stopped/terminated instances)
- Automatically recreates failed resources
- **Evaluation**: Mean time to recovery (MTTR), success rate

### 2. **Cost Optimization** (Future)

- Analyzes resource utilization (CloudWatch metrics)
- Identifies idle/underutilized instances
- Autonomously downsizes or stops resources
- **Evaluation**: Cost savings, performance impact

### 3. **Security Guardrails** (Future)

- Policy engine validates actions before execution
- Prevents dangerous operations (open ports, delete production)
- Budget limits and cost estimation
- **Evaluation**: Prevented incidents, compliance rate

---

## 🔐 Security Considerations

### Current Implementation

- ✅ AWS credentials via environment variables (.env)
- ✅ Instances tagged with `ManagedBy: AIAgent` for tracking
- ⚠️ No policy enforcement yet
- ⚠️ No cost limits yet

### Future Additions

- Pre-execution policy checks
- Budget threshold enforcement
- Audit logging of all actions
- Dry-run mode for testing
- IAM role scope limitations

---

## 🚀 Advantages Over Traditional IaC

| Aspect            | Traditional IaC (Terraform) | AI Agent with MCP               |
| ----------------- | --------------------------- | ------------------------------- |
| **Configuration** | Static HCL/YAML files       | Natural language goals          |
| **Adaptation**    | Manual updates needed       | Autonomous decision-making      |
| **State Drift**   | Manual reconciliation       | Continuous monitoring & healing |
| **Multi-Cloud**   | Provider-specific code      | Unified agent, modular servers  |
| **Complexity**    | Learn DSL syntax            | Describe intent in English      |

---

## 📊 Evaluation Metrics (For Thesis)

1. **Correctness**: Does agent achieve stated goals? (% success rate)
2. **Efficiency**: How many LLM calls per task? (cost analysis)
3. **Safety**: Are guardrails effective? (prevented violations)
4. **Latency**: Time from goal → completion (seconds)
5. **Cost**: AWS spend vs manual provisioning
6. **Scalability**: Performance with 10, 100, 1000 resources
7. **Multi-Cloud**: Complexity of adding new providers (LOC, time)

---

## 🎓 Academic Contributions

1. **Novel Architecture**: MCP-based multi-cloud agent design
2. **Practical Implementation**: Real AWS integration (not just simulation)
3. **Safety Framework**: Autonomous operation with guardrails
4. **Comparative Analysis**: MCP vs alternatives (LangChain, direct LLM calls)
5. **Extensibility Study**: Effort required to add Azure/GCP
6. **Cost-Benefit Analysis**: AI-driven infra vs traditional IaC

---

## 🔮 Future Expansions

### Phase 1 (Current - Week 2)

- ✅ MCP architecture with AWS
- ✅ EC2 instance management
- ✅ Dynamic tool discovery

### Phase 2 (Weeks 3-4)

- Add Azure VM support
- Add GCP Compute support
- Multi-cloud orchestration tests

### Phase 3 (Weeks 5-8)

- Policy engine implementation
- Cost tracking & optimization
- Self-healing capabilities

### Phase 4 (Weeks 9-12)

- Advanced features (VPC, databases, IAM)
- Comprehensive testing
- Thesis writeup & evaluation

### Beyond Thesis

- Production-grade error handling
- Web UI for monitoring
- Support for more AWS services (RDS, Lambda, S3)
- Kubernetes cluster management
- GitOps integration

---

## 📚 References

- [Model Context Protocol Specification](https://modelcontextprotocol.io/)
- [AWS EC2 Documentation](https://docs.aws.amazon.com/ec2/)
- [Boto3 (AWS SDK for Python)](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html)
- [OpenAI Function Calling](https://platform.openai.com/docs/guides/function-calling)

---

**Last Updated**: February 26, 2026  
**Status**: Weeks 1-2 Complete - AWS MCP Integration Functional
