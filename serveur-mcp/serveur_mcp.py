#!/usr/bin/env python3
"""
Serveur MCP du testbed NWDAF (à lancer sur la VM du cœur).

MCP (Model Context Protocol) : un serveur publie une liste d'« outils » (nom, description, paramètres).
Un client (notre agent) peut lister ces outils, puis les appeler ; le LLM choisit lesquels utiliser
d'après leurs descriptions. Ici, tous les outils sont en LECTURE SEULE : ils observent le réseau.

Brique 2 : un seul outil, etat_reseau, qui lit Prometheus (où le NWDAF et la sonde publient leurs mesures).
Démarrage : python serveur_mcp.py  -> écoute sur http://0.0.0.0:8765/mcp
"""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request

from mcp.server.mcpserver import MCPServer

PROMETHEUS = os.environ.get("PROMETHEUS", "http://127.0.0.1:9090")

# 1. Le serveur, avec une consigne générale lue par le client
mcp = MCPServer(name="nwdaf-testbed",
                instructions="Outils d'observation du testbed 5G (free5GC + NWDAF de Daniel et al., 4 gNB, 4 UE).")


# 2. Petites fonctions pour interroger Prometheus
def instant_utc(instant: str | None) -> dt.datetime:
    """Texte ISO 8601 (ex. 2026-11-26T01:53:30Z) -> date UTC ; vide -> maintenant."""
    if not instant:
        return dt.datetime.now(dt.timezone.utc)
    t = dt.datetime.fromisoformat(instant.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def prometheus(requete: str, t: dt.datetime) -> list[tuple[dict, float]]:
    """Requête PromQL à l'instant t -> liste de (étiquettes, valeur)."""
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": requete, "time": t.timestamp()})
    with urllib.request.urlopen(url, timeout=20) as r:
        reponse = json.load(r)
    return [(s["metric"], float(s["value"][1])) for s in reponse["data"]["result"]]


# 3. Un outil = une fonction Python décorée par @mcp.tool().
#    Son nom, sa docstring et ses paramètres deviennent la « fiche » que le LLM lira.
@mcp.tool()
def etat_reseau(instant: str | None = None) -> str:
    """Photo du réseau 5G à un instant (UTC ISO 8601, ex. 2026-11-26T01:53:30Z ; vide = maintenant).
    Pour chaque UE : enregistré selon le NWDAF, enregistré selon l'UE, session PDU, TAC de la cellule,
    succès et latence du trafic HTTPS. Plus : nombre d'UE actifs selon le NWDAF et cibles injoignables."""
    t = instant_utc(instant)
    ues = {}
    mesures = {
        "enregistre_nwdaf": "max by (supi) (amf_ue_registration_state)",   # vue du NWDAF (signalisation)
        "enregistre_vue_ue": "max by (supi) (ue_enregistre)",              # vue de l'UE (sonde)
        "session_pdu": "max by (supi) (ue_session_pdu)",
        "tac": "max by (supi) (ue_tac)",
        "trafic_ok": "max by (supi) (ue_sonde_succes)",
        "latence_s": "max by (supi) (ue_sonde_latence_secondes)",
    }
    for cle, requete in mesures.items():
        for etiquettes, valeur in prometheus(requete, t):
            ues.setdefault(etiquettes.get("supi", "?"), {})[cle] = round(valeur, 3)
    actifs = prometheus("max(active_UEs)", t)
    injoignables = [e.get("instance") for e, v in prometheus("max by (instance) (up)", t) if v == 0]
    return json.dumps({
        "instant": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ues_actifs_selon_nwdaf": actifs[0][1] if actifs else None,
        "ue": dict(sorted(ues.items())),
        "cibles_injoignables": injoignables,
    }, ensure_ascii=False)


# 4. Lancement : transport HTTP « streamable », accessible depuis Windows
if __name__ == "__main__":
    print("Serveur MCP : http://0.0.0.0:8765/mcp", flush=True)
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8765)
