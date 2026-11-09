"""Application web locale (Flask) : on dépose des factures PDF (glisser-déposer), chacune est lue par Docling puis par
le LLM local (Ollama, dans Docker), et le résultat s'affiche dans un tableau modifiable, à enregistrer ou à exporter
en Excel. Même prompt simple et même schéma que test/llm_local/test_llm_ollama.py.

Aucun masquage : rien ne quitte le serveur. Application autonome : n'importe aucun autre fichier du projet
(seulement ses propres templates/index.html, static/style.css et static/app.js).
Les factures sont traitées une par une (le LLM occupe tout le processeur) : les suivantes attendent leur tour.
Fichiers : Output/app/<date>_<facture>.json (le résultat, avec les corrections enregistrées) ; le PDF déposé attend dans
Output/app jusqu'au clic sur « Enregistrer », puis est rangé d'après sa date de paiement :
app_invoice/Data/<année>/<MM-MOIS>/<facture>.pdf (dossiers créés au besoin). Sans date valable, il reste dans Output/app.
Base PostgreSQL (si app_invoice/.env la décrit, voir .env.example) : une ligne par facture dans <table> et une ligne
par article dans <table>_articles (liée par facture_id), écrites à la fin de l'extraction puis mises à jour à chaque
enregistrement ; schéma et tables créés s'ils n'existent pas, colonnes manquantes ajoutées s'ils existent. Jamais deux lignes avec le même numero_facture : si une facture enregistrée a déjà ce numéro,
la nouvelle n'est ni écrite en base ni rangée dans Data (signalée « Doublon » dans la page).

Production (Docker : Ollama + application avec gunicorn, depuis le dossier du projet) :
    docker compose -f app_invoice/docker-compose.yml up -d --build                -> http://localhost:6001
Développement (serveur Flask, avec l'environnement Docling ; Ollama doit tourner) :
    .venv-docling/bin/python app_invoice/app.py                    -> http://localhost:8000
    .venv-docling/bin/python app_invoice/app.py -m gemma3:4b --port 8000         -> autre modèle (défaut : gemma4:e2b)
"""
import argparse
import io
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ContentLayer
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request, send_file
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from psycopg import connect, sql

PROJET = Path(__file__).resolve().parents[1]  # dossier facture/ : Output
SORTIE = PROJET / "Output" / "app"
DATA = Path(__file__).resolve().parent / "Data"  # PDF rangés par date de paiement : Data/2026/01-JAN/...
load_dotenv(Path(__file__).resolve().parent / ".env")  # base PostgreSQL (POSTGRES_...) ; avec Docker : env_file
MOIS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
logging.getLogger("docling").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------------------------------------------
# Champs à extraire (identiques à test_llm_ollama.py) : nom -> (type, explication donnée au modèle)
# Le tableau de la page est construit à partir d'eux : rien à changer dans la page pour d'autres champs.
# ---------------------------------------------------------------------------------------------------------------
TYPE_DOCUMENT = "une facture"
CHAMPS = {
    "fournisseur": ("texte", "société qui émet la facture et reçoit le paiement (NETIS Holding est le client, jamais le"
                             "fournisseur), sans forme juridique (Inc, Ltd, LLC, SAS...)"),
    "numero_facture": ("texte?", "numéro de la facture (Invoice number, N° de facture, Référence de la "
                                 "facture...) tel qu'écrit ; pas un numéro de commande, de client, de contrat ni de "
                                 "paiement"),
    "date_paiement": ("texte", "date de paiement, sinon date d'échéance, sinon date de la facture jamais une date de début ou de fin de période d’abonnement , au format JJ/MM/AAAA"),
    "devise": ("texte", "code ISO à 3 lettres (USD, EUR, MAD...)"),
    "taxe": ("nombre?", "montant total des taxes (TVA, VAT, sales tax...) écrit sur le document, sinon null"),
    "total": ("nombre", "montant total écrit sur le document, taxes comprises"),
}
LISTE = ("articles", "une entrée par ligne d'article des tableaux, dans l'ordre ; pas les sous-totaux, taxes, totaux "
                     "ni paiements", {
    "description": ("texte", "nom de l'article tel qu'écrit"),
    "quantite": ("nombre?", "quantité écrite, sinon null"),
    "prix_unitaire": ("nombre?", "prix unitaire écrit, sinon null"),
    "prix": ("nombre", "montant de la ligne (négatif s'il est écrit avec un signe -)"),
})
TYPES_JSON = {"texte": {"type": "string"}, "nombre": {"type": "number"},
              "texte?": {"type": ["string", "null"]}, "nombre?": {"type": ["number", "null"]}}


def construire_schema():
    proprietes = {nom: TYPES_JSON[t] for nom, (t, _) in CHAMPS.items()}
    nom, _, champs = LISTE
    proprietes[nom] = {"type": "array", "items": {"type": "object",
                                                   "properties": {n: TYPES_JSON[t] for n, (t, _) in champs.items()},
                                                   "required": list(champs)}}
    return {"type": "object", "properties": proprietes, "required": list(proprietes)}


def construire_prompt():
    lignes = [f"Tu extrais les données d'un document : {TYPE_DOCUMENT}. Il est donné en Markdown : les tableaux sont "
              "écrits avec des colonnes | ... |.",
              "Remplis le JSON uniquement avec ce qui est écrit dans le document : recopie les textes et les nombres, "
              "ne calcule rien, n'invente rien. Une information absente vaut null.",
              "Montants : nombres à point décimal, sans devise ni séparateur de milliers (\"1 234,56 €\" = 1234.56).",
              ""]
    lignes += [f"- {nom} : {explication}" for nom, (_, explication) in CHAMPS.items()]
    nom, explication, champs = LISTE
    lignes.append(f"- {nom} : {explication}")
    lignes += [f"  - {n} : {e}" for n, (_, e) in champs.items()]
    return "\n".join(lignes)


# ---------------------------------------------------------------------------------------------------------------
# Lecture du PDF (Docling) et appel du LLM local (Ollama)
# ---------------------------------------------------------------------------------------------------------------
def convertisseur():
    options = PdfPipelineOptions()
    options.do_ocr = False                   # PDF numériques : le texte est déjà dans le fichier
    options.do_table_structure = True        # vrais tableaux (cellules sur plusieurs lignes, colonnes nommées)
    options.generate_picture_images = False  # pas de logos en base64 dans le texte
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def pdf_en_markdown(conv, pdf):
    # tableaux compacts : moins de tokens à lire ; en-têtes et pieds de page gardés : le nom du fournisseur n'est
    # parfois écrit que là (OVH, mentions légales)
    return conv.convert(str(pdf)).document.export_to_markdown(
        compact_tables=True, escape_html=False, escape_underscores=False,
        included_content_layers={ContentLayer.BODY, ContentLayer.FURNITURE})


def appeler_ollama(url, modele, prompt, schema, texte):
    corps = {
        "model": modele,
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": texte}],
        "format": schema,          # réponse forcée au format du schéma
        "stream": False,
        "think": False,            # pas de raisonnement écrit avant la réponse (trop lent sur CPU)
        "keep_alive": "30m",       # le modèle reste chargé entre deux factures
        "options": {"temperature": 0, "num_ctx": 8192},
    }
    req = urllib.request.Request(f"{url}/api/chat", data=json.dumps(corps).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            rep = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Erreur d'Ollama ({e.code}) : {e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama injoignable sur {url} ({e.reason}) : le conteneur ollama tourne-t-il ?")
    return json.loads(rep["message"]["content"])


# ---------------------------------------------------------------------------------------------------------------
# File d'attente : les factures sont traitées une par une, en arrière-plan
# ---------------------------------------------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 Mo par envoi
factures = {}                # id -> facture déposée (statut, résultat...), dans l'ordre de dépôt
file_attente = queue.Queue()
reglages = {"modele": "gemma4:e2b", "url": "http://localhost:11434"}


def travailleur():
    conv, prompt, schema = convertisseur(), construire_prompt(), construire_schema()
    while True:
        f = file_attente.get()
        f["debut"] = time.time()
        try:
            f["statut"] = "lecture"
            texte = pdf_en_markdown(conv, f["pdf"])                                       # 1. Docling
            f["statut"] = "llm"
            f["resultat"] = appeler_ollama(reglages["url"], reglages["modele"], prompt, schema, texte)  # 2. LLM
            f["statut"] = "fini"
            enregistrer(f)
            f["doublon"] = doublon(f)
            if not f["doublon"]:
                f["erreur_bd"] = sauvegarder_bd(f)
        except Exception as e:
            f["statut"], f["erreur"] = "erreur", str(e) or e.__class__.__name__
        f["fin"] = time.time()


def enregistrer(f):
    f["json"].write_text(json.dumps(f["resultat"], indent=2, ensure_ascii=False), encoding="utf-8")


def date_valable(date):
    """JJ/MM/AAAA (ou AAAA-MM-JJ) -> date ; None si la date n'est pas valable."""
    texte = str(date or "").strip()
    for format_ in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texte, format_).date()
        except ValueError:
            pass
    return None


def dossier_paiement(date):
    """JJ/MM/AAAA (ou AAAA-MM-JJ) -> Data/AAAA/MM-MOIS ; None si la date n'est pas valable."""
    d = date_valable(date)
    return DATA / str(d.year) / f"{d.month:02d}-{MOIS[d.month - 1]}" if d else None


def classer(f):
    """Range le PDF dans le dossier de sa date de paiement (et le déplace si la date est corrigée)."""
    dossier = dossier_paiement((f["resultat"] or {}).get("date_paiement"))
    if dossier is None or f["pdf"].parent == dossier:
        return
    dossier.mkdir(parents=True, exist_ok=True)
    nom = re.sub(r'[^\w.-]+', '_', Path(f["nom"]).stem)[:80] or "facture"
    cible, n = dossier / f"{nom}.pdf", 2
    while cible.exists():                     # même nom déjà rangé ce mois-ci : _2, _3...
        cible, n = dossier / f"{nom}_{n}.pdf", n + 1
    f["pdf"] = Path(shutil.move(f["pdf"], cible))


# ---------------------------------------------------------------------------------------------------------------
# Base PostgreSQL : une ligne par facture (réglages POSTGRES_... dans app_invoice/.env)
# ---------------------------------------------------------------------------------------------------------------
TYPES_SQL = {"texte": "text", "texte?": "text", "nombre": "numeric", "nombre?": "numeric"}
etat_bd = {"prete": False}


def bd_active():
    return bool(os.environ.get("POSTGRES_USER"))


def connexion():
    return connect(host=os.environ.get("POSTGRES_HOST", "localhost"), port=os.environ.get("POSTGRES_PORT", "5432"),
                   dbname=os.environ.get("POSTGRES_DB", "postgres"), user=os.environ["POSTGRES_USER"],
                   password=os.environ.get("POSTGRES_PASSWORD", ""), connect_timeout=5)


def schema_table():
    return os.environ.get("POSTGRES_SCHEMA") or "public", os.environ.get("POSTGRES_TABLE") or "factures"


def table_articles():
    """(schéma, table) des articles : <table>_articles, dans le même schéma."""
    schema, table = schema_table()
    return schema, f"{table}_{LISTE[0]}"


def colonnes_sql():
    """Colonnes de la table des factures : les champs extraits (date_paiement en vrai type date) et le suivi."""
    colonnes = {"id": "text", "fichier": "text"}
    colonnes |= {nom: "date" if nom == "date_paiement" else TYPES_SQL[t] for nom, (t, _) in CHAMPS.items()}
    colonnes |= {"modele": "text", "pdf": "text", "enregistree": "boolean", "modifie": "boolean",
                 "cree_le": "timestamptz default now()", "maj_le": "timestamptz default now()"}
    return colonnes


def colonnes_articles():
    """Colonnes de la table des articles : la facture (supprimée avec elle), le n° de ligne, les champs de la liste."""
    colonnes = {"facture_id": sql.SQL("text references {} (id) on delete cascade").format(
                    sql.Identifier(*schema_table())),
                "ligne": "integer"}
    return colonnes | {n: TYPES_SQL[t] for n, (t, _) in LISTE[2].items()}


def creer_ou_completer(c, schema, table, colonnes, contraintes=""):
    """Crée la table si elle n'existe pas ; sinon ajoute seulement les colonnes qui manquent."""
    nom = sql.Identifier(schema, table)
    type_ = lambda t: t if isinstance(t, sql.Composable) else sql.SQL(t)
    existantes = {r[0] for r in c.execute("SELECT column_name FROM information_schema.columns "
                                          "WHERE table_schema = %s AND table_name = %s", [schema, table])}
    if not existantes:
        definitions = [sql.SQL("{} {}").format(sql.Identifier(n), type_(t)) for n, t in colonnes.items()]
        c.execute(sql.SQL("CREATE TABLE {} ({})").format(
            nom, sql.SQL(", ").join(definitions + ([sql.SQL(contraintes)] if contraintes else []))))
    for n, t in colonnes.items():
        if existantes and n not in existantes:
            c.execute(sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(nom, sql.Identifier(n), type_(t)))


def preparer_table():
    """Crée le schéma et les tables s'ils n'existent pas ; sinon ajoute seulement les colonnes qui manquent.
    Chaque CREATE / ALTER n'est lancé que si besoin : un utilisateur qui a seulement le droit d'écrire suffit."""
    schema, table = schema_table()
    nom = sql.Identifier(schema, table)
    with connexion() as c:
        if not c.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", [schema]).fetchone():
            c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        creer_ou_completer(c, schema, table, colonnes_sql())
        # une facture = une ligne : index unique sur id (pour mettre la ligne à jour à chaque enregistrement)
        unique = c.execute("SELECT 1 FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                           "AND a.attnum = i.indkey[0] WHERE i.indrelid = %s::regclass AND i.indisunique "
                           "AND i.indnatts = 1 AND a.attname = 'id'", [f'"{schema}"."{table}"']).fetchone()
        if not unique:
            c.execute(sql.SQL("CREATE UNIQUE INDEX {} ON {} (id)").format(sql.Identifier(f"{table}_id_unique"), nom))
        creer_ou_completer(c, *table_articles(), colonnes_articles(), "primary key (facture_id, ligne)")
    etat_bd["prete"] = True


def numero_normalise(f):
    """numero_facture sans espaces, en majuscules ; None s'il est vide ou vaut N/A."""
    numero = str((f["resultat"] or {}).get("numero_facture") or "").strip()
    return None if not numero or valeur_manquante(numero, "texte?") else re.sub(r"\s+", "", numero).upper()


def doublon(f):
    """Une autre facture déjà enregistrée en base a-t-elle le même numéro ? Message à afficher, sinon None."""
    numero = numero_normalise(f)
    if not bd_active() or not numero:
        return None
    try:
        if not etat_bd["prete"]:
            preparer_table()
        with connexion() as c:
            ligne = c.execute(sql.SQL("SELECT fichier, maj_le FROM {} WHERE id <> %s AND enregistree AND "
                                      "upper(regexp_replace(numero_facture, '\\s+', '', 'g')) = %s LIMIT 1")
                              .format(sql.Identifier(*schema_table())), [f["id"], numero]).fetchone()
    except Exception:
        return None  # base injoignable : l'erreur sera affichée à l'enregistrement
    if not ligne:
        return None
    return (f"Doublon : la facture n° {f['resultat']['numero_facture']} est déjà en base ({ligne[0]}, enregistrée le "
            f"{ligne[1]:%d/%m/%Y})")


def nombre(valeur):
    try:
        return None if valeur in (None, "") else float(valeur)
    except (TypeError, ValueError):
        return None


def valeur_sql(valeur, type_):
    return nombre(valeur) if type_.startswith("nombre") else None if valeur is None else str(valeur)


def sauvegarder_bd(f):
    """Écrit (ou met à jour) la ligne de la facture et remplace ses articles. Renvoie le message d'erreur, ou None.
    Une erreur de la base n'empêche ni l'extraction ni l'enregistrement des fichiers.
    Une ligne jamais enregistrée (dépôt abandonné) avec le même numéro est remplacée par celle-ci."""
    if not bd_active():
        return None
    resultat = f["resultat"] or {}
    valeurs = {"id": f["id"], "fichier": f["nom"]}
    for nom, (t, _) in CHAMPS.items():
        v = resultat.get(nom)
        valeurs[nom] = date_valable(v) if nom == "date_paiement" else valeur_sql(v, t)
    valeurs |= {"modele": reglages["modele"], "pdf": str(f["pdf"].relative_to(PROJET)), "enregistree": f["enregistree"], "modifie": f["modifie"]}
    requete = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (id) DO UPDATE SET {}, maj_le = now()").format(
        sql.Identifier(*schema_table()), sql.SQL(", ").join(map(sql.Identifier, valeurs)),
        sql.SQL(", ").join(sql.Placeholder() * len(valeurs)),
        sql.SQL(", ").join(sql.SQL("{0} = excluded.{0}").format(sql.Identifier(c)) for c in list(valeurs)[1:]))
    nom_liste, _, champs_liste = LISTE
    articles = [[f["id"], i, *[valeur_sql(a.get(n), t) for n, (t, _) in champs_liste.items()]]
                for i, a in enumerate(resultat.get(nom_liste) or [], 1)]
    colonnes = ["facture_id", "ligne", *champs_liste]
    ajout_articles = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(*table_articles()), sql.SQL(", ").join(map(sql.Identifier, colonnes)),
        sql.SQL(", ").join(sql.Placeholder() * len(colonnes)))
    try:
        if not etat_bd["prete"]:
            preparer_table()
        with connexion() as c:
            if numero_normalise(f):
                c.execute(sql.SQL("DELETE FROM {} WHERE id <> %s AND NOT coalesce(enregistree, false) AND "
                                  "upper(regexp_replace(numero_facture, '\\s+', '', 'g')) = %s")
                          .format(sql.Identifier(*schema_table())), [f["id"], numero_normalise(f)])
            c.execute(requete, list(valeurs.values()))
            # articles : ceux du tableau tel qu'enregistré remplacent les précédents
            c.execute(sql.SQL("DELETE FROM {} WHERE facture_id = %s").format(sql.Identifier(*table_articles())),
                      [f["id"]])
            if articles:
                c.cursor().executemany(ajout_articles, articles)
        return None
    except Exception as e:
        message = f"Base PostgreSQL : {str(e).strip() or e.__class__.__name__}"
        print(f"⚠ {f['nom']} : {message}", flush=True)
        return message


def public(f):
    """Ce que la page reçoit pour une facture."""
    attente = [g["id"] for g in factures.values() if g["statut"] == "attente"]
    return {"id": f["id"], "nom": f["nom"], "statut": f["statut"], "resultat": f["resultat"], "erreur": f["erreur"],
            "modifie": f["modifie"], "enregistree": f["enregistree"], "erreur_bd": f["erreur_bd"],
            "doublon": f["doublon"],
            "dossier": str(f["pdf"].parent.relative_to(DATA.parent)) if f["pdf"].is_relative_to(DATA) else None,
            "rang": attente.index(f["id"]) + 1 if f["id"] in attente else None,
            "duree": round((f["fin"] or time.time()) - f["debut"]) if f["debut"] else None}


def valeur_manquante(valeur, type_):
    """« N/A » (ce que le modèle écrit quand il ne trouve rien), ou champ obligatoire vide."""
    texte = str(valeur if valeur is not None else "").strip()
    return re.fullmatch(r"n/?a|null|none|unknown|inconnu|-", texte, re.I) or (not texte and not type_.endswith("?"))


def classeur_excel(liste):
    """Une feuille des factures et une feuille des lignes (articles) ; en rouge si ni la somme des lignes, ni
    la somme des lignes + taxe ne font le total, si le fournisseur est NETIS ou si un champ vaut N/A (ou est vide)."""
    nom_liste, _, champs_liste = LISTE
    classeur = Workbook()
    feuille = classeur.active
    feuille.title = "Factures"
    feuille.append(["Fichier", *CHAMPS, f"Somme des {nom_liste}", "Contrôle"])
    lignes = classeur.create_sheet(nom_liste.capitalize())
    lignes.append(["Fichier", *champs_liste])
    rouge = PatternFill("solid", fgColor="F8D7DA")
    for f in liste:
        donnees = f.get("donnees") or {}
        articles = donnees.get(nom_liste) or []
        somme = round(sum(a.get("prix") or 0 for a in articles), 2)
        total, taxe = donnees.get("total"), donnees.get("taxe") or 0
        ok = total is None or abs(somme - total) < 0.01 or abs(somme + taxe - total) < 0.01  # lignes TTC ou HT
        ok = ok and "netis" not in str(donnees.get("fournisseur") or "").lower()  # NETIS est le client
        ok = ok and not any(valeur_manquante(donnees.get(c), t) for c, (t, _) in CHAMPS.items())
        feuille.append([f["nom"], *[donnees.get(c) for c in CHAMPS], somme, "OK" if ok else "À vérifier"])
        if not ok:
            for cellule in feuille[feuille.max_row]:
                cellule.fill = rouge
        for a in articles:
            lignes.append([f["nom"], *[a.get(c) for c in champs_liste]])
    for f in (feuille, lignes):
        for cellule in f[1]:
            cellule.font = Font(bold=True)
        for colonne in f.columns:
            largeur = max(len(str(c.value or "")) for c in colonne)
            f.column_dimensions[colonne[0].column_letter].width = min(60, max(10, largeur + 2))
    tampon = io.BytesIO()
    classeur.save(tampon)
    tampon.seek(0)
    return tampon


# ---------------------------------------------------------------------------------------------------------------
# Pages et API
# ---------------------------------------------------------------------------------------------------------------
@app.get("/")
def accueil():
    return render_template("index.html", modele=reglages["modele"])


@app.get("/api/champs")
def champs():
    """Les champs à extraire : la page construit le tableau à partir d'eux."""
    nom, aide, champs_liste = LISTE
    return jsonify(modele=reglages["modele"],
                   champs=[{"nom": n, "type": t, "aide": a} for n, (t, a) in CHAMPS.items()],
                   liste={"nom": nom, "aide": aide,
                          "champs": [{"nom": n, "type": t, "aide": a} for n, (t, a) in champs_liste.items()]})


@app.get("/api/factures")
def liste_factures():
    return jsonify([public(f) for f in factures.values()])


@app.post("/api/factures")
def deposer():
    """Un ou plusieurs PDF (champ "pdf" du formulaire) mis dans la file d'attente."""
    deposees = []
    for fichier in request.files.getlist("pdf"):
        contenu = fichier.read()
        if not contenu.startswith(b"%PDF"):
            return jsonify(erreur=f"{fichier.filename} n'est pas un PDF"), 400
        nom = Path(fichier.filename or "facture.pdf").name
        pdf = SORTIE / f"{datetime.now():%Y%m%d-%H%M%S}_{re.sub(r'[^\w.-]+', '_', Path(nom).stem)[:80]}.pdf"
        pdf.write_bytes(contenu)
        f = {"id": uuid.uuid4().hex, "nom": nom, "pdf": pdf, "json": pdf.with_suffix(".json"),
             "statut": "attente", "resultat": None,
             "erreur": None, "erreur_bd": None, "doublon": None, "modifie": False, "enregistree": False, "debut": None, "fin": None}
        factures[f["id"]] = f
        file_attente.put(f)
        deposees.append(public(f))
    return jsonify(deposees)


@app.post("/api/factures/<id_>")
def corriger(id_):
    """Bouton « Enregistrer » : le tableau (corrigé ou non) remplace le résultat du LLM (et son fichier .json), puis le
    PDF est rangé dans Data d'après sa date de paiement ; numéro déjà enregistré en base : ni ligne, ni rangement."""
    if id_ not in factures:
        abort(404)
    f = factures[id_]
    donnees = request.get_json()
    f["modifie"] = f["modifie"] or donnees != f["resultat"]
    f["resultat"] = donnees
    enregistrer(f)
    f["doublon"] = doublon(f)
    if not f["doublon"]:
        f["enregistree"] = True
        classer(f)
        f["erreur_bd"] = sauvegarder_bd(f)
    return jsonify(public(f))


@app.get("/api/factures/<id_>/pdf")
def voir_pdf(id_):
    if id_ not in factures:
        abort(404)
    return send_file(factures[id_]["pdf"], mimetype="application/pdf", download_name=factures[id_]["nom"])


@app.post("/api/excel")
def exporter():
    """[{nom, donnees}] (les tableaux tels qu'affichés) -> classeur Excel."""
    return send_file(classeur_excel(request.get_json()), as_attachment=True,
                     download_name=f"factures_{datetime.now():%Y%m%d-%H%M}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def demarrer(modele, url):
    """Réglages, dossier de sortie, vérification du modèle, puis traitement des factures en arrière-plan."""
    reglages.update(modele=modele, url=url)
    SORTIE.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    # le modèle est-il installé dans Ollama ?
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=5) as r:
            modeles = [m["name"] for m in json.load(r).get("models", [])]
        if modele not in modeles and f"{modele}:latest" not in modeles:
            print(f"⚠ Modèle {modele} absent d'Ollama (installés : {', '.join(modeles) or 'aucun'}) :\n"
                  f"   docker exec ollama ollama pull {modele}", flush=True)
    except OSError:
        print(f"⚠ Ollama injoignable sur {url} : le conteneur ollama tourne-t-il ?", flush=True)

    # la base PostgreSQL est-elle joignable ? (table créée dès maintenant si besoin)
    if bd_active():
        schema, table = schema_table()
        try:
            preparer_table()
            print(f"Base PostgreSQL : tables {schema}.{table} et {schema}.{table_articles()[1]} prêtes", flush=True)
        except Exception as e:
            print(f"⚠ Base PostgreSQL ({schema}.{table}) : {e} — nouvel essai à la prochaine facture", flush=True)
    else:
        print("Base PostgreSQL non configurée (POSTGRES_USER vide dans app_invoice/.env) : résultats en fichiers seulement",
              flush=True)

    threading.Thread(target=travailleur, daemon=True).start()


def creer_app():
    """Production (gunicorn, voir Dockerfile) : réglages lus dans les variables MODELE et OLLAMA_URL.
    Un seul worker gunicorn : la file d'attente et la liste des factures sont en mémoire dans ce processus."""
    demarrer(os.environ.get("MODELE", reglages["modele"]), os.environ.get("OLLAMA_URL", reglages["url"]))
    return app


def main():
    """Développement : serveur intégré de Flask."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--modele", default=os.environ.get("MODELE", reglages["modele"]))
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", reglages["url"]))
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 : accessible depuis ce serveur seulement")
    args = ap.parse_args()
    demarrer(args.modele, args.url)
    print(f"Application : http://localhost:{args.port}   (modèle {args.modele}, Ctrl+C pour arrêter)")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
