"""
Appels au LLM local (Ollama) par son API HTTP, avec la seule bibliothèque standard de Python.

Ollama tourne sur Windows et écoute sur http://127.0.0.1:11434. Deux points d'entrée nous servent :
  /api/chat   conversation : on envoie une liste de messages, le modèle répond (brique 3),
              et peut demander à appeler des outils (brique 4)
  /api/embed  plongements (vecteurs) de textes, pour le RAG (brique 6)
"""
import json
import os
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA

# Réglages du modèle : température basse = réponses plus stables et factuelles ;
# num_ctx = taille du « contexte » (mémoire de travail en tokens), assez grande pour les résultats d'outils.
OPTIONS = {"temperature": 0.1, "num_ctx": 12288}


def _post(chemin: str, corps: dict, timeout: float = 600) -> dict:
    requete = urllib.request.Request(f"{OLLAMA}{chemin}", data=json.dumps(corps).encode(),
                                     headers={"Content-Type": "application/json"})
    ouvreur = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # Ollama est local : pas de proxy
    with ouvreur.open(requete, timeout=timeout) as r:
        return json.load(r)


def discuter(modele: str, messages: list, outils: list | None = None) -> dict:
    """Envoie la conversation au modèle et renvoie la réponse complète d'Ollama.
    reponse["message"] contient le texte (content) et, éventuellement, les outils demandés (tool_calls).
    reponse["eval_count"] / ["eval_duration"] permettent de mesurer la vitesse."""
    corps = {"model": modele, "messages": messages, "stream": False, "options": OPTIONS}
    if outils:
        corps["tools"] = outils
    return _post("/api/chat", corps)


def vecteurs(modele: str, textes: list[str]) -> list[list[float]]:
    """Plongements (embeddings) des textes, pour le RAG."""
    return _post("/api/embed", {"model": modele, "input": textes}, timeout=300)["embeddings"]
