#!/usr/bin/env bash
# Installe le serveur MCP sur la VM du cœur (une seule fois) : environnement Python isolé + SDK MCP.
set -e
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip
python3 -m venv ~/venv-mcp
~/venv-mcp/bin/pip install -q --upgrade pip
~/venv-mcp/bin/pip install -q "mcp>=2.3,<3"
~/venv-mcp/bin/python -c "from mcp.server.mcpserver import MCPServer; import sys; print('SDK MCP installé, Python', sys.version.split()[0])"
