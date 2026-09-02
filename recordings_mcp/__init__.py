"""Recordings MCP — an internal, read-only MCP over the processed archive.

Not the PLAUD source MCP: nothing here talks to PLAUD, downloads audio or
writes anything. It serves ONE tenant's already-archived recordings to internal
bots, addressed by their permanent numbers (N-####, D-####, B-####).

  service name : recordings-mcp
  Russian label: MCP записей
"""

SERVICE_NAME = 'recordings-mcp'
SERVICE_LABEL_RU = 'MCP записей'
VERSION = '1.1.0'
PROTOCOL_VERSION = '2025-06-18'
