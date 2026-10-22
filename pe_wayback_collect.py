#!/usr/bin/env python3
"""Collecte des assistants des MPE francais via le Wayback Machine.

Sources :
  - Liste des MPE : referentiel public du projet (parlementaires.csv, chambre
    europarl, actif=1). Repli : affectations.csv, puis API v2 du PE.
  - Pages assistants : snapshots du Wayback Machine via CDX (prefixe par MPE,
    filtre original:.*assistants.*). Aucune requete vers europarl.europa.eu.
    SPN anonyme abandonne (HTTP 523, compte requis).

Wayback rate-limitant (429) : chaque requete est retryee avec backoff.

Sortie : data/europarl_assistants.csv + bilan dans une issue GitHub.
"""

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

LIMITE = int(os.environ.get("LIMITE", "84"))
PAUSE_GET = 12
TIMEOUT = 60
FRAIS_MAX_H = int(os.environ.get("FRAIS_MAX_H", "168"))

RAW_PARLEMENTAIRES = ("https://raw.githubusercontent.com/publicdatanetwork-dataparl/"
                      "collaborateurs/main/data/referentiels/parlementaires.csv")
RAW_AFFECTATIONS = ("https://raw.githubusercontent.com/publicdatanetwork-dataparl/"
                    "collaborateurs/main/data/europarl/affectations.csv")
API = "https://data.europarl.europa.eu/api/v2/meps"
WB_CDX = "https://web.archive.org/cdx/search/cdx"
WB_WEB = "https://web.archive.org/web/"
RDF_ABOUT = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr,fr-FR;q=0.8,en;q=0.6",
}


def _get_once(url, timeout):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except Exception as e:
        return 0, str(e).encode()


def get(url, timeout=TIMEOUT, retries=3, backoff=(30, 60)):
    """GET avec retry sur 429 / erreurs reseau (Wayback rate-limite)."""
    code, body = _get_once(url, timeout)
    for i in range(retries - 1):
        if code == 200 and body:
            return code, body
        if code not in (429, 0, 502, 503, 504):
            return code, body
        pause = backoff[min(i, len(backoff) - 1)]
        print(f"    HTTP {code}, nouvelle tentative dans {pause}s...")
        time.sleep(pause)
        code, body = _get_once(url, timeout)
    return code, body


def meps_depuis_referentiel():
    code, body = get(RAW_PARLEMENTAIRES)
    if code != 200:
        print(f"  referentiel: HTTP {code}")
        return []
    meps = []
    lecteur = csv.DictReader(io.StringIO(body.decode("utf-8", "replace")))
    for r in lecteur:
        if r.get("chambre") == "europarl" and str(r.get("actif", "")).strip() == "1":
            meps.append(str(r["elu_id"]).strip())
    print(f"  referentiel parlementaires.csv : {len(meps)} MPE actifs")
    return list(dict.fromkeys(meps))


def meps_depuis_affectations():
    code, body = get(RAW_AFFECTATIONS)
    if code != 200:
        print(f"  affectations: HTTP {code}")
        return []
    ids = []
    lecteur = csv.DictReader(io.StringIO(body.decode("utf-8", "replace")))
    for r in lecteur:
        if r.get("chambre") == "europarl" and r.get("elu_id"):
            ids.append(str(r["elu_id"]).strip())
    print(f"  affectations.csv : {len(set(ids))} MPE uniques")
    return list(dict.fromkeys(ids))


def extraire_ids_rdf(body):
    ids = []
    try:
        racine = ET.fromstring(body)
    except ET.ParseError:
        return []
    for elem in racine.iter():
        uri = elem.attrib.get(RDF_ABOUT, "")
        m = re.search(r"person/(\d+)", uri)
        if m:
            ids.append(m.group(1))
        tag = elem.tag.rsplit("}", 1)[-1].lower()
        if tag == "identifier" and (elem.text or "").strip().isdigit():
            ids.append(elem.text.strip())
    return list(dict.fromkeys(ids))


def meps_depuis_api():
    ids, offset = [], 0
    params = {"parliamentary-term": "10", "limit": "200"}
    while True:
        q = dict(params, offset=str(offset))
        code, body = get(API + "?" + urllib.parse.urlencode(q), retries=1)
        if code != 200 or not body:
            print(f"  API offset={offset}: HTTP {code}")
            break
        lot = extraire_ids_rdf(body)
        if not lot:
            break
        ids.extend(lot)
        if len(lot) < 200:
            break
        offset += 200
    print(f"  API v2 : {len(set(ids))} MPE (pays non filtrable)")
    return list(dict.fromkeys(ids))


def meps():
    for source in (meps_depuis_referentiel, meps_depuis_affectations, meps_depuis_api):
        ids = source()
        if ids:
            print(f"Source MPE : {source.__name__} ({len(ids)})")
            return ids
    return []


def snapshots_assistants(mid):
    """CDX par prefixe : snapshots de la page assistants du MPE (cf. pe_historique)."""
    q = urllib.parse.urlencode({
        "url": f"europarl.europa.eu/meps/fr/{mid}/",
        "matchType": "prefix",
        "output": "json",
        "filter": "statuscode:200",
        "filter": "original:.*assistants.*",
        "limit": "-10",
    })
    code, body = get(WB_CDX + "?" + q, timeout=90)
    if code != 200 or not body:
        return None
    try:
        lignes = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None
    if len(lignes) < 2:
        return None
    entetes = lignes[0]
    i_ts, i_url = entetes.index("timestamp"), entetes.index("original")
    snaps = [(r[i_ts], r[i_url]) for r in lignes[1:]]
    snaps.sort(reverse=True)
    return snaps


def age_heures(timestamp):
    try:
        annee, mois, jour, heure, minute = (
            int(timestamp[0:4]), int(timestamp[4:6]), int(timestamp[6:8]),
            int(timestamp[8:10]), int(timestamp[10:12]),
        )
        epoch = time.mktime((annee, mois, jour, heure, minute, 0, 0, 0, -1))
    except Exception:
        return None
    return max(0.0, (time.time() - epoch) / 3600)


def categorie_depuis(texte):
    t = texte.lower()
    if "stagiaire" in t or "trainee" in t:
        return "stagiaires"
    if "payant" in t or "paying" in t:
        return "tiers_payants"
    if "prestataire" in t or "service provider" in t:
        return "prestataires"
    if "vice" in t or "questur" in t or "quaestor" in t:
        return "vice-presidence_questure"
    if "accrédit" in t or "accredit" in t or "accredited" in t:
        return "accredites"
    if "local" in t:
        return "locaux"
    return "autre"


def parser_assistants(html, mep_id, snapshot):
    soup = BeautifulSoup(html, "html.parser")
    lignes = []
    titres = soup.find_all(["h2", "h3", "h4", "h5", "summary"])
    for t in titres:
        texte = t.get_text(" ", strip=True)
        if not any(m in texte.lower() for m in ("assistant", "provider", "paying", "payant", "agent")):
            continue
        cat = categorie_depuis(texte)
        noms = set()
        elem = t.find_next()
        prof = 0
        while elem is not None and prof < 50:
            if getattr(elem, "name", None) in ("h2", "h3", "h4", "h5", "summary"):
                break
            if getattr(elem, "name", None) in ("li", "tr"):
                nom = elem.get_text(" ", strip=True)
                if nom and 3 <= len(nom) <= 150:
                    noms.add(re.sub(r"\s+", " ", nom))
            elem = elem.find_next()
            prof += 1
        for nom in noms:
            lignes.append((mep_id, nom, cat, snapshot))
    return lignes


def main():
    meps_liste = meps()
    if not meps_liste:
        print("!! Aucune source MPE disponible")
        sys.exit(2)
    meps_liste = meps_liste[:LIMITE]
    print(f"{len(meps_liste)} MPE a traiter, pause {PAUSE_GET}s, frais max {FRAIS_MAX_H}h")

    os.makedirs("data", exist_ok=True)
    lignes_csv = []
    stats = {"ok": 0, "sans_snapshot": 0, "sans_noms": 0, "perimes": 0}
    ages = []
    diag_premier = True

    for i, mid in enumerate(meps_liste, 1):
        snaps = snapshots_assistants(mid)
        if not snaps:
            print(f"[{i}/{len(meps_liste)}] {mid}: aucun snapshot -> KO")
            stats["sans_snapshot"] += 1
            time.sleep(PAUSE_GET)
            continue
        ts, url_originale = snaps[0]
        age = age_heures(ts)
        if age is not None:
            ages.append(age)
        fraicheur = "frais" if (age is not None and age <= FRAIS_MAX_H) else f"perime ({age:.0f}h)" if age else "?"

        code, body = get(WB_WEB + ts + "/" + url_originale, timeout=90)
        if code != 200:
            print(f"[{i}/{len(meps_liste)}] {mid}: snapshot {ts} HTTP {code} -> KO")
            stats["sans_snapshot"] += 1
            time.sleep(PAUSE_GET)
            continue

        if diag_premier:
            with open("diag_page_exemple.txt", "w", encoding="utf-8") as f:
                f.write(f"URL snapshot : {WB_WEB + ts + '/' + url_originale}\n\n")
                f.write(body.decode("utf-8", "replace")[:6000])
            diag_premier = False

        trouvees = parser_assistants(body, mid, ts)
        if trouvees:
            lignes_csv.extend(trouvees)
            stats["ok"] += 1
            print(f"[{i}/{len(meps_liste)}] {mid}: {ts} ({fraicheur}), {len(trouvees)} entrees")
        else:
            stats["sans_noms"] += 1
            print(f"[{i}/{len(meps_liste)}] {mid}: {ts} ({fraicheur}), 0 nom parse")
        if fraicheur.startswith("perime"):
            stats["perimes"] += 1
        time.sleep(PAUSE_GET)

    with open("data/europarl_assistants.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mep_id", "nom", "categorie", "snapshot", "url"])
        for mid, nom, cat, snap in lignes_csv:
            w.writerow([mid, nom, cat, snap,
                        f"https://www.europarl.europa.eu/meps/fr/{mid}/assistants"])

    ages_txt = ", ".join(f"{a:.0f}h" for a in sorted(ages)[:8]) if ages else "aucun"
    print(f"\nResume : {stats['ok']} MPE collectes, {stats['sans_snapshot']} sans snapshot, "
          f"{stats['sans_noms']} sans noms, {stats['perimes']} perimes")
    print(f"Ages des snapshots (8 plus frais) : {ages_txt}")
    print(f"Total lignes CSV : {len(lignes_csv)}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as s:
            s.write("## Collecte Wayback europarl\n\n")
            s.write(f"- MPE traites : {len(meps_liste)}\n")
            s.write(f"- MPE collectes : {stats['ok']}\n")
            s.write(f"- Lignes CSV : {len(lignes_csv)}\n")
            s.write(f"- Snapshots perimes (> {FRAIS_MAX_H}h) : {stats['perimes']}\n")

    if lignes_csv:
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
