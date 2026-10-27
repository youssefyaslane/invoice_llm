"""Test d'une facture avec un LLM local (Ollama, dans Docker) : même Markdown Docling, même prompt simple et même
schéma que test_llm_openai.py, pour comparer le modèle local au modèle OpenAI sur la même entrée.

Aucun masquage : le texte ne quitte pas le serveur. Script autonome (n'importe aucun autre fichier du projet).

Usage (depuis le dossier du projet ; Ollama doit tourner) :
    .venv-llm/bin/python llm_local/test_llm_ollama.py "Output/docling/facture.md"
    .venv-llm/bin/python llm_local/test_llm_ollama.py "Output/docling/facture.md" -m qwen2.5:7b
    .venv-llm/bin/python llm_local/test_llm_ollama.py --prompt          -> affiche le prompt envoyé
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
# Champs à extraire (identiques au test OpenAI) : nom -> (type, explication donnée au modèle)
# ---------------------------------------------------------------------------------------------------------------
TYPE_DOCUMENT = "une facture"
CHAMPS = {
    "fournisseur": ("texte", "société qui émet la facture et reçoit le paiement (NETIS est le client, jamais le "
                             "fournisseur), sans forme juridique (Inc, Ltd, LLC, SAS...)"),
    "date_paiement": ("texte", "date de paiement, sinon date d'échéance, sinon date de la facture, au format JJ/MM/AAAA"),
    "devise": ("texte", "code ISO à 3 lettres (USD, EUR, MAD...)"),
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
    ap.add_argument("md", nargs="?", type=Path, help="fichier Markdown produit par Docling (Output/docling/<facture>.md)")
    ap.add_argument("-m", "--modele", default=os.environ.get("MODELE", "qwen2.5:3b"))
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--prompt", action="store_true", help="affiche le prompt envoyé et s'arrête")
    args = ap.parse_args()

    prompt, schema = construire_prompt(), construire_schema()
    if args.prompt:
        print(prompt)
        return
    if not args.md:
        ap.error("indiquez le fichier Markdown de la facture (Output/docling/<facture>.md)")

    # images intégrées (logos en base64) retirées : inutiles et très coûteuses en tokens
    texte = re.sub(r"!\[[^\]]*\]\(data:image/[^)]*\)", "<!-- image -->", args.md.read_text(encoding="utf-8"))
    print(f"=== {args.md.name}  —  modèle : {args.modele} (Ollama, local)  —  Docling + prompt simple")

    t0 = time.time()
    data, rep = appeler_ollama(args.url, args.modele, prompt, schema, texte)
    print(f"Temps : {time.time() - t0:.1f} s  (chargement {rep.get('load_duration', 0) / 1e9:.1f} s, "
          f"lecture {rep.get('prompt_eval_count', 0)} tokens en {rep.get('prompt_eval_duration', 0) / 1e9:.1f} s, "
          f"génération {rep.get('eval_count', 0)} tokens en {rep.get('eval_duration', 0) / 1e9:.1f} s)\n")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    sortie = PROJET / "Output" / f"{args.md.stem}_{args.modele.replace(':', '_').replace('/', '_')}_local.json"
    sortie.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nRésultat enregistré : {sortie}")

    fichier_attendu = PROJET / "attendu.json"
    attendu = json.loads(fichier_attendu.read_text(encoding="utf-8")) if fichier_attendu.is_file() else {}
    if args.md.stem in attendu:
        print("\n--- Comparaison avec les valeurs exactes ---")
        comparer(data, attendu[args.md.stem])


if __name__ == "__main__":
    main()
