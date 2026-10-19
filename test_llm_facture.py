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
- date_paiement : date de paiement si elle est écrite, sinon date d'échéance ("Due Date", "Due on", "Échéance"),
  sinon date de la facture. Jamais une date de période de service ou d'abonnement.
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
- Inclus aussi les articles dont le prix est 0.
- total : montant total de la facture, taxes comprises.
- Montants : nombres à point décimal, sans devise ni séparateur de milliers."""

# Valeurs exactes des factures de test (hors dépôt git) :
# {"facture": {"champs": {champ: valeur ou [valeurs acceptées]}, "articles": [[description ou null, prix], ...]}}
FICHIER_ATTENDU = BASE_DIR / "attendu.json"
ATTENDU = json.loads(FICHIER_ATTENDU.read_text(encoding="utf-8")) if FICHIER_ATTENDU.is_file() else {}


def extraire_texte(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        texte = "\n".join(p.extract_text(x_tolerance=1) or "" for p in pdf.pages)
    # certains PDF insèrent un tiret "—" au milieu des mots : "Ju—n 24" -> "Jun 24", "Amou— nt" -> "Amount"
    return re.sub(r"(?<=\w)—\s?(?=\w)", "", texte)


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


FORME_JURIDIQUE_FIN = re.compile(
    r"[\s,]+(Inc|Ltd|LLC|L\.L\.C|Limited|Corp|Corporation|Co|Pty|Plc|GmbH|SAS|SARL|SA|S\.A|SL|SLU|S\.L|S\.L\.U)\.?$", re.I)


def sans_forme_juridique(nom):
    """Nom du fournisseur sans sa forme juridique : "BitTitan, Inc." -> "BitTitan", "AG Grid Ltd." -> "AG Grid"."""
    while nom and FORME_JURIDIQUE_FIN.search(nom):
        nom = FORME_JURIDIQUE_FIN.sub("", nom).strip(" ,")
    return nom


# Nom retenu pour les fournisseurs qui facturent sous plusieurs entités (filiale, pays...)
NOMS_FOURNISSEURS = {r"^Microsoft\b": "Microsoft", r"^Adobe\b": "Adobe", r"^Freepik\b": "Magnific"}


def nom_fournisseur(nom):
    """Nom final du fournisseur : sans forme juridique, puis nom retenu dans NOMS_FOURNISSEURS."""
    nom = sans_forme_juridique(nom)
    for motif, retenu in NOMS_FOURNISSEURS.items():
        if nom and re.search(motif, nom, re.I):
            return retenu
    return nom


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


MENTIONS_LEGALES = re.compile(r"\b(au capital de|RCS|SIREN|Registered office|Registered in|Company (?:no|number))\b", re.I)
FORMES_JURIDIQUES = {"sas", "sarl", "sa", "ltd", "limited", "llc", "inc", "gmbh", "plc"}


def nom_pied_de_page(texte):
    """Nom de société des mentions légales (capital, RCS, SIREN...), sur la même ligne ou juste au-dessus :
    "OVH - 2 rue kellermann ..." puis "SAS au capital de ... RCS ..." -> "OVH"."""
    lignes = texte.splitlines()
    for j, ligne in enumerate(lignes):
        m = MENTIONS_LEGALES.search(ligne)
        if not m:
            continue
        for candidat in (ligne[:m.start()], lignes[j - 1] if j else ""):
            nom = re.split(r" - |,", candidat)[0].strip(" -,")
            # un nom de société : des lettres, pas d'étiquette ("No:") ni de numéro d'immatriculation
            if (nom and "netis" not in nom.lower() and nom.lower() not in FORMES_JURIDIQUES and ":" not in nom
                    and re.search(r"[A-Za-z]", nom) and len(re.findall(r"\d", nom)) < 3 and len(nom) <= 40):
                return nom
    return None


def corriger_fournisseur(url, modele, messages, rep, data):
    """NETIS est le client : si le modèle l'a pris pour le fournisseur, on lui repose la question.
    Le début de la conversation est identique, donc Ollama réutilise la lecture déjà faite de la facture."""
    client = data.get("fournisseur")
    if "netis" not in str(client).lower():
        return
    # Sans la 1re réponse du modèle, pour qu'il ne la répète pas ; le début (facture) reste en cache
    suite = messages + [
        {"role": "user", "content": "NETIS est le client de cette facture, pas le fournisseur. Quel est le nom de "
                                    "l'autre société, celle qui émet la facture et reçoit le paiement ? Cherche après "
                                    "\"Issued by\" ou \"From\", dans le pied de page (adresse, capital, RCS, SIREN, "
                                    "TVA) ou le site web. Réponds seulement par ce nom, recopié du texte."},
    ]
    t0 = time.time()
    reponse, _ = appeler_ollama(url, modele, suite, {"type": "object", "properties": {"fournisseur": TXT},
                                                      "required": ["fournisseur"]})
    nouveau = nettoyer_nom(reponse["fournisseur"])
    texte = messages[-1]["content"]
    # Réponse encore NETIS ou absente du PDF : on retire NETIS du nom collé ("NETIS Holding Ltd c/o ...")
    if "netis" in nouveau.lower() or nouveau.lower() not in texte.lower():
        nouveau = retirer_netis(nouveau if "netis" in nouveau.lower() else client) or nouveau
    if "netis" in nouveau.lower():
        nouveau = nom_pied_de_page(texte) or nouveau
    nouveau = nom_commercial(nouveau, texte)
    print(f"Correction : fournisseur « {client} » -> « {nouveau} » "
          f"(question reposée au modèle, {time.time() - t0:.1f} s)")
    data["fournisseur"] = nouveau


FORMATS_DATE = ["%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%d/%b/%Y", "%d %b %Y", "%d %B %Y", "%d %b, %Y", "%d %B, %Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d.%m.%Y", "%d-%m-%Y"]


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


DATE_TEXTE = (r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{4}-\d{2}-\d{2}|[A-Za-zéû]+\.? \d{1,2},? \d{4}"
              r"|\d{1,2}[ /-][A-Za-zéû]+\.?,?[ /-]\d{4})")
# Par ordre de priorité : paiement, puis échéance, puis date de la facture
ETIQUETTES_PAIEMENT = ["date paid", "paid date", "paid on", "payment date", "date de paiement", "prélevé le",
                       "due date", "date due", "due on", "date d'échéance", "échéance",
                       "invoice date", "date de facture", "date d'émission", "document date", "date"]


ADRESSE_US = re.compile(r"\b[A-Z]{2},?\s+\d{5}(?:-\d{4})?\b")  # code d'État + code postal : "TX 78731"


def date_americaine(date, texte):
    """Facture d'un fournisseur américain (adresse US dans le PDF) : une date ambiguë recopiée du PDF
    est au format MM/JJ/AAAA. "03/06/2026" -> "06/03/2026" (6 mars)."""
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", str(date))
    if (m and m[1] != m[2] and int(m[1]) <= 12 and int(m[2]) <= 12 and date in texte
            and ADRESSE_US.search(texte) and re.search(r"\b(USA|United States)\b", texte)):
        return f"{m[2]}/{m[1]}/{m[3]}"
    return date


def date_dans_texte(texte):
    """Date de paiement lue dans le PDF après son étiquette ("Date Paid: October 12, 2026"), sinon d'échéance."""
    for etiquette in ETIQUETTES_PAIEMENT:
        debut = r"^\s*" if etiquette == "date" else r"\b"
        m = re.search(debut + re.escape(etiquette) + r"\s*:?\s*" + DATE_TEXTE, texte, re.I | re.M)
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
                "balance", "invoice amount", "payment", "montant dû", "montant du", "montant payé", "net à payer", "reste à payer")
DEVISES = re.compile(r"[$€£]|\b(?:USD|EUR|GBP|CHF|MAD)\b")


MOTS_TOTAUX = re.compile(r"\b(tax|taxes|tva|vat|total|subtotal|sous-total|sous total|amount due|balance)\b", re.I)


def meme_montant(m, prix):
    """Le montant lu m vaut prix, y compris quand une quantité est collée devant au format français :
    "1 250,00" (quantité 1 + 250,00) vaut 250."""
    return any(m[i:].strip() in ecritures(prix) for i in [0] + [k for k, c in enumerate(m) if c == " "])


def est_non_article(libelle):
    return libelle.strip().lower().startswith(NON_ARTICLES)


def completer_ligne(a, ligne):
    """Complète l'article avec sa ligne du PDF ("Pro Plan 2 $10.00 $20.00") :
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


def forme(ligne):
    """Forme d'une ligne d'article : nombre de montants, et présence d'une quantité entière avant le 1er montant."""
    premier = MONTANT.search(ligne)
    avant = DEVISES.sub("", ligne[:premier.start()]) if premier else ""
    return len(MONTANT.findall(ligne)), bool(re.search(r"\s\d+\s*$", " " + avant))


def ajouter_oublies(data, toutes, lignes, positions):
    """Ajoute les lignes d'article oubliées par le modèle (ex. articles à 0 en fin de facture) : même forme que les
    articles trouvés, libre, pas une taxe ni un total, et la somme des articles reste égale au total."""
    formes = {forme(toutes[j]) for j in positions.values()}
    total = data.get("total")
    for j, ligne, m, libelle in lignes:
        # au moins 2 montants (quantité/prix/total) : une ligne à 1 seul montant est trop ambiguë
        if (j in positions.values() or forme(ligne) not in formes or forme(ligne)[0] < 2
                or MOTS_TOTAUX.search(libelle) or total is None):
            continue
        prix = float(m.replace(" ", "").replace("\u00a0", "").replace(",", ".") if "," in m[-3:]
                     else m.replace(",", ""))
        somme = round(sum(a.get("prix") or 0 for a in data["articles"]), 2)
        if prix != 0 and prix != round(total - somme, 2):
            continue
        desc = re.sub(r"\s\d+$", "", libelle).strip()
        precedente = toutes[j - 1] if j else ""
        # ligne du dessus = nom de l'article (OVH), si c'est du texte et non des dates ou des montants
        if not MONTANT.search(precedente) and not est_non_article(precedente) and re.search(r"[A-Za-zÀ-ÿ]{3}", precedente):
            desc = f"{precedente.strip()} {desc}".strip()
        article = {"description": desc, "quantite": None, "prix_unitaire": None, "prix": prix}
        completer_ligne(article, ligne)
        # insérée après le dernier article situé plus haut dans le PDF
        avant = [i for i, a in enumerate(data["articles"]) if positions.get(id(a), -1) < j]
        data["articles"].insert(avant[-1] + 1 if avant else 0, article)
        positions[id(article)] = j
        print(f"Correction : article oublié ajouté « {desc} » {prix} (lu sur la ligne du PDF)")


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
    utilisees, a_corriger, positions = set(), [], {}
    for a in articles:
        if not isinstance(a.get("prix"), (int, float)):
            continue
        desc = (a.get("description") or "").lower()
        # description sur la ligne des montants, ou sur la ligne juste au-dessus
        j = next((j for j, ligne, m, _ in lignes if j not in utilisees and meme_montant(m, a["prix"]) and desc
                  and (desc[:10] in ligne.lower() or desc[-10:] in ligne.lower()
                       or (j > 0 and desc[:10] in toutes[j - 1].lower()))), None)
        if j is None:
            a_corriger.append(a)
        else:
            utilisees.add(j)
            positions[id(a)] = j
            completer_ligne(a, next(ligne for k, ligne, _, _ in lignes if k == j))

    # 2e passage : les autres prennent le libellé d'une ligne d'article libre qui se termine par leur prix
    for a in a_corriger:
        trouve = False
        for j, ligne, m, libelle in lignes:
            if j in utilisees or not meme_montant(m, a["prix"]) or est_non_article(libelle):
                continue
            if re.search(r"[A-Za-zÀ-ÿ]", libelle):
                utilisees.add(j)
                positions[id(a)] = j
                print(f"Correction : description « {a.get('description')} » -> « {libelle} » (lue sur la ligne du PDF)")
                a["description"] = libelle
                completer_ligne(a, ligne)
                trouve = True
                break
        # Article à 0 qui ne correspond à aucune ligne de la facture (ex. "Looking for our W-9?")
        if not trouve and a.get("prix") == 0:
            print(f"Correction : article « {a.get('description')} » retiré (à 0, aucune ligne dans la facture)")
            articles.remove(a)

    if positions:
        ajouter_oublies(data, toutes, lignes, positions)


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
    data["fournisseur"] = nom_fournisseur(data.get("fournisseur"))
    data["date_paiement"] = normaliser_date(data.get("date_paiement"))
    # La date écrite à côté de son étiquette dans le PDF passe avant celle du modèle
    date_pdf = date_dans_texte(texte)
    if date_pdf and date_pdf != data["date_paiement"]:
        print(f"Correction : date_paiement « {data['date_paiement']} » -> « {date_pdf} » (lue dans le PDF)")
        data["date_paiement"] = date_pdf
    if date_americaine(data["date_paiement"], texte) != data["date_paiement"]:
        print(f"Correction : date_paiement « {data['date_paiement']} » -> « {date_americaine(data['date_paiement'], texte)} » "
              "(facture américaine : MM/JJ/AAAA)")
        data["date_paiement"] = date_americaine(data["date_paiement"], texte)
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
