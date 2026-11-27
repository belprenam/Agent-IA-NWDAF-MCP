#!/usr/bin/env python3
"""
Test de la brique 3 : poser une question au LLM local et mesurer sa vitesse.
Aucun outil ici : le modèle ne répond qu'avec ce qu'il sait déjà.

  python agent/tester_llm.py                    (qwen2.5:7b)
  python agent/tester_llm.py qwen2.5:3b
"""
import sys
import time

from ollama_local import discuter

QUESTION = ("Dans un réseau 5G, tous les UE sont enregistrés et ont une session PDU active, "
            "mais aucun trafic de données ne passe. Quelle est la cause la plus probable ? Réponds en 3 phrases.")


def main():
    modele = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5:7b"
    messages = [
        {"role": "system", "content": "Tu es un expert des réseaux 5G. Réponds en français, de façon précise."},
        {"role": "user", "content": QUESTION},
    ]
    print(f"Modèle : {modele}\nQuestion : {QUESTION}\n")
    debut = time.time()
    reponse = discuter(modele, messages)
    duree = time.time() - debut
    print("Réponse :\n" + reponse["message"]["content"])
    tokens = reponse.get("eval_count", 0)
    generation_s = reponse.get("eval_duration", 0) / 1e9
    print(f"\nDurée totale : {duree:.1f} s (dont chargement du modèle : {reponse.get('load_duration', 0) / 1e9:.1f} s)")
    if generation_s:
        print(f"Vitesse : {tokens} tokens en {generation_s:.1f} s = {tokens / generation_s:.1f} tokens/s")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
