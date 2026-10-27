"""Test d'extraction d'une facture PDF par le LLM seul : toutes les règles sont dans le prompt,
aucune correction par le code (contrairement à test_llm_facture.py).

Usage (voir docker-compose.yml) :
    docker compose run --rm test-prompt [Data/facture.pdf]
ou en local :
    .venv-llm/bin/python test_llm_prompt.py [facture.pdf] [-m modele] [--url http://localhost:11434]

Le résultat est comparé à attendu.json (s'il existe) pour mesurer ce que le prompt obtient seul.
"""
import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

import pdfplumber

BASE_DIR = Path(__file__).resolve().parent

TXT, NUM, NUM_NULL = {"type": "string"}, {"type": "number"}, {"type": ["number", "null"]}
# fournisseur en dernier : le modèle écrit le JSON dans cet ordre, il a donc déjà lu les articles
# (règle "ISO") et applique la vérification finale du prompt au moment d'écrire le nom
CHAMPS = {
    "date_paiement": TXT,
    "devise": TXT,
    "articles": {
        "type": "array",
        "items": {"type": "object",
                  "properties": {"description": TXT, "quantite": NUM_NULL, "prix_unitaire": NUM_NULL, "prix": NUM},
                  "required": ["description", "quantite", "prix_unitaire", "prix"]},
    },
    "total": NUM,
    "fournisseur": TXT,
}
SCHEMA = {"type": "object", "properties": CHAMPS, "required": list(CHAMPS)}

PROMPT = """Extrais de la facture : le fournisseur, la date de paiement, la devise, les articles achetés et le total.
Le texte vient d'un PDF à colonnes mélangées : une étiquette et sa valeur peuvent être sur des lignes différentes,
et une même ligne peut coller deux colonnes.
RÈGLE ABSOLUE : tous les nombres (prix, quantités, prix unitaires, total) sont recopiés tels qu'écrits dans la facture.
Ne calcule rien : pas d'addition, pas de multiplication, pas d'arrondi. Un nombre absent de la facture est null.

FOURNISSEUR
- C'est la société qui émet la facture et reçoit le paiement. Cherche-la après "Issued by" ou "From", dans l'en-tête,
  le pied de page (adresse, capital, RCS, SIREN, n° TVA), le site web ou l'email.
- NETIS (NETIS Holding, NETIS GROUP) est le client : ce n'est jamais le fournisseur.

DATE DE PAIEMENT (format JJ/MM/AAAA)
- Par ordre de priorité : date de paiement ("Date Paid", "Paid Date", "Paid on", "prélevé le"),
  sinon date d'échéance ("Due Date", "Date due", "Due on", "Échéance"), sinon date de la facture ("Invoice Date").
- Jamais une période de service ou d'abonnement, une prochaine facturation ("Next Billing Date") ou une expiration de carte.
- Mois écrits en lettres (anglais ou français) : convertis-les en chiffres.
- Fournisseur aux États-Unis (adresse USA) : les dates en chiffres sont au format MM/JJ/AAAA ; inverse jour et mois.

DEVISE : code ISO à 3 lettres ($ = USD, € = EUR, £ = GBP ; MAD, CHF...).

ARTICLES
- Un article par ligne du tableau d'articles qui contient des montants, dans l'ordre. Inclus aussi les articles à 0.
- Ne sont pas des articles : titre de section (ligne juste avant les en-têtes de colonnes), taxe (VAT, TVA, Tax),
  sous-total, total, montant dû ou payé (Amount due, Balance, Payments, Invoice Amount), notes et publicités,
  les lignes de paiement ("Paid via", "Payment", carte bancaire, "Payé par"),
  et les nombres sans décimales (ex. un nombre de contacts).
- description : nom de l'article recopié sans traduire, sans la quantité ni les montants. Le texte sans montant juste
  au-dessus ou en dessous fait partie de la description. Si la ligne n'a que des dates et des montants,
  prends le titre de section au-dessus.
- quantite, prix_unitaire : recopiés de la ligne de l'article ; null s'ils n'y sont pas écrits.
  Un nombre entier juste avant le prix unitaire est la quantité.
- Un nombre suivi d'un mot ("5,000 contacts", "10 Sites", "48TB") fait partie de la description :
  ce n'est ni une quantité, ni un prix, ni un total.
- prix, prix_unitaire et total sont toujours des montants écrits avec une devise ou deux décimales ("$100.00", "1 234,56 €").
- prix : dernier montant de la ligne de l'article (montant total de l'article). Ne calcule jamais un montant.

TOTAL : montant total écrit sur la facture, taxes comprises. Recopie-le : ne l'additionne jamais.

MONTANTS : nombres à point décimal, sans devise ni séparateur de milliers ("1 234,56 €" = 1234.56 ; "1,234.56" = 1234.56).

VÉRIFICATION FINALE DU FOURNISSEUR, à appliquer avant de répondre :
1. Retire la forme juridique à la fin du nom : Inc, Ltd, LLC, Limited, Corp, Company, Pty, GmbH, SAS, SARL, SA, SL, SLU.
2. Si le nom contient "Microsoft", écris "Microsoft".
3. Si le nom contient "Adobe", écris "Adobe".
4. Si le nom contient "Freepik", écris "Magnific".
5. Si le nom contient "Rocket Science Group", écris "Mailchimp".
6. Si la description d'un article contient "ISO", écris "ISO".
7. Si le nom contient "NETIS", c'est le client : cherche l'autre société."""


def extraire_texte(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(p.extract_text(x_tolerance=1) or "" for p in pdf.pages)


def appeler_ollama(url, modele, texte):
    corps = {
        "model": modele,
        "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": texte}],
        "format": SCHEMA,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 4096},
    }
    req = urllib.request.Request(f"{url}/api/chat", data=json.dumps(corps).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        rep = json.load(r)
    return json.loads(rep["message"]["content"]), rep


def norm(v):
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    return str(v or "").strip().lower()


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
    print(f"\nScore : {ok}/{total} = {ok / total:.0%}   "
          f"({len(articles)} articles extraits, {len(attendu['articles'])} attendus)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", default=BASE_DIR / "Data" / "INV-55868.pdf", type=Path)
    ap.add_argument("-m", "--modele", default=os.environ.get("MODELE", "qwen2.5:3b"))
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    args = ap.parse_args()

    texte = extraire_texte(args.pdf)
    print(f"Facture : {args.pdf.name}  ({len(texte)} caractères)  —  modèle : {args.modele}  —  prompt seul")

    t0 = time.time()
    data, rep = appeler_ollama(args.url, args.modele, texte)
    print(f"Temps : {time.time() - t0:.1f} s  (chargement {rep.get('load_duration', 0) / 1e9:.1f} s, "
          f"lecture {rep.get('prompt_eval_count', 0)} tokens en {rep.get('prompt_eval_duration', 0) / 1e9:.1f} s, "
          f"génération {rep.get('eval_count', 0)} tokens en {rep.get('eval_duration', 0) / 1e9:.1f} s)\n")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    sortie = BASE_DIR / "Output" / f"{args.pdf.stem}_{args.modele.replace(':', '_')}_prompt.json"
    sortie.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nRésultat enregistré : {sortie}")

    fichier_attendu = BASE_DIR / "attendu.json"
    attendu = json.loads(fichier_attendu.read_text(encoding="utf-8")) if fichier_attendu.is_file() else {}
    if args.pdf.stem in attendu:
        print("\n--- Comparaison avec les valeurs exactes ---")
        comparer(data, attendu[args.pdf.stem])


if __name__ == "__main__":
    main()
