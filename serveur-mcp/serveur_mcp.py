#!/usr/bin/env python3
"""
Serveur MCP du testbed NWDAF de Daniel et al. (à lancer sur la VM du cœur).

Il donne à un agent IA des outils en lecture seule pour observer le réseau 5G :
  etat_reseau            photo du réseau à un instant (UE, sessions, sonde, cibles, VM)
  historique_indicateur  évolution d'un indicateur sur une fenêtre (résumé + points)
  evenements_reseau      événements reçus par le NWDAF sur une fenêtre (enregistrements, sessions, cellules)
  journaux_coeur         avertissements et erreurs des fonctions du cœur (AMF, SMF, UPF…), dédoublonnés
  journaux_ran           erreurs des gNB et des UE (VM daniel-gnb et daniel-ue, par SSH)
  requete_promql         requête PromQL libre (instantanée), pour l'expert
Chaque outil accepte un paramètre « instant » (UTC ISO 8601) pour rejouer un moment passé ;
sans lui, c'est maintenant. Aucun outil ne modifie le réseau.

Démarrage : bash serveur-mcp/demarrer.sh --redemarrer   (écoute sur 0.0.0.0:8765, chemin /mcp)

Brique 2 : etat_reseau. Brique 5 : les outils d'enquête (historique, événements, journaux, PromQL).
"""
import datetime as dt
import functools
import glob
import json
import os
import re
import statistics
import subprocess
import urllib.parse
import urllib.request

from mcp.server.mcpserver import MCPServer

PROMETHEUS = os.environ.get("PROMETHEUS", "http://127.0.0.1:9090")
LOGS_FREE5GC = os.path.expanduser(os.environ.get("LOGS_FREE5GC", "~/5g-testbed-conference/free5gc/log"))
UTILISATEUR = os.environ.get("USER", "user")
GNB_SSH = os.environ.get("GNB_SSH", f"{UTILISATEUR}@192.168.56.41")
UE_SSH = os.environ.get("UE_SSH", f"{UTILISATEUR}@192.168.56.42")
HOTE = os.environ.get("MCP_HOTE", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8765"))

INSTRUCTIONS = (
    "Outils d'observation du testbed 5G (Free5GC v3.4 modifié + NWDAF de Daniel et al., 4 gNB, 4 UE). "
    "Commencer par etat_reseau, puis evenements_reseau et historique_indicateur pour voir l'évolution, "
    "et journaux_coeur / journaux_ran pour confirmer une cause. Les heures sont en UTC."
)
mcp = MCPServer(name="nwdaf-testbed", instructions=INSTRUCTIONS)

# Indicateurs nommés : nom -> (PromQL, description)
INDICATEURS = {
    "ues_actifs_nwdaf": ("max(active_UEs)", "UE enregistrés selon l'AMF (vu par le NWDAF)"),
    "ues_enregistres_nwdaf": ("sum(amf_ue_registration_state)", "Somme des états d'enregistrement par UE (NWDAF)"),
    "enregistrement_par_ue_nwdaf": ("max by (supi) (amf_ue_registration_state)", "État d'enregistrement de chaque UE (NWDAF)"),
    "evts_enregistrement_1m": ("sum by (state) (increase(amf_registration_state_events_total[1m]))",
                               "Événements d'enregistrement / désenregistrement par minute"),
    "sessions_pdu_actives": ("sum(active_pdu_sessions)", "Sessions PDU actives selon le SMF (NWDAF)"),
    "evts_pdu_1m": ("sum by (Est_or_Rel) (increase(total_pdu_session_events[1m]))", "Établissements / libérations de sessions PDU par minute"),
    "rapports_localisation_1m": ("count(UE_location_report) - count(UE_location_report offset 1m)",
                                 "Nouveaux rapports de localisation (changements de cellule) par minute"),
    "taux_succes_trafic": ("avg(ue_sonde_succes)", "Part des UE dont la requête HTTPS passe (plan usager)"),
    "succes_trafic_par_ue": ("max by (supi) (ue_sonde_succes)", "Requête HTTPS réussie par UE (1/0)"),
    "latence_trafic_s": ("avg(ue_sonde_latence_secondes)", "Latence HTTPS moyenne des requêtes réussies (s)"),
    "latence_par_ue_s": ("max by (supi) (ue_sonde_latence_secondes)", "Latence HTTPS par UE (s)"),
    "ues_enregistres_vue_ue": ("sum(ue_enregistre)", "UE qui se voient enregistrés (vue des UE)"),
    "session_pdu_par_ue": ("max by (supi) (ue_session_pdu)", "Session PDU avec interface active, par UE"),
    "tac_par_ue": ("max by (supi) (ue_tac)", "TAC de la cellule courante par UE (gnb5=1, gnb4=2, gnb3=3, gnb2=4)"),
    "cibles_joignables": ("max by (instance) (up)", "Cible Prometheus joignable (1/0) : NWDAF, UE, sonde, VM"),
    "cpu_vm": ("1 - avg by (vm) (rate(node_cpu_seconds_total{mode=\"idle\"}[1m]))", "CPU utilisé par VM (0-1)"),
    "memoire_vm": ("1 - max by (vm) (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)", "Mémoire utilisée par VM (0-1)"),
    "debit_rx_vm": ("sum by (vm) (rate(node_network_receive_bytes_total{device=\"enp0s8\"}[1m]))", "Débit reçu sur le réseau 5G interne (o/s)"),
    "debit_tx_vm": ("sum by (vm) (rate(node_network_transmit_bytes_total{device=\"enp0s8\"}[1m]))", "Débit émis sur le réseau 5G interne (o/s)"),
}
NIVEAUX = {"trace": 0, "debug": 1, "info": 2, "warn": 3, "warning": 3, "error": 4, "erro": 4, "fatal": 5, "panic": 6}


# --- utilitaires ---------------------------------------------------------------------------
def _instant(instant: str | None) -> dt.datetime:
    if not instant:
        return dt.datetime.now(dt.timezone.utc)
    t = dt.datetime.fromisoformat(instant.strip().replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prom(chemin: str, params: dict) -> dict | list:
    url = f"{PROMETHEUS}{chemin}?{urllib.parse.urlencode(params, doseq=True)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        rep = json.load(r)
    if rep.get("status") != "success":
        raise RuntimeError(rep.get("error", "erreur Prometheus"))
    return rep["data"]


def _instantane(requete: str, t: dt.datetime) -> list[tuple[dict, float]]:
    data = _prom("/api/v1/query", {"query": requete, "time": t.timestamp()})
    if data["resultType"] == "scalar":
        return [({}, float(data["result"][1]))]
    return [(s["metric"], float(s["value"][1])) for s in data["result"]]


def _etiquette(labels: dict) -> str:
    labels = {k: v for k, v in labels.items() if k != "__name__"}
    return ",".join(f"{v}" for _, v in sorted(labels.items())) or "total"


def _arrondi(v: float) -> float:
    return round(v, 3) if abs(v) < 1000 else round(v)


def outil(fn):
    """Enregistre un outil MCP ; une erreur est renvoyée à l'agent en JSON lisible au lieu d'un échec opaque."""
    @functools.wraps(fn)
    def enveloppe(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"erreur": f"{type(e).__name__} : {e}"}, ensure_ascii=False)
    return mcp.tool()(enveloppe)


# --- outils ----------------------------------------------------------------------------------
@outil
def etat_reseau(instant: str | None = None) -> str:
    """Photo du réseau 5G à un instant (UTC ISO 8601, ex. 2026-11-25T09:31:30Z ; vide = maintenant).
    Pour chaque UE : enregistré selon le NWDAF, enregistré selon l'UE, session PDU, TAC de la cellule,
    succès et latence du trafic HTTPS. Plus : totaux NWDAF, cibles injoignables, CPU/mémoire des VM."""
    t = _instant(instant)
    ues: dict[str, dict] = {}
    for nom, cle in [("enregistrement_par_ue_nwdaf", "enregistre_nwdaf"), ("session_pdu_par_ue", "session_pdu"),
                     ("tac_par_ue", "tac"), ("succes_trafic_par_ue", "trafic_ok"), ("latence_par_ue_s", "latence_s")]:
        for labels, v in _instantane(INDICATEURS[nom][0], t):
            if "supi" in labels:
                ues.setdefault(labels["supi"], {})[cle] = _arrondi(v)
    for labels, v in _instantane("max by (supi) (ue_enregistre)", t):
        ues.setdefault(labels["supi"], {})["enregistre_vue_ue"] = int(v)
    for labels, v in _instantane("max by (supi) (ue_processus_actif)", t):
        ues.setdefault(labels["supi"], {})["processus_ue_actif"] = int(v)
    totaux = {}
    for nom in ["ues_actifs_nwdaf", "sessions_pdu_actives", "taux_succes_trafic", "latence_trafic_s", "ues_enregistres_vue_ue"]:
        r = _instantane(INDICATEURS[nom][0], t)
        totaux[nom] = _arrondi(r[0][1]) if r else None
    injoignables = [l.get("instance", "?") for l, v in _instantane("max by (instance) (up)", t) if v == 0]
    vm = {}
    for nom, cle in [("cpu_vm", "cpu"), ("memoire_vm", "memoire")]:
        for labels, v in _instantane(INDICATEURS[nom][0], t):
            vm.setdefault(labels.get("vm", "?"), {})[cle] = round(v, 2)
    return json.dumps({"instant": _iso(t), "totaux": totaux, "ue": dict(sorted(ues.items())),
                       "cibles_injoignables": injoignables, "vm": vm}, ensure_ascii=False)


@outil
def historique_indicateur(indicateur: str, fenetre_minutes: float = 15, instant: str | None = None) -> str:
    """Évolution d'un indicateur sur les « fenetre_minutes » précédant « instant » (UTC ISO ; vide = maintenant).
    Indicateurs : ues_actifs_nwdaf, ues_enregistres_nwdaf, enregistrement_par_ue_nwdaf, evts_enregistrement_1m,
    sessions_pdu_actives, evts_pdu_1m, rapports_localisation_1m, taux_succes_trafic, succes_trafic_par_ue,
    latence_trafic_s, latence_par_ue_s, ues_enregistres_vue_ue, session_pdu_par_ue, tac_par_ue,
    cibles_joignables, cpu_vm, memoire_vm, debit_rx_vm, debit_tx_vm.
    Renvoie pour chaque série : min, max, moyenne, première et dernière valeur, et ~12 points."""
    if indicateur not in INDICATEURS:
        return json.dumps({"erreur": f"indicateur inconnu : {indicateur}", "disponibles": list(INDICATEURS)}, ensure_ascii=False)
    fin = _instant(instant)
    fenetre = max(1.0, min(float(fenetre_minutes), 360.0))
    debut = fin - dt.timedelta(minutes=fenetre)
    pas = max(5, int(fenetre * 60 / 60))
    data = _prom("/api/v1/query_range", {"query": INDICATEURS[indicateur][0], "start": debut.timestamp(),
                                         "end": fin.timestamp(), "step": pas})
    series = {}
    for s in data["result"]:
        points = [(float(ts), float(v)) for ts, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")]
        if not points:
            continue
        valeurs = [v for _, v in points]
        echant = points[:: max(1, len(points) // 12)]
        series[_etiquette(s["metric"])] = {
            "min": _arrondi(min(valeurs)), "max": _arrondi(max(valeurs)), "moyenne": _arrondi(statistics.mean(valeurs)),
            "premiere": _arrondi(valeurs[0]), "derniere": _arrondi(valeurs[-1]),
            "points": [[dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%M:%S"), _arrondi(v)] for ts, v in echant],
        }
    return json.dumps({"indicateur": indicateur, "description": INDICATEURS[indicateur][1],
                       "debut": _iso(debut), "fin": _iso(fin), "series": series or "aucune donnée"}, ensure_ascii=False)


@outil
def evenements_reseau(fenetre_minutes: float = 10, instant: str | None = None) -> str:
    """Événements reçus par le NWDAF depuis l'AMF et le SMF pendant les « fenetre_minutes » précédant
    « instant » (UTC ISO ; vide = maintenant) : enregistrements / désenregistrements par UE,
    établissements / libérations de sessions PDU, et rapports de localisation (changements de cellule)."""
    fin = _instant(instant)
    fenetre = max(1.0, min(float(fenetre_minutes), 360.0))
    debut = fin - dt.timedelta(minutes=fenetre)
    duree = f"{int(fenetre * 60)}s"
    enreg = {}
    for labels, v in _instantane(f"sum by (supi, state) (increase(amf_registration_state_events_total[{duree}]))", fin):
        if v >= 0.5:
            enreg.setdefault(labels.get("supi", "?"), {})[labels.get("state", "?")] = round(v)
    pdu = {}
    for labels, v in _instantane(f"sum by (Est_or_Rel) (increase(total_pdu_session_events[{duree}]))", fin):
        if v >= 0.5:
            pdu[labels.get("Est_or_Rel", "?")] = round(v)
    localisations = []
    try:
        series = _prom("/api/v1/series", {"match[]": "UE_location_report", "start": debut.timestamp(), "end": fin.timestamp()})
        for s in series:
            m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", s.get("time", ""))
            if m:
                t = dt.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}+00:00")
                if debut <= t <= fin:
                    localisations.append((t, s.get("supi", "?"), s.get("tac", "?"), s.get("NrCellId", "?")))
    except Exception as e:
        localisations = [(fin, "erreur", str(e), "")]
    localisations.sort()
    return json.dumps({
        "debut": _iso(debut), "fin": _iso(fin),
        "enregistrements_par_ue": enreg or "aucun",
        "sessions_pdu": pdu or "aucune",
        "rapports_localisation": [{"heure": t.strftime("%H:%M:%S"), "supi": s, "tac": tac, "cellule": c}
                                  for t, s, tac, c in localisations[-30:]] or "aucun",
    }, ensure_ascii=False)


# --- journaux du cœur ---------------------------------------------------------------------------
LIGNE_F5GC = re.compile(r'time="([^"]+)" level="(\w+)" msg="((?:[^"\\]|\\.)*)"(.*)')


def _date_ligne(ligne: str) -> dt.datetime | None:
    m = re.match(r'time="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', ligne)
    return dt.datetime.fromisoformat(m.group(1) + "+00:00") if m else None


def _position(f, debut: dt.datetime, taille: int) -> int:
    """Recherche dichotomique de la première ligne >= debut (le journal est trié par heure)."""
    bas, haut = 0, taille
    while haut - bas > 65536:
        milieu = (bas + haut) // 2
        f.seek(milieu)
        f.readline()
        t = None
        for _ in range(50):
            ligne = f.readline()
            if not ligne:
                break
            t = _date_ligne(ligne.decode(errors="replace"))
            if t:
                break
        if t is None or t >= debut:
            haut = milieu
        else:
            bas = milieu
    return bas


@outil
def journaux_coeur(fenetre_minutes: float = 5, instant: str | None = None, fonction: str | None = None,
                   niveau_min: str = "warning", motif: str | None = None, max_lignes: int = 40) -> str:
    """Messages des journaux de Free5GC (AMF, SMF, UPF, PCF, UDM, UDR, AUSF, NRF, CHF…) pendant les
    « fenetre_minutes » précédant « instant » (UTC ISO ; vide = maintenant).
    fonction : filtre sur une fonction (ex. « SMF ») ; niveau_min : info, warning ou error ;
    motif : texte ou expression régulière à chercher dans le message.
    Les messages identiques sont regroupés avec leur nombre d'occurrences et leur première heure."""
    fin = _instant(instant)
    debut = fin - dt.timedelta(minutes=max(0.5, min(float(fenetre_minutes), 120.0)))
    seuil = NIVEAUX.get(niveau_min.lower(), 3)
    regex = re.compile(motif, re.I) if motif else None
    groupes: dict[tuple, dict] = {}
    fichiers = sorted(glob.glob(os.path.join(LOGS_FREE5GC, "*", "free5gc.log")))
    lus = 0
    for chemin in fichiers:
        if os.path.getmtime(chemin) < debut.timestamp():
            continue
        taille = os.path.getsize(chemin)
        with open(chemin, "rb") as f:
            f.seek(_position(f, debut, taille))
            for brut in f:
                ligne = re.sub(r"\x1b\[[0-9;]*m", "", brut.decode(errors="replace"))
                m = LIGNE_F5GC.match(ligne)
                if not m:
                    continue
                t = dt.datetime.fromisoformat(m.group(1)[:19] + "+00:00")
                if t > fin:
                    break
                if t < debut:
                    continue
                lus += 1
                niveau = m.group(2).lower()
                if NIVEAUX.get(niveau, 2) < seuil:
                    continue
                nf = (re.search(r'NF="(\w+)"', m.group(4)) or [None, "?"])[1]
                if fonction and nf.upper() != fonction.upper():
                    continue
                message = m.group(3).replace("\\n", " ").replace('\\"', '"')
                if regex and not regex.search(message):
                    continue
                cle = (nf, niveau, re.sub(r"\d+", "#", message[:160]))
                g = groupes.setdefault(cle, {"premiere": t.strftime("%H:%M:%S"), "nombre": 0,
                                             "fonction": nf, "niveau": niveau, "message": message[:300]})
                g["nombre"] += 1
                g["derniere"] = t.strftime("%H:%M:%S")
    resultats = sorted(groupes.values(), key=lambda g: g["premiere"])
    return json.dumps({"debut": _iso(debut), "fin": _iso(fin), "lignes_lues": lus,
                       "messages": resultats[:max(1, min(int(max_lignes), 100))] or "aucun message à ce niveau",
                       "tronque": len(resultats) > max_lignes}, ensure_ascii=False)


# --- journaux des gNB et des UE -------------------------------------------------------------------
@outil
def journaux_ran(element: str, fenetre_minutes: float = 5, instant: str | None = None,
                 niveau_min: str = "warning", max_lignes: int = 30) -> str:
    """Messages des journaux d'un gNB (gnb2, gnb3, gnb4, gnb5) ou d'un UE (ue1, ue2, ue3, ue4) de UERANSIM,
    pendant les « fenetre_minutes » précédant « instant » (UTC ISO ; vide = maintenant).
    niveau_min : info, warning ou error. Les messages identiques sont regroupés."""
    element = element.strip().lower()
    if not re.fullmatch(r"gnb[2-5]|ue[1-9]", element):
        return json.dumps({"erreur": "element doit être gnb2..gnb5 ou ue1..ue4"}, ensure_ascii=False)
    hote = GNB_SSH if element.startswith("gnb") else UE_SSH
    fin = _instant(instant)
    debut = fin - dt.timedelta(minutes=max(0.5, min(float(fenetre_minutes), 120.0)))
    niveaux = {"info": "info|warning|error", "warning": "warning|error", "error": "error"}.get(niveau_min.lower(), "warning|error")
    jour = debut.strftime("%Y-%m-%d")
    commande = f"grep -E '^\\[{jour}.*\\] \\[[a-z0-9-]+\\] \\[({niveaux})\\]' ~/campagne-logs/{element}.log | tail -n 20000"
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", hote, commande],
                           capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        return json.dumps({"erreur": f"{hote} injoignable : {e}"}, ensure_ascii=False)
    groupes: dict[tuple, dict] = {}
    for ligne in r.stdout.splitlines():
        m = re.match(r"\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})[^\]]*\] \[([^\]]+)\] \[(\w+)\] (.*)", ligne)
        if not m:
            continue
        t = dt.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}+00:00")
        if not debut <= t <= fin:
            continue
        cle = (m.group(3), m.group(4), re.sub(r"\d+", "#", m.group(5)[:160]))
        g = groupes.setdefault(cle, {"premiere": m.group(2), "nombre": 0, "couche": m.group(3),
                                     "niveau": m.group(4), "message": m.group(5)[:250]})
        g["nombre"] += 1
        g["derniere"] = m.group(2)
    resultats = sorted(groupes.values(), key=lambda g: g["premiere"])
    return json.dumps({"element": element, "debut": _iso(debut), "fin": _iso(fin),
                       "messages": resultats[:max(1, min(int(max_lignes), 100))] or "aucun message à ce niveau",
                       "journal_absent": r.returncode == 2}, ensure_ascii=False)


@outil
def requete_promql(requete: str, instant: str | None = None) -> str:
    """Requête PromQL instantanée libre sur Prometheus, à l'instant donné (UTC ISO ; vide = maintenant).
    Métriques disponibles : active_UEs, amf_ue_registration_state{supi}, amf_registration_state_events_total{supi,state},
    active_pdu_sessions, total_pdu_session_events{Est_or_Rel}, UE_location_report{supi,tac,NrCellId,time},
    ue_enregistre/ue_cm_connecte/ue_tac/ue_session_pdu/ue_sonde_succes/ue_sonde_latence_secondes{supi},
    up{job,instance}, node_* (node-exporter, label vm). Résultat limité à 40 séries."""
    t = _instant(instant)
    try:
        r = _instantane(requete, t)
    except Exception as e:
        return json.dumps({"erreur": str(e)}, ensure_ascii=False)
    return json.dumps({"instant": _iso(t), "nombre_series": len(r),
                       "resultat": [{"labels": {k: v for k, v in l.items() if k != "__name__"}, "valeur": _arrondi(v)}
                                    for l, v in r[:40]]}, ensure_ascii=False)


if __name__ == "__main__":
    print(f"Serveur MCP NWDAF : http://{HOTE}:{PORT}/mcp (Prometheus {PROMETHEUS})", flush=True)
    mcp.run(transport="streamable-http", host=HOTE, port=PORT)
