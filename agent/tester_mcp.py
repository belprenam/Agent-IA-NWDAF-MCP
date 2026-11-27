#!/usr/bin/env python3
"""
Test de la brique 2, depuis Windows : se connecter au serveur MCP du cœur, lister ses outils,
puis appeler etat_reseau. Aucun LLM ici : on fait à la main ce que l'agent fera automatiquement.

  python agent/tester_mcp.py
  python agent/tester_mcp.py 2026-11-26T01:53:30Z     (rejoue un instant passé)
"""
import asyncio
import json
import sys

from mcp import Client

URL = "http://192.168.56.40:8765/mcp"


async def main():
    async with Client(URL) as client:                       # 1. connexion au serveur MCP
        outils = (await client.list_tools()).tools           # 2. le « menu » des outils
        for o in outils:
            print(f"Outil : {o.name}\n  Description : {o.description}\n  Paramètres : "
                  f"{json.dumps(o.input_schema.get('properties', {}), ensure_ascii=False)}\n")
        arguments = {"instant": sys.argv[1]} if len(sys.argv) > 1 else {}
        resultat = await client.call_tool("etat_reseau", arguments)   # 3. appel d'un outil
        texte = resultat.content[0].text
        print("Résultat de etat_reseau :")
        print(json.dumps(json.loads(texte), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
