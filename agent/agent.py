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
import re
import sys
import time

from mcp import Client

from ollama_local import discuter

MCP_URL = "http://192.168.56.40:8765/mcp"
MAX_ETAPES = 10
MAX_APPELS = 5               # budget d'appels d'outils ; ensuite, réponse finale obligatoire
TAILLE_MAX_RESULTAT = 3500   # caractères d'un résultat d'outil transmis au LLM (sa mémoire est limitée)

SYSTEME = """Tu es l'agent de diagnostic d'un réseau 5G de test (cœur free5GC, NWDAF, 4 gNB, 4 UE).
Tu réponds en français. Tu observes le réseau uniquement à l'aide des outils fournis. N'invente aucune valeur.

Méthode :
1. Photo : appelle etat_reseau. Repère ce qui s'écarte de la normale (UE non enregistrés, sessions manquantes,
   trafic en échec, latence élevée, cibles injoignables).
2. Compare les vues : le NWDAF voit la signalisation (enregistrement, session) ; la sonde côté UE voit le trafic
   réel (trafic_ok = 1 si une requête HTTPS passe par UE -> gNB -> UPF -> Internet).
3. Dynamique : si c'est anormal, appelle evenements_reseau et historique_indicateur pour savoir depuis quand,
   et si un seul UE ou tous sont touchés.
4. Confirmation : journaux_coeur (AMF pour les gNB et les enregistrements, SMF pour les sessions, UPF pour le
   plan usager) ou journaux_ran (un gNB ou un UE précis).
5. Conclus. Pas plus de 6 appels d'outils ; si tout est normal, dis-le.

Réponse finale avec ces rubriques :
- Résumé : une phrase.
- Observations : les valeurs et heures qui comptent.
- Cause probable et élément touché.
- Confiance : élevée, moyenne ou faible.
- Actions recommandées."""


def appels_ecrits_en_texte(texte: str, noms: set) -> list:
    """Rattrapage : un petit modèle écrit parfois sa demande d'outil dans le texte, par exemple
    {"name": "journaux_ran", "arguments": {...}}, au lieu du format prévu. On la reconnaît quand même."""
    appels = []
    for m in re.finditer(r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{[^{}]*\})\s*\}', texte or ""):
        if m.group(1) in noms:
            try:
                appels.append({"function": {"name": m.group(1), "arguments": json.loads(m.group(2))}})
            except json.JSONDecodeError:
                pass
    return appels


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

        noms = {o["function"]["name"] for o in outils}
        nb_appels = 0
        relances = 0
        for etape in range(1, MAX_ETAPES + 1):
            debut = time.time()
            if nb_appels >= MAX_APPELS:      # budget épuisé : plus d'outils, le modèle doit conclure
                messages.append({"role": "user", "content": "Tu as assez d'informations. Donne maintenant ta "
                                 "réponse finale avec les rubriques demandées, sans appeler d'outil."})
                return discuter(modele, messages)["message"].get("content", "")
            message = discuter(modele, messages, outils)["message"]          # le LLM réfléchit
            appels = message.get("tool_calls") or appels_ecrits_en_texte(message.get("content", ""), noms)
            if appels and not message.get("tool_calls"):
                print("  (demande d'outil écrite en texte : reconnue par le rattrapage)")
                message = {"role": "assistant", "content": "", "tool_calls": appels}
            print(f"\n[tour {etape}] réponse du LLM en {time.time() - debut:.1f} s : "
                  f"{'demande ' + str(len(appels)) + ' outil(s)' if appels else 'réponse finale'}")
            messages.append(message)
            if not appels:
                texte = (message.get("content") or "").strip()
                if nb_appels == 0 and relances < 2:   # répond sans avoir rien observé : on insiste
                    relances += 1
                    print("  (aucun outil appelé : on demande d'observer d'abord)")
                    messages.append({"role": "user", "content": "Observe d'abord le réseau : appelle etat_reseau."})
                    continue
                if "résumé" not in texte.lower() and relances < 2:   # réponse vide ou hors format
                    relances += 1
                    print(f"  (réponse {'vide' if not texte else 'sans les rubriques'} : on relance le modèle)")
                    messages.append({"role": "user", "content": "Ta réponse est vide ou incomplète. Si tu as besoin "
                                     "d'une information, appelle un outil ; sinon donne ta réponse finale avec les "
                                     "rubriques Résumé, Observations, Cause probable, Confiance, Actions recommandées."})
                    continue
                return texte

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
                if len(texte) > TAILLE_MAX_RESULTAT:
                    texte = texte[:TAILLE_MAX_RESULTAT] + " …(tronqué)"
                print(f"     résultat : {len(texte)} caractères renvoyés au LLM")
                messages.append({"role": "tool", "content": texte, "tool_name": nom})
                nb_appels += 1

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
