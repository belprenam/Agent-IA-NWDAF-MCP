#!/usr/bin/env bash
# Lance le serveur MCP en arrière-plan (ou le relance avec --redemarrer). Journal : ~/campagne-logs/mcp.log
ICI="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/campagne-logs
if [ "$1" = "--redemarrer" ]; then pkill -f "serveur-mcp/serveur_mcp.py"; sleep 2; fi
if pgrep -f "serveur-mcp/serveur_mcp.py" > /dev/null; then
  echo "Serveur MCP : déjà lancé."
else
  (setsid nohup ~/venv-mcp/bin/python "$ICI/serveur_mcp.py" >> ~/campagne-logs/mcp.log 2>&1 < /dev/null &)
  sleep 4
fi
if ss -ltn | grep -q ":8765 "; then
  echo "Serveur MCP : à l'écoute sur http://192.168.56.40:8765/mcp"
else
  echo "Le serveur MCP ne démarre pas. Fin du journal :"; tail -n 20 ~/campagne-logs/mcp.log
fi
