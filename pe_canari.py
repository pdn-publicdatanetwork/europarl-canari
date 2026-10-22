#!/usr/bin/env python3
"""Canari : le WAF d'europarl.europa.eu laisse-t-il passer des requetes lentes
depuis une IP datacenter (GitHub Actions) ?

- Liste des MPE via l'API open data v2 (RDF/XML constate le 22/10/2026).
- 1 requete toutes les PAUSE secondes, en-tetes navigateur, timeout court.
- Publie sortie.txt (et diag.txt en cas d'echec API) pour le bilan d'issue.
- Sortie : 2 si liste MPE HS, 1 si 0 % de HTTP 200, sinon 0.

Aucune donnee personnelle n'est extraite : on ne releve que des codes HTTP.
"""

import csv
import json
import re
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

LIMITE = int(os.environ.get("LIMITE", "84"))
PAUSE = 15
TIMEOUT = 20
API = "https://data.europarl.europa.eu/api/v2/meps"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr,fr-FR;q=0.8,en;q=0.6",
}

VARIANTES = [
    {"parliamentary-term": "10", "country-code": "FRA", "limit": "200"},
    {"parliamentary-term": "10", "limit": "200"},
]


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
    """RDF/XML : identifiants depuis rdf:about/rdf:resource et identifiants
    numeriques presents comme valeurs. Defense en profondeur : on collecte
    tout attribut URI contenant un identifiant numerique, et les elements
    dont le tag local évoque un identifier/date d'entree."""
    ids = []
    try:
        racine = ET.fromstring(body)
    except ET.ParseError:
        return []
    for elem in racine.iter():
        for val in elem.attrib.values():
            m = re.search(r"meps?/(\d+)", val)
            if m:
                ids.append(m.group(1))
        tag = elem.tag.rsplit("}", 1)[-1].lower()
        if tag in ("identifier", "id", "dentifier") and (elem.text or "").strip().isdigit():
            ids.append(elem.text.strip())
    return list(dict.fromkeys(ids))


def extraire_ids_json(body):
    texte = body.decode("utf-8", "replace").strip()
    objets = []
    lignes = [l for l in texte.splitlines() if l.strip()]
    if len(lignes) > 1:
        try:
            objets = [json.loads(l) for l in lignes]
        except Exception:
            objets = []
    if not objets:
        try:
            doc = json.loads(texte)
            pile = [doc]
            while pile:
                n = pile.pop()
                if isinstance(n, list):
                    pile.extend(n)
                elif isinstance(n, dict):
                    if isinstance(n.get("@graph"), list):
                        pile.extend(n["@graph"])
                    else:
                        objets.append(n)
        except Exception:
            objets = []
    ids = []
    for obj in objets:
        if not isinstance(obj, dict):
            continue
        ident = obj.get("identifier")
        if ident is None and isinstance(obj.get("id"), str):
            m = re.search(r"meps?/(\d+)", obj["id"])
            ident = m.group(1) if m else None
        if ident:
            ids.append(str(ident))
    return list(dict.fromkeys(ids))


def extraire_ids(body):
    return extraire_ids_rdf(body) or extraire_ids_json(body)


def meps_depuis_api():
    diag = io = ""
    for params in VARIANTES:
        ids, offset = [], 0
        while True:
            q = dict(params)
            q["offset"] = str(offset)
            url = API + "?" + urllib.parse.urlencode(q)
            code, body = get(url)
            if code != 200 or not body:
                print(f"  variante {params} offset={offset}: HTTP {code} ({len(body)} o)")
                break
            lot = extraire_ids(body)
            if not lot:
                print(f"  variante {params} offset={offset}: 200, {len(body)} octets, 0 MPE lisible")
                # Diagnostic pousse dans l'issue via diag.txt
                with open("diag.txt", "w", encoding="utf-8") as f:
                    f.write(f"Requete : {url}\n\n")
                    f.write(body.decode("utf-8", "replace")[:4000])
                break
            ids.extend(lot)
            if len(lot) < int(params["limit"]):
                break
            offset += int(params["limit"])
        if ids:
            print(f"Variante retenue : {params} -> {len(ids)} MPE")
            return ids
        print(f"Variante {params} sans resultat, essai de la suivante")
    return []


def main():
    meps = meps_depuis_api()
    if not meps:
        print("!! Aucun MPE recuperable via l'API v2.")
        sys.exit(2)

    meps = meps[:LIMITE]
    print(f"{len(meps)} MPE a tester, pause {PAUSE}s entre requetes")

    pages = [("CENTRALE", "https://www.europarl.europa.eu/meps/fr/assistants")]
    for ident in meps:
        pages.append((ident, f"https://www.europarl.europa.eu/meps/fr/{ident}/assistants"))

    resultats = []
    for i, (cle, url) in enumerate(pages):
        code, _ = get(url)
        resultats.append((cle, url, code))
        print(f"[{i + 1}/{len(pages)}] {cle}: HTTP {code}")
        if i < len(pages) - 1:
            time.sleep(PAUSE)

    fiches = [(c, u, s) for (c, u, s) in resultats if c != "CENTRALE"]
    ok = sum(1 for _, _, s in fiches if s == 200)
    waf = sum(1 for _, _, s in fiches if s == 202)
    autre = len(fiches) - ok - waf
    taux = (100 * ok / len(fiches)) if fiches else 0

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as s:
            s.write("## Canari WAF europarl - resultats\n\n")
            s.write(f"- Fiches testees : **{len(fiches)}**\n")
            s.write(f"- HTTP 200 : **{ok}** ({taux:.0f} %)\n")
            s.write(f"- HTTP 202 (blocage WAF) : **{waf}**\n")
            s.write(f"- Autres codes : **{autre}**\n\n")
            s.write("| MPE | Code |\n|---|---|\n")
            for cle, _, code in resultats:
                s.write(f"| {cle} | {code} |\n")

    with open("canari_resultats.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mep", "url", "http"])
        w.writerows(resultats)

    print(f"Resume : {ok}/{len(fiches)} OK ({taux:.0f}%), {waf} blocages 202, {autre} autres")

    if fiches and ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
