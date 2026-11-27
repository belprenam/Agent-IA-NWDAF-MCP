#!/usr/bin/env python3
"""
Agent de diagnostic du testbed 5G : LLM local (Ollama) + outils du serveur MCP.

La boucle de l'agent :
  1. on donne au LLM la question ET le « menu » des outils MCP (nom, description, paramètres) ;
  2. le LLM répond soit par du texte (réponse finale), soit par une demande d'outil (tool_calls) ;
  3. si c'est une demande d'outil, l'agent l'exécute sur le serveur MCP et renvoie le résultat au LLM ;
  4. on recommence jusqu'à la réponse finale (au plus MAX_ETAPES tours).
Le LLM ne touche jamais le réseau lui-même : il ne fait que demander ; c'est l'agent qui appelle les outils.

  python agent/agent.py "Que se passe-t-il sur le réseau ?"
  python agent/agent.py "Diagnostique le réseau" --instant 2026-11-26T01:53:30Z
  python agent/agent.py "..." --modele qwen2.5:3b
"""
import argparse
import asyncio
import json
import sys
import time

from mcp import Client

from ollama_local import discuter

MCP_URL = "http://192.168.56.40:8765/mcp"
MAX_ETAPES = 6

SYSTEME = """Tu es l'agent de diagnostic d'un réseau 5G de test (cœur free5GC, NWDAF, 4 gNB, 4 UE).
Tu réponds en français. Tu observes le réseau uniquement à l'aide des outils fournis :
appelle d'abord etat_reseau, puis raisonne sur les valeurs obtenues. N'invente aucune valeur.

Rappel : le NWDAF voit la signalisation (enregistrement, session) ; la sonde côté UE voit le trafic réel
(trafic_ok = 1 si une requête HTTPS passe). Comparer les deux vues aide à trouver la panne.

Réponse finale avec ces rubriques :
- Résumé : une phrase.
- Observations : les valeurs qui comptent.
- Cause probable et élément touché.
- Actions recommandées."""


def outil_pour_ollama(outil) -> dict:
    """Convertit la fiche d'un outil MCP au format attendu par Ollama.
    (On simplifie « anyOf [texte, null] » en « texte » : plus lisible pour un petit modèle.)"""
    proprietes = {}
    for nom, p in outil.input_schema.get("properties", {}).items():
        p = dict(p)
        types = [t for t in p.pop("anyOf", []) if t.get("type") != "null"]
        if types:
            p.update(types[0])
        p.pop("title", None)
        proprietes[nom] = p
    return {"type": "function", "function": {
        "name": outil.name, "description": outil.description or "",
        "parameters": {"type": "object", "properties": proprietes, "required": outil.input_schema.get("required", [])}}}


async def diagnostiquer(question: str, modele: str, instant: str | None = None) -> str:
    if instant:   # rejouer un moment passé : on l'indique au LLM et on l'impose à chaque outil
        question = f"Instant d'analyse : {instant} (UTC).\n{question}"
    messages = [{"role": "system", "content": SYSTEME}, {"role": "user", "content": question}]

    async with Client(MCP_URL) as client:
        outils = [outil_pour_ollama(o) for o in (await client.list_tools()).tools]
        print(f"Outils disponibles : {', '.join(o['function']['name'] for o in outils)}")

        for etape in range(1, MAX_ETAPES + 1):
            debut = time.time()
            message = discuter(modele, messages, outils)["message"]          # le LLM réfléchit
            appels = message.get("tool_calls") or []
            print(f"\n[tour {etape}] réponse du LLM en {time.time() - debut:.1f} s : "
                  f"{'demande ' + str(len(appels)) + ' outil(s)' if appels else 'réponse finale'}")
            messages.append(message)
            if not appels:
                return message.get("content", "")

            for appel in appels:                                              # l'agent exécute
                nom = appel["function"]["name"]
                arguments = appel["function"].get("arguments") or {}
                if isinstance(arguments, str):
                    arguments = json.loads(arguments or "{}")
                if instant:
                    arguments["instant"] = instant
                print(f"  -> outil {nom}({json.dumps(arguments, ensure_ascii=False)})")
                resultat = await client.call_tool(nom, arguments)
                texte = "\n".join(getattr(c, "text", "") for c in resultat.content)
                print(f"     résultat : {len(texte)} caractères renvoyés au LLM")
                messages.append({"role": "tool", "content": texte, "tool_name": nom})

        messages.append({"role": "user", "content": "Donne maintenant ta réponse finale."})
        return discuter(modele, messages)["message"].get("content", "")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question", nargs="+")
    p.add_argument("--instant", help="instant à analyser (UTC ISO, ex. 2026-11-26T01:53:30Z)")
    p.add_argument("--modele", default="qwen2.5:7b")
    a = p.parse_args()
    debut = time.time()
    reponse = asyncio.run(diagnostiquer(" ".join(a.question), a.modele, a.instant))
    print("\n" + "=" * 70 + "\n" + reponse + "\n" + "=" * 70)
    print(f"Durée totale : {time.time() - debut:.0f} s")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
