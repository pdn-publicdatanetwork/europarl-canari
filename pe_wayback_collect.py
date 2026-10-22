#!/usr/bin/env python3
"""Collecte des assistants des MPE francais via le Wayback Machine.

Pipeline par MPE :
  1. SPN anonyme : GET https://web.archive.org/save/<url> (capture du jour)
  2. CDX : timestamp du snapshot le plus recent
  3. Recuperation du snapshot et parsing HTML (categories + noms)
Sortie : data/europarl_assistants.csv (mep_id, nom, categorie, snapshot, url)

Aucune requete vers europarl.europa.eu : seul web.archive.org est sollicite.
"""

import csv
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

LIMITE = int(os.environ.get("LIMITE", "5"))
PAUSE_SPN = 30
PAUSE_GET = 5
TIMEOUT = 60
API = "https://data.europarl.europa.eu/api/v2/meps"
WB_SAVE = "https://web.archive.org/save/"
WB_CDX = "https://web.archive.org/cdx/search/cdx"
WB_WEB = "https://web.archive.org/web/"
FRAIS_MAX_H = 72
RDF_ABOUT = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"

# Variantes de filtre pays a tester ; on retient celle qui renvoie le moins
# de MPE (84 francais attendus, 746 = filtre ignore).
VARIANTES_PAYS = [
    {"country-code": "FRA"},
    {"countryCode": "FRA"},
    {"country": "FRA"},
    {"country": "France"},
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr,fr-FR;q=0.8,en;q=0.6",
}


def get(url, headers=HEADERS, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except Exception as e:
        return 0, str(e).encode()


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


def pages_ids(params, tentatives=3):
    ids, offset, essais = [], 0, 0
    while True:
        q = dict(params, offset=str(offset))
        code, body = get(API + "?" + urllib.parse.urlencode(q))
        if code != 200 or not body:
            essais += 1
            print(f"  listing offset={offset}: HTTP {code}, essai {essais}/{tentatives}")
            if essais >= tentatives:
                return None
            time.sleep(20)
            continue
        essais = 0
        lot = extraire_ids_rdf(body)
        if not lot:
            with open("diag_api.txt", "w", encoding="utf-8") as f:
                f.write(f"Requete : {API}?{urllib.parse.urlencode(q)}\n\n")
                f.write(body.decode("utf-8", "replace")[:4000])
            return None
        ids.extend(lot)
        if len(lot) < int(params["limit"]):
            break
        offset += int(params["limit"])
    return ids


def meps_depuis_api():
    base = {"parliamentary-term": "10", "limit": "200"}
    meilleur = None
    for filtre in VARIANTES_PAYS:
        params = dict(base, **filtre)
        ids = pages_ids(params)
        if ids is None:
            print(f"  variante {filtre}: HS")
            continue
        print(f"  variante {filtre}: {len(ids)} MPE")
        if meilleur is None or len(ids) < len(meilleur[1]):
            meilleur = (filtre, ids)
        if len(ids) <= 100:
            break  # ~84 francais -> filtre efficace, inutile de tester le reste
    if meilleur is None:
        return None
    print(f"Filtre pays retenu : {meilleur[0]} ({len(meilleur[1])} MPE)")
    return meilleur[1]


def spn(url):
    code, body = get(WB_SAVE + url)
    return code, body


def dernier_snapshot(url):
    q = urllib.parse.urlencode({
        "url": url, "output": "json", "limit": "1",
        "filter": "statuscode:200", "collapse": "digest",
    })
    code, body = get(WB_CDX + "?" + q)
    if code != 200 or not body:
        return None
    try:
        lignes = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None
    if len(lignes) < 2:
        return None
    return lignes[1][1]


CATEGORIES = {
    "accredited": "accredites",
    "locaux": "locaux",
    "local": "locaux",
    "vice": "vice-presidence_questure",
    "quaestor": "vice-presidence_questure",
    "service provider": "prestataires",
    "paying agent": "tiers_payants",
    "trainee": "stagiaires",
}


def categorie_depuis(texte):
    t = texte.lower()
    if "stagiaire" in t or "trainee" in t:
        return "stagiaires"
    for cle, cat in CATEGORIES.items():
        if cle in t:
            return cat
    return "autre"


def parser_assistants(html, mep_id, snapshot):
    soup = BeautifulSoup(html, "html.parser")
    lignes = []
    titres = soup.find_all(["h2", "h3", "h4", "h5", "summary"])
    for t in titres:
        texte = t.get_text(" ", strip=True)
        if not any(m in texte.lower() for m in ("assistant", "provider", "paying", "agent")):
            continue
        cat = categorie_depuis(texte)
        noms = set()
        elem = t.find_next()
        prof = 0
        while elem is not None and prof < 40:
            if getattr(elem, "name", None) in ("h2", "h3", "h4", "h5", "summary"):
                break
            if getattr(elem, "name", None) == "li":
                nom = elem.get_text(" ", strip=True)
                if nom and 3 <= len(nom) <= 120:
                    noms.add(re.sub(r"\s+", " ", nom))
            elem = elem.find_next()
            prof += 1
        for nom in noms:
            lignes.append((mep_id, nom, cat, snapshot))
    return lignes


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


def main():
    meps = meps_depuis_api()
    if not meps:
        print("!! Listing MPE HS (diagnostics dans diag_api.txt)")
        sys.exit(2)
    meps = meps[:LIMITE]
    print(f"{len(meps)} MPE, pause SPN {PAUSE_SPN}s, pause GET {PAUSE_GET}s")

    os.makedirs("data", exist_ok=True)
    lignes_csv = []
    stats = {"ok": 0, "spn_ko": 0, "sans_snapshot": 0, "sans_noms": 0}
    diag_premier = True

    for i, mid in enumerate(meps, 1):
        url = f"https://www.europarl.europa.eu/meps/fr/{mid}/assistants"

        code, _ = spn(url)
        if code not in (200, 302, 303, 307, 308):
            print(f"[{i}/{len(meps)}] {mid}: SPN HTTP {code} -> KO")
            stats["spn_ko"] += 1
        else:
            print(f"[{i}/{len(meps)}] {mid}: SPN HTTP {code}")

        snap = dernier_snapshot(url)
        if not snap:
            print(f"    {mid}: aucun snapshot CDX -> KO")
            stats["sans_snapshot"] += 1
            time.sleep(PAUSE_SPN)
            continue

        age = age_heures(snap)
        fraicheur = "frais" if (age is not None and age <= FRAIS_MAX_H) else "moyen"
        code2, body2 = get(WB_WEB + snap + "/" + url)
        if code2 != 200:
            print(f"    {mid}: snapshot {snap} HTTP {code2} -> KO")
            stats["sans_snapshot"] += 1
            time.sleep(PAUSE_SPN)
            continue

        if diag_premier:
            with open("diag_page_exemple.txt", "w", encoding="utf-8") as f:
                f.write(f"URL snapshot : {WB_WEB + snap + '/' + url}\n\n")
                f.write(body2.decode("utf-8", "replace")[:6000])
            diag_premier = False

        trouvees = parser_assistants(body2, mid, snap)
        if trouvees:
            lignes_csv.extend(trouvees)
            stats["ok"] += 1
            print(f"    {mid}: snapshot {snap} ({fraicheur}), {len(trouvees)} entrees")
        else:
            stats["sans_noms"] += 1
            print(f"    {mid}: snapshot {snap} ({fraicheur}) mais 0 nom parse")
        time.sleep(PAUSE_SPN if code not in (200, 302) else PAUSE_GET)

    with open("data/europarl_assistants.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mep_id", "nom", "categorie", "snapshot", "url"])
        for mid, nom, cat, snap in lignes_csv:
            w.writerow([mid, nom, cat, snap,
                        f"https://www.europarl.europa.eu/meps/fr/{mid}/assistants"])

    print(f"\nResume : {stats['ok']} MPE collectes, {stats['spn_ko']} SPN KO, "
          f"{stats['sans_snapshot']} sans snapshot, {stats['sans_noms']} sans noms")
    print(f"Total lignes CSV : {len(lignes_csv)}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as s:
            s.write("## Collecte Wayback europarl\n\n")
            s.write(f"- MPE traites : {len(meps)}\n")
            s.write(f"- MPE collectes : {stats['ok']}\n")
            s.write(f"- Lignes CSV : {len(lignes_csv)}\n")

    if lignes_csv:
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
