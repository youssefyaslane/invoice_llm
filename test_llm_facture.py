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
import itertools
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
- Un crédit ou un remboursement (montant précédé de "-", "Unused time", "Credit", "Refund", "Avoir") a un prix négatif.
- Inclus aussi les articles dont le prix est 0.
- total : montant total de la facture, taxes comprises.
- Montants : nombres à point décimal, sans devise ni séparateur de milliers."""

# Valeurs exactes des factures de test (hors dépôt git) :
# {"facture": {"champs": {champ: valeur ou [valeurs acceptées]}, "articles": [[description ou null, prix], ...]}}
FICHIER_ATTENDU = BASE_DIR / "attendu.json"
ATTENDU = json.loads(FICHIER_ATTENDU.read_text(encoding="utf-8")) if FICHIER_ATTENDU.is_file() else {}


NUMERO_PAGE = re.compile(r"\bpage\s*\d+\s*(?:/|of|sur)\s*\d+\b|^\s*\d+\s*/\s*\d+\s*$", re.I)


def sans_pieds_de_page(pages):
    """Le pied de page répété en bas de chaque page (adresse, capital, "Page 1/2"...) tombe au milieu des articles :
    on le retire de chaque page et on le garde une seule fois à la fin du texte."""
    lignes = [p.splitlines() for p in pages]
    if len(lignes) < 2:
        return "\n".join(pages)
    cle = lambda l: NUMERO_PAGE.sub("", l).strip().lower()
    vues = {}
    for page in lignes:
        for l in {cle(l) for l in page[-5:]}:
            vues[l] = vues.get(l, 0) + 1
    repetees = {c for c, n in vues.items() if n >= 2 and c}
    corps, pied = [], []
    for page in lignes:
        bas = len(page) - 5
        for i, l in enumerate(page):
            if i >= bas and (cle(l) in repetees or not cle(l)):
                if cle(l) and cle(l) not in [cle(x) for x in pied]:
                    pied.append(NUMERO_PAGE.sub("", l).strip())
            else:
                corps.append(l)
    return "\n".join(corps + pied)


def extraire_texte(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        texte = sans_pieds_de_page([p.extract_text(x_tolerance=1) or "" for p in pdf.pages])
    # certains PDF insèrent un tiret "—" au milieu des mots : "Ju—n 24" -> "Jun 24", "Amou— nt" -> "Amount"
    texte = re.sub(r"(?<=\w)—\s?(?=\w)", "", texte)
    # certaines polices n'ont pas de code pour le signe moins : il sort comme "\x00" devant le montant
    return re.sub(r"\x00(?=\s?[$€£]?\s?\d)", "-", texte).replace("\x00", "")


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
    r"[\s,]+(Inc|Ltd|LLC|L\.L\.C|Limited|Corp|Corporation|Co|Pty|Plc|PBC|GmbH|SAS|SARL|SA|S\.A|SL|SLU|S\.L|S\.L\.U)\.?$", re.I)


def sans_forme_juridique(nom):
    """Nom du fournisseur sans sa forme juridique : "BitTitan, Inc." -> "BitTitan", "AG Grid Ltd." -> "AG Grid"."""
    while nom and FORME_JURIDIQUE_FIN.search(nom):
        nom = FORME_JURIDIQUE_FIN.sub("", nom).strip(" ,")
    return nom


# Nom retenu pour les fournisseurs qui facturent sous plusieurs entités (filiale, pays...)
NOMS_FOURNISSEURS = {r"^Microsoft\b": "Microsoft", r"^Adobe\b": "Adobe", r"^Freepik\b": "Magnific", r"^Paddle\b": "Paddle"}


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


SOCIETE = re.compile(r"^(.+?\b(?:Inc|Ltd|LLC|Limited|Corp|Pty|PBC|GmbH|SAS|SARL|SA|SL|SLU|Plc)\b\.?)", re.I)


def societe_collee(client, texte):
    """Colonnes client / fournisseur collées sur une ligne : "NETIS Holding Ltd Paddle.com Market Ltd".
    Sur la ligne qui contient le nom du client, la société qui suit (jusqu'à sa forme juridique) est le fournisseur."""
    for ligne in texte.splitlines():
        if client and client.lower() in ligne.lower():
            reste = ligne[ligne.lower().index(client.lower()) + len(client):].strip(" ,")
            m = SOCIETE.match(reste)
            if m and "netis" not in m[1].lower():
                return m[1]
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
        nouveau = societe_collee(client, texte) or nom_pied_de_page(texte) or nouveau
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
                       "invoice date", "billing date", "date de facture", "date d'émission", "document date", "date"]


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
        for m in re.finditer(debut + re.escape(etiquette) + r"\s*:?\s*" + DATE_TEXTE, texte, re.I | re.M):
            if texte[:m.start()].lower().endswith("next "):  # "Next Billing Date" : prochaine facture
                continue
            if re.fullmatch(r"\d{2}/\d{2}/\d{4}", normaliser_date(m[1])):
                return normaliser_date(m[1])
    return None


def norm(v):
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    return str(v or "").strip().lower()


def ecritures(v):
    """Façons d'écrire un montant dans une facture : 1,050.66 / 1050.66 / 1 050,66 / 1050,66."""
    v = abs(v)
    en = f"{v:,.2f}"
    return {en, f"{v:.2f}", en.replace(",", " ").replace(".", ","), f"{v:.2f}".replace(".", ",")}


MONTANT = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:[ \u00a0]\d{3})+,\d{2}|\d{1,3}(?:,\d{3})+\.\d{2}|\d+[.,]\d{2})(?![\dA-Za-z])")  # pas "1.92TB"
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


def bloc_au_dessus(toutes, j, n=5):
    """Lignes sans montant juste au-dessus de la ligne de montants j (titre + en-têtes, ex. Microsoft)."""
    bloc = []
    for k in range(j - 1, max(-1, j - n - 1), -1):
        if MONTANT.search(toutes[k]):
            break
        bloc.insert(0, toutes[k])
    return "\n".join(bloc)


def corriger_prix_ligne(articles, texte):
    """Le modèle a pris le prix unitaire au lieu du montant de la ligne : sur la ligne du PDF qui contient
    la description, le prix est un montant mais pas le dernier -> prix = dernier montant, prix_unitaire = ancien prix."""
    for a in articles:
        desc, prix = (a.get("description") or "").lower(), a.get("prix")
        if not desc or not isinstance(prix, (int, float)) or prix == 0:
            continue
        # déjà juste si une ligne de cet article se termine par ce prix (ex. AG Grid : 4 lignes de même description)
        toutes = texte.splitlines()
        lignes_article = [l for j, l in enumerate(toutes) if MONTANT.findall(l)
                          and (desc[:10] in l.lower() or desc[:10] in bloc_au_dessus(toutes, j).lower())]
        if any(meme_montant(MONTANT.findall(l)[-1], prix) for l in lignes_article):
            continue
        for ligne in lignes_article:
            montants = MONTANT.findall(ligne)
            if (len(montants) >= 2 and not meme_montant(montants[-1], prix)
                    and any(meme_montant(m, prix) for m in montants[:-1])):
                dernier = montants[-1]
                total_ligne = float(dernier.replace(" ", "").replace(",", ".") if "," in dernier[-3:]
                                    else dernier.replace(",", ""))
                print(f"Correction : prix {prix} -> {total_ligne} (prix unitaire pris pour le montant de la ligne)")
                a["prix_unitaire"], a["prix"] = prix, total_ligne
                if a.get("quantite") is None and prix and float(total_ligne / prix).is_integer():
                    a["quantite"] = int(total_ligne / prix)
                break


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
    elif premier and a.get("quantite") is None:
        avant = DEVISES.sub("", ligne[:premier.start()]).strip().lower()
        q = (re.search(re.escape(desc.lower()) + r"\s+(\d+)$", avant) if desc else None) or re.fullmatch(r"(\d+)", avant)
        if q:
            print(f"Correction : quantite {q[1]} (écrite après la description sur la ligne du PDF)")
            a["quantite"] = int(q[1])

    if a.get("quantite") is None and isinstance(a.get("prix"), (int, float)):
        for pu, n, mt in re.findall(r"([\d.,]+\d)\s+(\d+)\s+[$€£]?\s*([\d.,]+\d)", ligne):
            pu_v, mt_v = montant_cellule(pu), montant_cellule(mt)
            if pu_v and mt_v is not None and round(pu_v * int(n), 2) == round(mt_v, 2) == round(abs(a["prix"]), 2):
                print(f"Correction : quantite {n} et prix_unitaire {pu_v} (lus sur la ligne du PDF : {pu} x {n} = {mt})")
                a["quantite"], a["prix_unitaire"] = int(n), pu_v
                break
    q, prix = a.get("quantite"), a.get("prix")
    if a.get("prix_unitaire") is None and isinstance(q, (int, float)) and q > 0 and isinstance(prix, (int, float)):
        unitaire = round(prix / q, 2)
        if any(e in ligne for e in ecritures(unitaire)):
            print(f"Correction : prix_unitaire {unitaire} (= {prix} / {q}, écrit sur la ligne du PDF)")
            a["prix_unitaire"] = unitaire


# Mots trop courants pour reconnaître un article (colonnes, type d'abonnement, mots outils)
MOTS_GENERIQUES = {"abonnement", "installation", "location", "mois", "sans", "engagement", "avance", "paiement",
                   "référence", "quantité", "prix", "unitaire", "the", "and", "for", "des", "les", "pour", "une"}
EN_TETE = re.compile(r"^(prix|unitaire|description|qty|quantity|quantité)\b|référence|unit price", re.I)
REFERENCE = re.compile(r"\S*\.ip-\S*|\b\d{1,3}-\d{1,3}-\S*|\b\d+\.eu\b")  # "ns123456.ip-10-20-", "30.eu"


def mots_article(desc):
    return {m for m in re.findall(r"[a-z0-9][\w.+-]*", desc.lower()) if len(m) >= 3 and m not in MOTS_GENERIQUES}


def proximite(desc, toutes, j):
    """Part des mots de la description présents autour de la ligne j (3 lignes au-dessus, 2 en dessous) ;
    1 si la description est sur la ligne ou juste au-dessus (cas simple)."""
    desc, ligne = desc.lower(), toutes[j].lower()
    if desc and (desc[:10] in ligne or desc[-10:] in ligne or (j > 0 and desc[:10] in toutes[j - 1].lower())):
        return 1.0
    # la ligne porte son propre nom ("Networking 383.28 383.28") et ce n'est pas la description : autre article
    premier = MONTANT.search(toutes[j])
    nom_ligne = REFERENCE.sub("", DEVISES.sub("", toutes[j][:premier.start()] if premier else ""))
    if re.search(r"[A-Za-zÀ-ÿ]{3}", nom_ligne) and not est_non_article(nom_ligne):
        return 0.0
    mots = mots_article(desc)
    fenetre = " ".join(toutes[max(0, j - 4):j + 3]).lower()
    return sum(m in fenetre for m in mots) / len(mots) if mots else 0.0


def forme(ligne):
    """Forme d'une ligne d'article : nombre de montants, et présence d'une quantité entière avant le 1er montant."""
    premier, montants = MONTANT.search(ligne), MONTANT.findall(ligne)
    avant = DEVISES.sub("", ligne[:premier.start()]) if premier else ""
    quantite_collee = len(montants) >= 2 and " " in montants[0] and montants[0].split(" ", 1)[1] == montants[-1]
    return len(montants), bool(re.search(r"\s\d+\s*$", " " + avant)) or quantite_collee


def nom_au_dessus(toutes, j):
    """Lignes de texte au-dessus de la ligne de montants j qui forment le nom de l'article (OVH : jusqu'à 3 lignes).
    S'arrête à l'en-tête du tableau, à "Sans engagement", et à la suite de l'article précédent (ligne juste
    sous ses montants, ex. "données gra - 230.eu")."""
    dessus = []
    for k in range(j - 1, max(-1, j - 4), -1):
        precedente = toutes[k].strip()
        if (MONTANT.search(precedente) or est_non_article(precedente) or EN_TETE.search(precedente)
                or precedente.lower().startswith("sans engagement") or (k > 0 and MONTANT.search(toutes[k - 1]))):
            break
        if re.search(r"[A-Za-zÀ-ÿ]{3}", REFERENCE.sub("", precedente)):
            dessus.insert(0, precedente)
    return dessus


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
        desc = re.sub(r"(^|\s)\d+$", "", libelle).strip()
        desc = re.sub(r"\s+", " ", REFERENCE.sub("", " ".join(nom_au_dessus(toutes, j) + [desc]))).strip(" -")
        article = {"description": desc, "quantite": None, "prix_unitaire": None, "prix": prix}
        completer_ligne(article, ligne)
        # insérée après le dernier article situé plus haut dans le PDF
        avant = [i for i, a in enumerate(data["articles"]) if positions.get(id(a), -1) < j]
        data["articles"].insert(avant[-1] + 1 if avant else 0, article)
        positions[id(article)] = j
        print(f"Correction : article oublié ajouté « {desc} » {prix} (lu sur la ligne du PDF)")


def corriger_signes(data):
    """Crédit dont le signe moins a été perdu : si la somme des articles ne vaut le total qu'en passant
    certains prix en négatif (ex. 30.00 - 10.00 = 20.00), ces articles sont des crédits."""
    articles, total = data.get("articles") or [], data.get("total")
    prix = [a.get("prix") for a in articles]
    if (not isinstance(total, (int, float)) or not 1 < len(prix) <= 10
            or not all(isinstance(p, (int, float)) for p in prix) or round(sum(prix), 2) == round(total, 2)):
        return
    for n in range(1, len(prix)):
        for credits in itertools.combinations(range(len(prix)), n):
            if all(prix[i] > 0 for i in credits) and \
                    round(sum(-p if i in credits else p for i, p in enumerate(prix)), 2) == round(total, 2):
                for i in credits:
                    print(f"Correction : article « {articles[i].get('description')} » {prix[i]} -> {-prix[i]} "
                          "(crédit : la somme des articles ne tombe juste qu'avec ce prix négatif)")
                    articles[i]["prix"] = -prix[i]
                    if isinstance(articles[i].get("prix_unitaire"), (int, float)):
                        articles[i]["prix_unitaire"] = -abs(articles[i]["prix_unitaire"])
                return


RUBRIQUE = re.compile(r"^\s*Rubrique\s+(?:divers\s+)?(.+)$", re.I)
TYPE_TABLEAU = re.compile(r"^\s*(\w+)\s+(?:Référence|Reference)\b", re.I)


def completer_descriptions(articles, toutes, positions):
    for a in articles:
        desc, j = (a.get("description") or "").strip(), positions.get(id(a))
        premier = desc.split(" ", 1)[0].lower()
        if premier in ("référence", "reference", "description", "item") and " " in desc:
            a["description"] = desc = desc.split(" ", 1)[1].strip()
            premier = desc.split(" ", 1)[0].lower()
        if j is not None and premier in MOTS_GENERIQUES - {"abonnement", "installation"}:
            dessus = nom_au_dessus(toutes, j)
            if dessus:
                nouveau = re.sub(r"\s+", " ", REFERENCE.sub("", " ".join(dessus + [desc]))).strip(" -")
                print(f"Correction : description « {desc} » -> « {nouveau} » (nom lu au-dessus dans le PDF)")
                a["description"] = nouveau


def ajouter_sections(articles, toutes, positions):
    """Section de l'article d'après les titres au-dessus de sa ligne : "Rubrique divers DEDICATED-OPTION" puis
    l'en-tête "Installation Référence Quantité Prix HT" -> section "DEDICATED-OPTION - Installation"."""
    types = {TYPE_TABLEAU.match(l)[1].lower() for l in toutes if TYPE_TABLEAU.match(l)}
    for a in articles:
        j = positions.get(id(a))
        if j is None:
            continue
        rubrique = type_ = None
        for l in reversed(toutes[:j]):
            if type_ is None and TYPE_TABLEAU.match(l):
                type_ = TYPE_TABLEAU.match(l)[1].capitalize()
            if RUBRIQUE.match(l):
                rubrique = RUBRIQUE.match(l)[1].strip()
                break
        section = " - ".join(x for x in (rubrique, type_) if x)
        if not section:
            continue
        a["section"] = section
        # le type (lu dans le PDF) est dans la section : "Abonnement 32GB DDR5 ..." -> "32GB DDR5 ..."
        premier = (a.get("description") or "").split(" ", 1)
        if type_ and len(premier) == 2 and premier[0].lower() in types:
            a["description"] = premier[1].strip()


def retirer_excedent(data, toutes, positions):
    """Article en double (ex. Azure : "Usage Charges - Microsoft Azure Standard" ET "Storage" au même prix) :
    la somme dépasse le total exactement du prix d'un article qui a un jumeau au même prix -> on le retire,
    de préférence celui dont la description est un titre de section du PDF, sinon celui sans ligne dans le PDF."""
    articles, total = data.get("articles") or [], data.get("total")
    titres = {l.strip().lower() for l in toutes if not MONTANT.search(l)}
    # Tous les articles nommés par un titre de section et qui ont un jumeau au même prix sous leur vrai nom
    # (Azure : "Usage Charges..." + Storage, Networking, Compute) : retirés ensemble si la somme tombe alors juste
    titres_doubles = [a for a in articles if (a.get("description") or "").strip().lower() in titres and a.get("prix")
                      and any(b is not a and norm(b.get("prix")) == norm(a["prix"]) for b in articles)]
    reste = round(sum(a.get("prix") or 0 for a in articles if a not in titres_doubles), 2)
    if titres_doubles and isinstance(total, (int, float)) and reste == round(total, 2):
        for a in titres_doubles:
            print(f"Correction : article « {a.get('description')} » {a['prix']} retiré (titre de section en double)")
            articles.remove(a)
    while isinstance(total, (int, float)) and articles:
        excedent = round(sum(a.get("prix") or 0 for a in articles) - total, 2)
        jumeaux = [a for a in articles if a.get("prix") and round(a["prix"], 2) == excedent
                   and sum(round(b.get("prix") or 0, 2) == excedent for b in articles) >= 2]
        if excedent <= 0 or not jumeaux:
            return
        a = (next((a for a in jumeaux if (a.get("description") or "").strip().lower() in titres), None)
             or next((a for a in jumeaux if id(a) not in positions), None))
        if a is None:
            return
        print(f"Correction : article « {a.get('description')} » {a['prix']} retiré (en double : la somme dépassait le total)")
        articles.remove(a)


def corriger_articles(data, texte):
    """Corrige les articles avec le texte du PDF : retire les lignes de taxe/total et, si la description
    n'est pas sur la ligne du PDF qui se termine par le prix, la remplace par le libellé de cette ligne."""
    articles = []
    for a in data.get("articles") or []:
        if est_non_article(a.get("description") or ""):
            print(f"Correction : article « {a.get('description')} » retiré (taxe ou total)")
        elif "netis" in (a.get("description") or "").lower():
            print(f"Correction : article « {a.get('description')} » retiré (nom du client : ligne récapitulative)")
        else:
            articles.append(a)
    corriger_prix_ligne(articles, texte)
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
        # ligne libre au bon montant dont l'entourage contient le plus de mots de la description (au moins 60 %)
        scores = [(proximite(a.get("description") or "", toutes, j), -j) for j, _, m, libelle in lignes
                  if j not in utilisees and meme_montant(m, a["prix"]) and not est_non_article(libelle)
                  and not MOTS_TOTAUX.search(libelle)]
        meilleur = max(scores, default=(0, 0))
        j = -meilleur[1] if meilleur[0] >= 0.6 else None
        if j is None:
            a_corriger.append(a)
        else:
            utilisees.add(j)
            positions[id(a)] = j
            completer_ligne(a, next(ligne for k, ligne, _, _ in lignes if k == j))

    # 2e passage : les autres prennent le libellé d'une ligne d'article libre qui se termine par leur prix
    for a in a_corriger:
        trouve = False
        # description du modèle écrite dans le PDF (titre de section) : la ligne de l'article est en dessous
        desc = (a.get("description") or "").lower()[:10]
        debut = next((k for k, l in enumerate(toutes) if desc and desc in l.lower()), -1)
        for j, ligne, m, libelle in lignes:
            if (j in utilisees or j <= debut or not meme_montant(m, a["prix"]) or est_non_article(libelle)
                    or "netis" in libelle.lower()):
                continue
            if re.search(r"[A-Za-zÀ-ÿ]", libelle) and len(MONTANT.findall(ligne)) >= 2 and a["prix"] != 0:
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
        # Doublon : aucune ligne libre dans le PDF et identique à un autre article (même description, même prix)
        elif not trouve and any(b is not a and norm(b.get("description")) == norm(a.get("description"))
                                and norm(b.get("prix")) == norm(a.get("prix")) for b in articles):
            print(f"Correction : article « {a.get('description')} » {a.get('prix')} retiré (doublon)")
            articles.remove(a)

    if positions:
        ajouter_oublies(data, toutes, lignes, positions)
    corriger_signes(data)
    retirer_excedent(data, toutes, positions)
    total = data.get("total")
    for a in list(articles):
        autres = round(sum(b.get("prix") or 0 for b in articles if b is not a), 2)
        if len(articles) > 1 and a.get("prix") and total and a["prix"] == total and autres == round(total, 2):
            print(f"Correction : article « {a.get('description')} » {a['prix']} retiré (récapitulatif = total)")
            articles.remove(a)
    completer_descriptions(articles, toutes, positions)
    ajouter_sections(articles, toutes, positions)


def verifier(data, texte):
    """Résultat de chaque contrôle : "OK" ou le problème trouvé."""
    articles = data.get("articles") or []
    somme = round(sum(a.get("prix") or 0 for a in articles), 2)
    total = data.get("total") or 0
    # Un montant absent du texte du PDF a été inventé ou calculé par le modèle
    montants = [("total", total)] + [(f"article {i + 1}", a.get("prix")) for i, a in enumerate(articles)]
    absents = [f"{nom} = {v}" for nom, v in montants
               if isinstance(v, (int, float)) and not any(e in texte for e in ecritures(v))]
    return {
        "somme articles = total": "OK" if somme == total else f"ÉCART ({somme} vs {total}, normal si la facture a des taxes)",
        "fournisseur différent de NETIS": "ERREUR (NETIS est le client)" if "netis" in str(data.get("fournisseur")).lower() else "OK",
        "montants présents dans la facture": "OK" if not absents else "INTROUVABLES : " + ", ".join(absents),
    }


def controles(data, texte):
    print()
    for nom, resultat in verifier(data, texte).items():
        print(f"Contrôle {nom} :", resultat)


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


def montant_cellule(cellule):
    """Montant d'une cellule de tableau ("1 234,56 €", "-$5.00") ou None."""
    c = DEVISES.sub("", cellule or "").replace("\n", " ").strip()
    signe, c = (-1, c[1:].strip()) if c.startswith("-") else (1, c)
    if not MONTANT.fullmatch(c):
        return None
    return signe * float(c.replace(" ", "").replace("\u00a0", "").replace(",", ".") if "," in c[-3:] else c.replace(",", ""))


def articles_tableau(pdf_path):
    """Articles lus cellule par cellule dans les tableaux du PDF (factures OVH : le nom d'un article tient sur
    plusieurs lignes dans sa cellule, mélangées avec la colonne Référence dans le texte simple).
    Ligne d'article = un nom, puis au moins 2 cellules dont une quantité ou 2 montants, la dernière étant le prix.
    Section = rubrique ("Rubrique divers DEDICATED-OPTION") + titre de la colonne du nom ("Installation")."""
    articles, rubrique, type_ = [], None, None
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for tableau in page.find_tables():
                for ligne in tableau.extract():
                    cellules = [c.strip() for c in ligne if c and c.strip()]
                    if not cellules:
                        continue
                    nom = re.sub(r"\s+", " ", cellules[0])
                    montants = [montant_cellule(c) for c in cellules[1:]]
                    est_article = (len(cellules) >= 3 and montants[-1] is not None and re.search(r"[A-Za-zÀ-ÿ]", nom)
                                   and (any(re.fullmatch(r"\d+", c) for c in cellules[1:])
                                        or sum(m is not None for m in montants) >= 2))
                    if not est_article:
                        for l in "\n".join(cellules).split("\n"):  # titre de rubrique, parfois au bout d'une cellule
                            if RUBRIQUE.match(l):
                                rubrique, type_ = RUBRIQUE.match(l)[1].strip(), None
                        if len(cellules) >= 3 and re.search(r"quantit|qty|prix|price|amount", " ".join(cellules[1:]), re.I):
                            type_ = cellules[0].split("\n")[0].strip()  # en-tête : "Installation | Référence | ..."
                        continue
                    if est_non_article(nom) or MOTS_TOTAUX.search(nom):
                        continue
                    prix = [m for m in montants if m is not None]
                    articles.append({"description": nom,
                                     "quantite": next((int(c) for c in cellules[1:] if re.fullmatch(r"\d+", c)), None),
                                     "prix_unitaire": prix[-2] if len(prix) >= 2 else None, "prix": prix[-1],
                                     "section": " - ".join(x for x in (rubrique, type_) if x) or None})
    return articles


def extraire_facture(pdf, url, modele, texte=None):
    """Extrait une facture : texte du PDF, réponse du LLM, puis corrections avec le texte du PDF.
    Renvoie (texte, données, réponse Ollama, durée de l'appel principal en secondes)."""
    texte = texte if texte is not None else extraire_texte(pdf)
    t0 = time.time()
    messages = [{"role": "system", "content": SYSTEME}, {"role": "user", "content": texte}]
    data, rep = appeler_ollama(url, modele, messages)
    duree = time.time() - t0
    corriger_fournisseur(url, modele, messages, rep, data)
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
    # Tableau d'articles lisible dans le PDF et dont la somme tombe juste : il remplace les articles du modèle
    tableau = articles_tableau(pdf)
    if tableau and round(sum(a["prix"] for a in tableau), 2) == round(data.get("total") or 0, 2):
        print(f"Correction : {len(tableau)} articles lus directement dans le tableau du PDF (somme = total)")
        data["articles"] = tableau
    return texte, data, rep, duree


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", default=BASE_DIR / "Data" / "INV-55868.pdf", type=Path)
    ap.add_argument("-m", "--modele", default=os.environ.get("MODELE", "qwen2.5:3b"))
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    args = ap.parse_args()

    texte = extraire_texte(args.pdf)
    print(f"Facture : {args.pdf.name}  ({len(texte)} caractères)  —  modèle : {args.modele}")
    texte, data, rep, duree = extraire_facture(args.pdf, args.url, args.modele, texte)
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
