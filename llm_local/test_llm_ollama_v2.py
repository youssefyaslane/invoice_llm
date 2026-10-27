"""Test du nouveau prompt avec le LLM local (Ollama) : le modèle recopie, le code décide.

Différences avec test_llm_ollama.py (ancien prompt) :
    - plus de consigne « sinon... sinon... » : le modèle donne séparément l'émetteur et le client, la date de la
      facture, la date d'échéance et la date de paiement ; le code choisit ensuite le fournisseur et la date ;
    - quantité et prix unitaire : seulement s'ils sont dans une colonne Quantité / Prix unitaire ;
    - nom du fournisseur normalisé après le LLM (forme juridique retirée, table de noms connus) ;
    - contrôles affichés (sans relance) : somme des lignes = total, nombres présents dans le document,
      quantité égale au prix, fournisseur = client.
Le résultat final a le même format que l'ancien (fournisseur, date_paiement...) pour être comparé à attendu.json.

Entrée : le PDF (lu par Docling, pied de page compris : il faut .venv-docling) ou un Markdown déjà produit.
Script autonome (n'importe aucun autre fichier du projet).

Usage (depuis le dossier du projet ; Ollama doit tourner) :
    .venv-docling/bin/python llm_local/test_llm_ollama_v2.py "Data/2026/03-MAR/OVH/facture.pdf"
    .venv-docling/bin/python llm_local/test_llm_ollama_v2.py facture.pdf -m qwen2.5:3b
    .venv-docling/bin/python llm_local/test_llm_ollama_v2.py --prompt          -> affiche le prompt envoyé
"""
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJET = Path(__file__).resolve().parent.parent  # dossier facture/ : Output, attendu.json

# ---------------------------------------------------------------------------------------------------------------
# Ce que le modèle recopie : nom -> (type, explication donnée au modèle). Aucun choix à faire de sa part.
# ---------------------------------------------------------------------------------------------------------------
TYPE_DOCUMENT = "une facture"
CHAMPS = {
    "emetteur": ("texte?", "nom de la société qui émet la facture (en-tête, « Issued by », mentions légales ou pied "
                           "de page), tel qu'écrit"),
    "client": ("texte?", "nom de la société à qui la facture est adressée (« Facturé à », « Bill to », « Issued to »), "
                         "tel qu'écrit"),
    "date_facture": ("texte?", "date d'émission de la facture, au format JJ/MM/AAAA"),
    "date_echeance": ("texte?", "date limite de paiement (« échéance », « due date »), au format JJ/MM/AAAA"),
    "date_paiement": ("texte?", "date à laquelle le paiement a été fait (« payé le », « date paid »), au format "
                                "JJ/MM/AAAA"),
    "devise": ("texte", "code ISO à 3 lettres (USD, EUR, MAD...)"),
    "total": ("nombre", "montant total écrit sur le document, taxes comprises"),
}
LISTE = ("articles", "une entrée par ligne d'article des tableaux, dans l'ordre ; pas les sous-totaux, taxes, totaux "
                     "ni paiements", {
    "description": ("texte", "nom de l'article tel qu'écrit"),
    "quantite": ("nombre?", "nombre de la colonne Quantité (Qty, Quantity) de cette ligne ; null s'il n'y a pas de "
                            "colonne quantité"),
    "prix_unitaire": ("nombre?", "nombre de la colonne Prix unitaire (Unit price) de cette ligne ; null s'il n'y a "
                                 "pas de colonne prix unitaire"),
    "prix": ("nombre", "montant de la ligne (négatif s'il est écrit avec un signe -)"),
})
TYPES_JSON = {"texte": {"type": "string"}, "nombre": {"type": "number"},
              "texte?": {"type": ["string", "null"]}, "nombre?": {"type": ["number", "null"]}}

# Après le LLM : nom du fournisseur
CLIENT = re.compile(r"\bnetis\b", re.I)  # la société cliente : jamais le fournisseur
FORME_JURIDIQUE = re.compile(r"[\s,]+(Inc|Ltd|LLC|L\.L\.C|Limited|Corp|Corporation|Co|Pty|Plc|PBC|GmbH|SAS|SARL|SA|"
                             r"S\.A|SL|SLU|S\.L|S\.L\.U)\.?$", re.I)
NOMS_FOURNISSEURS = {r"^Microsoft\b": "Microsoft", r"^Adobe\b": "Adobe", r"^Freepik\b": "Magnific",
                     r"^Paddle\b": "Paddle"}


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
              "Les dates d'une période (du ... au ..., début et fin d'abonnement) ne sont ni la date de la facture, ni "
              "l'échéance, ni la date de paiement.",
              ""]
    lignes += [f"- {nom} : {explication}" for nom, (_, explication) in CHAMPS.items()]
    nom, explication, champs = LISTE
    lignes.append(f"- {nom} : {explication}")
    lignes += [f"  - {n} : {e}" for n, (_, e) in champs.items()]
    return "\n".join(lignes)


def pdf_en_markdown(pdf):
    """Mêmes réglages Docling que le pipeline : vrais tableaux, en-têtes et pieds de page gardés, sans images."""
    import logging

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import ContentLayer
    logging.getLogger("docling").setLevel(logging.WARNING)
    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = True
    options.generate_picture_images = False
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
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
        "keep_alive": "30m",       # le modèle reste chargé entre deux tests
        "options": {"temperature": 0, "num_ctx": 8192},
    }
    req = urllib.request.Request(f"{url}/api/chat", data=json.dumps(corps).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            rep = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Erreur d'Ollama ({e.code}) : {e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Ollama injoignable sur {url} ({e.reason}) : le conteneur ollama tourne-t-il ?")
    return json.loads(rep["message"]["content"]), rep


# ---------------------------------------------------------------------------------------------------------------
# Après le LLM : le code décide, puis contrôle
# ---------------------------------------------------------------------------------------------------------------
def nom_fournisseur(brut):
    """Émetteur, sauf si c'est le client NETIS (le modèle a inversé) ; sans forme juridique ; nom usuel."""
    nom = brut.get("emetteur")
    if not nom or CLIENT.search(nom):
        client = brut.get("client")
        nom = client if client and not CLIENT.search(client) else None
    if not nom:
        return None
    nom = FORME_JURIDIQUE.sub("", nom.strip()).strip(" ,.")
    for motif, usuel in NOMS_FOURNISSEURS.items():
        if re.search(motif, nom, re.I):
            return usuel
    return nom


def decider(brut):
    """Réponse brute (ce qui est écrit) -> résultat au format habituel (fournisseur, date_paiement...)."""
    return {
        "fournisseur": nom_fournisseur(brut),
        "date_paiement": brut.get("date_paiement") or brut.get("date_echeance") or brut.get("date_facture"),
        "devise": brut.get("devise"),
        "total": brut.get("total"),
        "articles": brut.get("articles") or [],
    }


def nombres_du_texte(texte):
    """Tous les nombres écrits dans le document, lus avec une virgule ou un point décimal ("1 234,56", "1,234.56")."""
    nombres = set()
    for m in re.finditer(r"\d[\d\s  .,]*", texte):
        s = re.sub(r"[\s  ]", "", m[0]).rstrip(".,")
        for decimal, milliers in ((",", "."), (".", ",")):
            try:
                nombres.add(round(float(s.replace(milliers, "").replace(decimal, ".")), 2))
            except ValueError:
                pass
    return nombres


def controles(data, texte):
    """Erreurs détectables sans connaître la bonne réponse."""
    alertes = []
    articles = data["articles"]
    somme = round(sum(a.get("prix") or 0 for a in articles), 2)
    if data["total"] is not None and abs(somme - data["total"]) >= 0.01:
        alertes.append(f"somme des lignes {somme} ≠ total {data['total']}")
    ecrits = nombres_du_texte(texte)
    for i, a in enumerate(articles, 1):
        for champ in ("quantite", "prix_unitaire", "prix"):
            v = a.get(champ)
            if v not in (None, 0) and round(abs(v), 2) not in ecrits:
                alertes.append(f"ligne {i} : {champ} {v} n'est écrit nulle part dans le document")
        if a.get("quantite") not in (None, 0, 1) and a.get("quantite") == a.get("prix"):
            alertes.append(f"ligne {i} : quantité = prix ({a['prix']}), colonne probablement mal lue")
    if data["total"] is not None and round(abs(data["total"]), 2) not in ecrits:
        alertes.append(f"total {data['total']} n'est écrit nulle part dans le document")
    if not data["fournisseur"]:
        alertes.append("fournisseur introuvable (ou seulement le client NETIS)")
    return alertes


def norm(v):
    return round(float(v), 2) if isinstance(v, (int, float)) else str(v or "").strip().lower()


def comparer(data, attendu):
    ok = 0
    for champ, val in attendu["champs"].items():
        bon = norm(data.get(champ)) in {norm(v) for v in (val if isinstance(val, list) else [val])}
        ok += bon
        print(f"{'✅' if bon else '❌'} {champ:<15} attendu={val!r:<40} obtenu={data.get(champ)!r}")
    articles = data.get("articles") or []
    for i, (desc, prix) in enumerate(attendu["articles"]):
        art = articles[i] if i < len(articles) else {}
        bon = norm(art.get("prix")) == norm(prix) and (desc is None or norm(art.get("description")) == norm(desc))
        ok += bon
        print(f"{'✅' if bon else '❌'} article {i + 1:<7} attendu={(desc or '') + ' ' + str(prix):<40} "
              f"obtenu={art.get('description') or ''} {art.get('prix')!r}")
    total = len(attendu["champs"]) + len(attendu["articles"])
    print(f"\nScore : {ok}/{total} = {ok / total:.0%}   ({len(articles)} articles extraits, {len(attendu['articles'])} attendus)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fichier", nargs="?", type=Path, help="PDF de la facture (ou Markdown produit par Docling)")
    ap.add_argument("-m", "--modele", default=os.environ.get("MODELE", "gemma3:4b"))
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--prompt", action="store_true", help="affiche le prompt envoyé et s'arrête")
    args = ap.parse_args()

    prompt, schema = construire_prompt(), construire_schema()
    if args.prompt:
        print(prompt)
        return
    if not args.fichier:
        ap.error("indiquez le PDF de la facture (ou son Markdown Docling)")

    print(f"=== {args.fichier.name}  —  modèle : {args.modele} (Ollama, local)  —  nouveau prompt (v2)")
    t0 = time.time()
    if args.fichier.suffix.lower() == ".pdf":
        texte = pdf_en_markdown(args.fichier)
        print(f"Docling : {time.time() - t0:.1f} s")
    else:
        texte = args.fichier.read_text(encoding="utf-8")
    # images intégrées (logos en base64) retirées : inutiles et très coûteuses en tokens
    texte = re.sub(r"!\[[^\]]*\]\(data:image/[^)]*\)", "<!-- image -->", texte)

    t0 = time.time()
    brut, rep = appeler_ollama(args.url, args.modele, prompt, schema, texte)
    lus = rep.get("prompt_eval_count", 0)
    print(f"LLM : {time.time() - t0:.1f} s  (lecture {lus} tokens en {rep.get('prompt_eval_duration', 0) / 1e9:.1f} s, "
          f"génération {rep.get('eval_count', 0)} tokens en {rep.get('eval_duration', 0) / 1e9:.1f} s)")
    if lus >= 8192 * 0.95:
        print("⚠ Le document remplit presque tout le contexte (8192 tokens) : la fin a pu être coupée.")

    print("\n--- Ce que le modèle a recopié ---")
    print(json.dumps({k: v for k, v in brut.items() if k != LISTE[0]}, indent=2, ensure_ascii=False))
    data = decider(brut)
    print("\n--- Résultat (après décision du code) ---")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    alertes = controles(data, texte)
    print("\n--- Contrôles ---")
    print("\n".join(f"⚠ {a}" for a in alertes) if alertes else "✓ aucun problème détecté")

    sortie = PROJET / "Output" / f"{args.fichier.stem}_{args.modele.replace(':', '_').replace('/', '_')}_v2.json"
    sortie.write_text(json.dumps({"brut": brut, "resultat": data, "alertes": alertes}, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    print(f"\nRésultat enregistré : {sortie}")

    fichier_attendu = PROJET / "attendu.json"
    attendu = json.loads(fichier_attendu.read_text(encoding="utf-8")) if fichier_attendu.is_file() else {}
    if args.fichier.stem in attendu:
        print("\n--- Comparaison avec les valeurs exactes ---")
        comparer(data, attendu[args.fichier.stem])


if __name__ == "__main__":
    main()
