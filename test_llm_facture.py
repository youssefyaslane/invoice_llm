"""Test d'extraction d'une facture PDF par un LLM local (Ollama dans Docker).

Usage (voir docker-compose.yml) :
    docker compose run --rm test [Data/facture.pdf] [-m modele]
ou en local :
    .venv-llm/bin/python test_llm_facture.py [facture.pdf] [-m modele] [--url http://localhost:11434]

Sans argument, traite Data/INV-55868.pdf avec qwen2.5:3b (ou $MODELE).
Extrait seulement : fournisseur, date de paiement, devise, articles (description + prix) et total.
Si la facture est dans ATTENDU, le résultat est comparé aux valeurs exactes.
"""
import argparse
import json
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pdfplumber

BASE_DIR = Path(__file__).resolve().parent

TXT, NUM, NUM_NULL = {"type": "string"}, {"type": "number"}, {"type": ["number", "null"]}
CHAMPS = {
    "fournisseur": TXT,
    "date_paiement": TXT,
    "devise": TXT,
    "articles": {
        "type": "array",
        # quantite et prix_unitaire aident le modèle à ne pas confondre prix unitaire et montant de la ligne
        "items": {"type": "object",
                  "properties": {"description": TXT, "quantite": NUM_NULL, "prix_unitaire": NUM_NULL, "prix": NUM},
                  "required": ["description", "quantite", "prix_unitaire", "prix"]},
    },
    "total": NUM,
}
SCHEMA = {"type": "object", "properties": CHAMPS, "required": list(CHAMPS)}

SYSTEME = """Extrais de la facture : le fournisseur, la date de paiement, la devise, les articles achetés et le total.
Le texte vient d'un PDF à colonnes mélangées : une étiquette et sa valeur peuvent être sur des lignes différentes.
- fournisseur : nom de la société qui émet la facture et reçoit le paiement (jamais un nom de produit).
- Si la description d'un article contient "ISO", le fournisseur est "ISO".
- date_paiement : date de paiement si elle est écrite, sinon date d'échéance ("Due Date", "Due on", "Échéance").
- date_paiement est toujours au format JJ/MM/AAAA.
- devise : code ISO à 3 lettres (USD, EUR, GBP, MAD...).
- articles : un article par ligne qui contient des montants, dans l'ordre. Les lignes de texte sans montant
  complètent la description de l'article précédent : ce ne sont pas des articles.
- Un titre de section est la ligne juste avant les en-têtes de colonnes d'un tableau (Qty, Unit Price, Total...) :
  ce n'est ni un article ni la suite d'une description.
- description : libellé écrit sur la ligne de l'article, sans la quantité ni les montants, recopié sans traduire ;
  si la ligne n'a pas de libellé (seulement des dates et des montants), prends le titre de section au-dessus.
- quantite, prix_unitaire : recopiés de la ligne de l'article ; null s'ils n'y sont pas écrits.
- prix : recopie le dernier montant écrit sur la ligne de l'article. Ne calcule jamais un montant.
- Ne sont pas des articles : taxe (VAT, TVA, Tax), sous-total, total, montant dû (Amount due), montant payé.
- total : montant total de la facture, taxes comprises.
- Montants : nombres à point décimal, sans devise ni séparateur de milliers."""

# Valeurs exactes des factures de test (hors dépôt git) :
# {"facture": {"champs": {champ: valeur ou [valeurs acceptées]}, "articles": [[description ou null, prix], ...]}}
FICHIER_ATTENDU = BASE_DIR / "attendu.json"
ATTENDU = json.loads(FICHIER_ATTENDU.read_text(encoding="utf-8")) if FICHIER_ATTENDU.is_file() else {}


def extraire_texte(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(p.extract_text(x_tolerance=1) or "" for p in pdf.pages)


def appeler_ollama(url, modele, messages, schema=SCHEMA):
    corps = {
        "model": modele,
        "messages": messages,
        "format": schema,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 4096},
    }
    req = urllib.request.Request(f"{url}/api/chat", data=json.dumps(corps).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        rep = json.load(r)
    return json.loads(rep["message"]["content"]), rep


def retirer_netis(nom):
    """Dernier recours quand les colonnes client/fournisseur sont collées sur une ligne :
    "NETIS Holding Ltd c/o The Rocket Science Group, LLC" -> "The Rocket Science Group, LLC"."""
    if re.search(r"\bc/o\b", nom, re.I):
        return re.split(r"\bc/o\b", nom, flags=re.I)[-1].strip(" ,")
    return re.sub(r"\bNETIS\b(\s+(Holding|Group)\b)?(\s+(Ltd|SA|SARL|SAS)\b\.?)?", "", nom, flags=re.I).strip(" ,-")


def nettoyer_nom(nom):
    """Garde seulement le nom de société : "Mailchimp® All rights reserved. (commentaire)" -> "Mailchimp"."""
    nom = re.sub(r"\([^)]*\)|[®™©]|all rights reserved\.?|\d{4}\s*[–-]\s*\d{4}", "", nom, flags=re.I)
    return re.sub(r"\s+", " ", nom).strip(" ,;:-")


def nom_commercial(nom, texte):
    """Préfère le nom commercial tiré du site web ou de l'email du fournisseur (hors NETIS) :
    "The Rocket Science Group, LLC" + "www.mailchimp.com" dans la facture -> "Mailchimp"."""
    domaines = re.findall(r"(?:@|www\.|://)((?:[\w-]+\.)+[a-z]{2,})", texte, re.I)
    marques = [d.split(".")[-2] for d in domaines if "netis" not in d.lower()]
    if any(m.replace("-", " ").lower() in nom.lower() for m in marques):
        return nom
    for m in marques:
        trouve = re.search(rf"(?<![@.\w]){re.escape(m)}(?![.\w@])", texte, re.I)
        if trouve:
            return trouve[0]
    return nom


def corriger_fournisseur(url, modele, messages, rep, data):
    """NETIS est le client : si le modèle l'a pris pour le fournisseur, on lui repose la question.
    Le début de la conversation est identique, donc Ollama réutilise la lecture déjà faite de la facture."""
    client = data.get("fournisseur")
    if "netis" not in str(client).lower():
        return
    suite = messages + [
        rep["message"],
        {"role": "user", "content": "NETIS est le client, pas le fournisseur. Quel est le nom de la société qui "
                                    "émet la facture (après \"Issued by\", \"From\" ou en en-tête) ? "
                                    "Réponds seulement par ce nom, recopié du texte."},
    ]
    t0 = time.time()
    reponse, _ = appeler_ollama(url, modele, suite, {"type": "object", "properties": {"fournisseur": TXT},
                                                      "required": ["fournisseur"]})
    nouveau = nettoyer_nom(reponse["fournisseur"])
    texte = messages[-1]["content"]
    # Réponse encore NETIS ou absente du PDF : on retire NETIS du nom collé ("NETIS Holding Ltd c/o ...")
    if "netis" in nouveau.lower() or nouveau.lower() not in texte.lower():
        nouveau = retirer_netis(nouveau if "netis" in nouveau.lower() else client) or nouveau
    nouveau = nom_commercial(nouveau, texte)
    print(f"Correction : fournisseur « {client} » -> « {nouveau} » "
          f"(question reposée au modèle, {time.time() - t0:.1f} s)")
    data["fournisseur"] = nouveau


FORMATS_DATE = ["%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d.%m.%Y", "%d-%m-%Y"]


MOIS_FR = {"janvier": "January", "février": "February", "fevrier": "February", "mars": "March", "avril": "April",
           "mai": "May", "juin": "June", "juillet": "July", "août": "August", "aout": "August",
           "septembre": "September", "octobre": "October", "novembre": "November", "décembre": "December",
           "decembre": "December"}


def normaliser_date(texte):
    """Remet une date au format JJ/MM/AAAA ("22 Jan 2026", "01 Janvier 2026" -> JJ/MM/AAAA) ; inchangée si inconnue."""
    texte = re.sub(r"[A-Za-zéûè]+", lambda m: MOIS_FR.get(m[0].lower(), m[0]), str(texte))
    for fmt in FORMATS_DATE:
        try:
            return datetime.strptime(texte.strip(), fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return texte


DATE_TEXTE = r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{4}-\d{2}-\d{2}|[A-Za-zéû]+\.? \d{1,2},? \d{4}|\d{1,2} [A-Za-zéû]+\.? \d{4})"
ETIQUETTES_PAIEMENT = ["date paid", "paid on", "payment date", "date de paiement", "prélevé le", "due date", "date due", "due on",
                       "date d'échéance", "échéance"]


def date_dans_texte(texte):
    """Date de paiement lue dans le PDF après son étiquette ("Date Paid: October 12, 2026"), sinon d'échéance."""
    for etiquette in ETIQUETTES_PAIEMENT:
        m = re.search(re.escape(etiquette) + r"\s*:?\s*" + DATE_TEXTE, texte, re.I)
        if m and re.fullmatch(r"\d{2}/\d{2}/\d{4}", normaliser_date(m[1])):
            return normaliser_date(m[1])
    return None


def norm(v):
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    return str(v or "").strip().lower()


def ecritures(v):
    """Façons d'écrire un montant dans une facture : 1,050.66 / 1050.66 / 1 050,66 / 1050,66."""
    en = f"{v:,.2f}"
    return {en, f"{v:.2f}", en.replace(",", " ").replace(".", ","), f"{v:.2f}".replace(".", ",")}


MONTANT = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:[ \u00a0]\d{3})+,\d{2}|\d{1,3}(?:,\d{3})+\.\d{2}|\d+[.,]\d{2})(?!\d)")
# Débuts de libellé qui désignent une taxe ou un total, jamais un article
NON_ARTICLES = ("vat", "tva", "tax", "subtotal", "sub-total", "sous-total", "sous total", "total", "amount due", "amount paid",
                "balance", "montant dû", "montant du", "montant payé", "net à payer", "reste à payer")
DEVISES = re.compile(r"[$€£]|\b(?:USD|EUR|GBP|CHF|MAD)\b")


def est_non_article(libelle):
    return libelle.strip().lower().startswith(NON_ARTICLES)


def completer_ligne(a, ligne):
    """Complète l'article avec sa ligne du PDF ("Read Pro Plan 2 $19.75 $39.50") :
    - un entier juste avant le 1er montant est la quantité (colonne Qty), pas une partie de la description ;
    - si quantite est connue, le montant prix / quantite écrit sur la ligne est le prix unitaire."""
    desc = (a.get("description") or "").strip()
    m = re.search(r"\s(\d+)$", desc)
    premier = MONTANT.search(ligne)
    if m and premier:
        avant = DEVISES.sub("", ligne[:premier.start()]).strip()
        if avant.lower().endswith(desc.lower()) and avant.endswith(" " + m[1]):
            print(f"Correction : quantité {m[1]} retirée de la description « {desc} »")
            a["description"] = desc[:m.start()].strip()
            if a.get("quantite") is None:
                a["quantite"] = int(m[1])

    q, prix = a.get("quantite"), a.get("prix")
    if a.get("prix_unitaire") is None and isinstance(q, (int, float)) and q > 0 and isinstance(prix, (int, float)):
        unitaire = round(prix / q, 2)
        if any(e in ligne for e in ecritures(unitaire)):
            print(f"Correction : prix_unitaire {unitaire} (= {prix} / {q}, écrit sur la ligne du PDF)")
            a["prix_unitaire"] = unitaire


def corriger_articles(data, texte):
    """Corrige les articles avec le texte du PDF : retire les lignes de taxe/total et, si la description
    n'est pas sur la ligne du PDF qui se termine par le prix, la remplace par le libellé de cette ligne."""
    articles = []
    for a in data.get("articles") or []:
        if est_non_article(a.get("description") or ""):
            print(f"Correction : article « {a.get('description')} » retiré (taxe ou total)")
        else:
            articles.append(a)
    # Prix absent du PDF (ex. "5,000 contacts" lu comme 5000) : ce n'est pas un article
    for a in [a for a in articles if isinstance(a.get("prix"), (int, float))
              and not any(e in texte for e in ecritures(a["prix"]))]:
        print(f"Correction : article « {a.get('description')} » retiré (prix {a['prix']} absent de la facture)")
        articles.remove(a)
    data["articles"] = articles

    # Lignes du PDF qui se terminent par un montant, avec leur libellé (texte avant le 1er montant, sans devise)
    toutes, lignes = texte.splitlines(), []
    for j, ligne in enumerate(toutes):
        montants = MONTANT.findall(ligne)
        if montants:
            libelle = DEVISES.sub("", ligne[:MONTANT.search(ligne).start()]).strip()
            lignes.append((j, ligne, montants[-1], libelle))

    # 1er passage : articles dont la description est bien sur une ligne qui se termine par leur prix
    utilisees, a_corriger = set(), []
    for a in articles:
        if not isinstance(a.get("prix"), (int, float)):
            continue
        desc = (a.get("description") or "").lower()
        # description sur la ligne des montants, ou sur la ligne juste au-dessus
        j = next((j for j, ligne, m, _ in lignes if j not in utilisees and m in ecritures(a["prix"]) and desc
                  and (desc[:10] in ligne.lower() or desc[-10:] in ligne.lower()
                       or (j > 0 and desc[:10] in toutes[j - 1].lower()))), None)
        if j is None:
            a_corriger.append(a)
        else:
            utilisees.add(j)
            completer_ligne(a, next(ligne for k, ligne, _, _ in lignes if k == j))

    # 2e passage : les autres prennent le libellé d'une ligne d'article libre qui se termine par leur prix
    for a in a_corriger:
        trouve = False
        for j, ligne, m, libelle in lignes:
            if j in utilisees or m not in ecritures(a["prix"]) or est_non_article(libelle):
                continue
            if re.search(r"[A-Za-zÀ-ÿ]", libelle):
                utilisees.add(j)
                print(f"Correction : description « {a.get('description')} » -> « {libelle} » (lue sur la ligne du PDF)")
                a["description"] = libelle
                completer_ligne(a, ligne)
                trouve = True
                break
        # Article à 0 qui ne correspond à aucune ligne de la facture (ex. "Looking for our W-9?")
        if not trouve and a.get("prix") == 0:
            print(f"Correction : article « {a.get('description')} » retiré (à 0, aucune ligne dans la facture)")
            articles.remove(a)


def controles(data, texte):
    articles = data.get("articles") or []
    somme = round(sum(a.get("prix") or 0 for a in articles), 2)
    total = data.get("total") or 0
    print("\nContrôle somme articles = total :",
          "OK" if somme == total else f"ÉCART ({somme} vs {total}, normal si la facture a des taxes)")

    # Un montant absent du texte du PDF a été inventé ou calculé par le modèle
    montants = [("total", total)] + [(f"article {i + 1}", a.get("prix")) for i, a in enumerate(articles)]
    absents = [f"{nom} = {v}" for nom, v in montants
               if isinstance(v, (int, float)) and not any(e in texte for e in ecritures(v))]
    print("Contrôle fournisseur différent de NETIS :",
          "ERREUR (NETIS est le client)" if "netis" in str(data.get("fournisseur")).lower() else "OK")
    print("Contrôle montants présents dans la facture :", "OK" if not absents else "INTROUVABLES : " + ", ".join(absents))


def comparer(data, attendu):
    ok = 0
    for champ, val in attendu["champs"].items():
        bon = norm(data.get(champ)) in {norm(v) for v in (val if isinstance(val, list) else [val])}
        ok += bon
        print(f"{'✅' if bon else '❌'} {champ:<15} attendu={val!r:<40} obtenu={data.get(champ)!r}")

    articles = data.get("articles") or []
    # Article juste = bon prix, et bonne description quand elle est attendue (None = description non vérifiée)
    for i, (desc, prix) in enumerate(attendu["articles"]):
        art = articles[i] if i < len(articles) else {}
        bon = norm(art.get("prix")) == norm(prix) and (desc is None or norm(art.get("description")) == norm(desc))
        ok += bon
        print(f"{'✅' if bon else '❌'} article {i + 1:<7} attendu={(desc or '') + ' ' + str(prix):<40} "
              f"obtenu={art.get('description') or ''} {art.get('prix')!r}")

    total = len(attendu["champs"]) + len(attendu["articles"])
    print(f"\nScore : {ok}/{total} = {ok / total:.0%}   "
          f"({len(articles)} articles extraits, {len(attendu['articles'])} attendus)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", default=BASE_DIR / "Data" / "INV-55868.pdf", type=Path)
    ap.add_argument("-m", "--modele", default=os.environ.get("MODELE", "qwen2.5:3b"))
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    args = ap.parse_args()

    texte = extraire_texte(args.pdf)
    print(f"Facture : {args.pdf.name}  ({len(texte)} caractères)  —  modèle : {args.modele}")

    t0 = time.time()
    messages = [{"role": "system", "content": SYSTEME}, {"role": "user", "content": texte}]
    data, rep = appeler_ollama(args.url, args.modele, messages)
    duree = time.time() - t0
    corriger_fournisseur(args.url, args.modele, messages, rep, data)
    data["date_paiement"] = normaliser_date(data.get("date_paiement"))
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", str(data["date_paiement"])) and date_dans_texte(texte):
        print(f"Correction : date_paiement « {data['date_paiement']} » -> « {date_dans_texte(texte)} » (lue dans le PDF)")
        data["date_paiement"] = date_dans_texte(texte)
    corriger_articles(data, texte)
    print(f"Temps : {duree:.1f} s  (chargement {rep.get('load_duration', 0) / 1e9:.1f} s, "
          f"lecture {rep.get('prompt_eval_count', 0)} tokens en {rep.get('prompt_eval_duration', 0) / 1e9:.1f} s, "
          f"génération {rep.get('eval_count', 0)} tokens en {rep.get('eval_duration', 0) / 1e9:.1f} s)\n")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    controles(data, texte)

    sortie = BASE_DIR / "Output" / f"{args.pdf.stem}_{args.modele.replace(':', '_')}.json"
    sortie.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Résultat enregistré : {sortie}")

    if args.pdf.stem in ATTENDU:
        print("\n--- Comparaison avec les valeurs exactes ---")
        comparer(data, ATTENDU[args.pdf.stem])


if __name__ == "__main__":
    main()
