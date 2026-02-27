"""
MCP GCP Server (Stub - Future Implementation)

This server will expose GCP Compute Engine operations as MCP tools when implemented.

Architecture matches aws_server.py:
1. Import GCP Compute manager from cloud_providers.gcp
2. Define MCP tools for GCP operations
3. Implement tool handlers
4. Run MCP server on stdio

Tools to implement:
- gcp_list_instances
- gcp_create_instance
- gcp_delete_instance
- gcp_get_instance_status
- gcp_manage_zones

Run with: python mcp_servers/gcp_server.py
"""

import sys

print("=" * 60, file=sys.stderr)
print("⚠️  MCP GCP Server - Not Yet Implemented", file=sys.stderr)
print("=" * 60, file=sys.stderr)
print("", file=sys.stderr)
print("This server will provide GCP Compute Engine management via MCP.", file=sys.stderr)
print("", file=sys.stderr)
print("Implementation steps:", file=sys.stderr)
print("1. Install GCP SDK: pip install google-cloud-compute google-auth", file=sys.stderr)
print("2. Create cloud_providers/gcp/compute_manager.py", file=sys.stderr)
print("3. Implement MCP tool handlers (similar to aws_server.py)", file=sys.stderr)
print("4. Configure GCP credentials (Service Account JSON)", file=sys.stderr)
print("", file=sys.stderr)
print("=" * 60, file=sys.stderr)

sys.exit(1)
