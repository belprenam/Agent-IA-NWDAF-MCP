# Agent IA de diagnostic 5G relié au NWDAF par MCP

Projet SMART-ICT S5. Un agent d'IA générative observe un réseau 5G de test, détecte les pannes,
explique leur cause en langage naturel et recommande des actions. **Il ne modifie jamais le réseau.**

Contraintes : modèle de langage **local** (Ollama, aucune donnée envoyée à un service externe),
**RAG et prompting**, sans fine-tuning.

Le réseau observé est le testbed du dépôt
[Core5G-NWDAF-Daniel-Al](https://github.com/belprenam/Core5G-NWDAF-Daniel-Al) :
cœur free5GC v3.4 modifié, NWDAF de Daniel et al., Prometheus, 4 gNB et 4 UE UERANSIM, sur 3 VM.

## Architecture

```
PC Windows                                        VM du cœur 5G (192.168.56.40)
┌─────────────────────────────────┐               ┌──────────────────────────────────┐
│ agent/      l'agent             │               │ serveur-mcp/   le serveur MCP    │
│   └─ Ollama : LLM local         │  MCP (HTTP)   │   ├─ NWDAF + Prometheus          │
│ rag/        base de             │ ────────────► │   ├─ journaux du cœur free5GC    │
│             connaissances 5G    │               │   └─ journaux des gNB et UE      │
│ evaluation/ mesure des résultats│               │                                  │
└─────────────────────────────────┘               └──────────────────────────────────┘
```

- **Ollama** fait tourner le modèle de langage (LLM) sur le PC.
- **MCP** (Model Context Protocol) est un protocole standard qui permet au LLM d'appeler des **outils** :
  le serveur MCP, installé sur la VM du cœur, propose des outils en lecture seule pour observer le réseau.
- **L'agent** est la boucle qui relie les deux : il pose la question au LLM, exécute les outils que le LLM
  demande, lui renvoie les résultats, jusqu'au diagnostic final.
- **Le RAG** (Retrieval-Augmented Generation) ajoute à la question les passages utiles d'une base de
  connaissances (procédures 5G, NWDAF, signatures des pannes) : c'est ainsi qu'on donne du savoir au LLM
  sans le réentraîner.

## Structure du dépôt

| Dossier | Contenu | Où ça tourne |
| --- | --- | --- |
| `serveur-mcp/` | Serveur MCP et ses outils d'observation | VM du cœur |
| `agent/` | Appels au LLM local et boucle de l'agent | Windows |
| `rag/` | Base de connaissances et recherche des passages utiles | Windows |
| `evaluation/` | Évaluation de l'agent sur les pannes étiquetées | Windows |
| `resultats/` | Résultats des évaluations (tableaux, réponses) | – |
| `docs/` | Notes, schémas, extraits pour le rapport | – |

## Avancement

| Brique | Contenu | État |
| --- | --- | --- |
| 1 | Structure du dépôt, environnement Python | fait |
| 2 | Serveur MCP minimal (outil `etat_reseau`) | en cours |
| 3 | Appel au LLM local (Ollama) | à faire |
| 4 | Agent minimal : LLM + outil MCP | à faire |
| 5 | Outils d'enquête : historique, événements, journaux | à faire |
| 6 | RAG : base de connaissances 5G / NWDAF | à faire |
| 7 | Évaluation sur les campagnes de pannes | à faire |

## Installation (Windows)

1. Python 3.10 ou plus récent, et [Ollama](https://ollama.com/).
2. Dans PowerShell :
   ```powershell
   git clone https://github.com/belprenam/Agent-IA-NWDAF-MCP
   cd Agent-IA-NWDAF-MCP
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   Si `python` ouvre le Microsoft Store, utiliser `py -m venv .venv` (lanceur Python de Windows).

## Serveur MCP (VM du cœur)

```bash
git clone https://github.com/belprenam/Agent-IA-NWDAF-MCP ~/Agent-IA-NWDAF-MCP   # ou git pull
bash ~/Agent-IA-NWDAF-MCP/serveur-mcp/installer.sh        # une seule fois
bash ~/Agent-IA-NWDAF-MCP/serveur-mcp/demarrer.sh         # --redemarrer après une mise à jour
```
Test depuis Windows : `python agent/tester_mcp.py` (liste les outils et appelle `etat_reseau`).
