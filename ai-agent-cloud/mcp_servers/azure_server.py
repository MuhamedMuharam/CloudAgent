"""
MCP Azure Server (Stub - Future Implementation)

This server will expose Azure VM operations as MCP tools when implemented.

Architecture matches aws_server.py:
1. Import Azure VM manager from cloud_providers.azure
2. Define MCP tools for Azure operations
3. Implement tool handlers
4. Run MCP server on stdio

Tools to implement:
- azure_list_vms
- azure_create_vm
- azure_delete_vm
- azure_get_vm_status
- azure_manage_resource_group

Run with: python mcp_servers/azure_server.py
"""

import sys

print("=" * 60, file=sys.stderr)
print("⚠️  MCP Azure Server - Not Yet Implemented", file=sys.stderr)
print("=" * 60, file=sys.stderr)
print("", file=sys.stderr)
print("This server will provide Azure VM management via MCP.", file=sys.stderr)
print("", file=sys.stderr)
print("Implementation steps:", file=sys.stderr)
print("1. Install Azure SDK: pip install azure-identity azure-mgmt-compute", file=sys.stderr)
print("2. Create cloud_providers/azure/vm_manager.py", file=sys.stderr)
print("3. Implement MCP tool handlers (similar to aws_server.py)", file=sys.stderr)
print("4. Configure Azure credentials (Service Principal)", file=sys.stderr)
print("", file=sys.stderr)
print("=" * 60, file=sys.stderr)

sys.exit(1)
