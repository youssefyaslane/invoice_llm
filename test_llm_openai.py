"""Test d'une facture avec un modèle OpenAI (par défaut gpt-6-luna) : même Markdown Docling, même prompt et même
schéma que test_llm_docling.py (le modèle local), pour comparer les deux sur la même entrée.

⚠ Le texte de la facture est envoyé à l'API OpenAI (il quitte le serveur), après masquage des données
personnelles (masquage.py : noms, adresses du client, emails, téléphones, IBAN, carte bancaire).

Clé API dans le fichier .env (à côté de ce script, jamais sur GitHub) :
    OPENAI_API_KEY=sk-...
    OPENAI_MODEL=gpt-6-luna          (facultatif)

Usage :
    .venv-llm/bin/python test_llm_openai.py "Output/docling/facture.md"
    .venv-llm/bin/python test_llm_openai.py "Output/docling/facture.md" -m gpt-6-sol
    .venv-llm/bin/python test_llm_openai.py "Output/docling/facture.md" --voir-envoi   -> sans envoi
"""
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from masquage import masquer
from test_llm_docling import BASE_DIR, comparer, construire_prompt, construire_schema


def lire_env(chemin):
    """Charge les variables CLE=valeur du fichier .env (sans écraser celles déjà définies)."""
    if chemin.is_file():
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            if "=" in ligne and not ligne.strip().startswith("#"):
                cle, valeur = ligne.split("=", 1)
                os.environ.setdefault(cle.strip(), valeur.strip().strip('"'))


def schema_strict(schema):
    """Le mode strict d'OpenAI exige "additionalProperties": false sur chaque objet."""
    if isinstance(schema, dict):
        schema = {k: schema_strict(v) for k, v in schema.items()}
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
    return schema


def appeler_openai(modele, prompt, schema, texte):
    corps = {
        "model": modele,
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": texte}],
        # réponse forcée au format du schéma (comme "format" avec Ollama)
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "extraction", "strict": True, "schema": schema_strict(schema)}},
    }
    req = urllib.request.Request(f"{os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')}/chat/completions",
                                 data=json.dumps(corps).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            rep = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Erreur de l'API OpenAI ({e.code}) : {e.read().decode(errors='replace')[:500]}")
    return json.loads(rep["choices"][0]["message"]["content"]), rep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("md", type=Path, help="fichier Markdown produit par Docling (Output/docling/<facture>.md)")
    ap.add_argument("-m", "--modele", help="modèle OpenAI (par défaut OPENAI_MODEL du .env, sinon gpt-6-luna)")
    ap.add_argument("--voir-envoi", action="store_true",
                    help="affiche le texte masqué qui serait envoyé, sans appeler l'API")
    args = ap.parse_args()

    # images intégrées (logos en base64) retirées, puis données personnelles masquées avant tout envoi
    texte = re.sub(r"!\[[^\]]*\]\(data:image/[^)]*\)", "<!-- image -->", args.md.read_text(encoding="utf-8"))
    texte, masques = masquer(texte)
    if args.voir_envoi:
        print(f"Masqué : {masques or 'rien'}\n\n{texte}")
        return

    lire_env(BASE_DIR / ".env")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Clé manquante : ajoutez OPENAI_API_KEY=... dans le fichier .env")
    modele = args.modele or os.environ.get("OPENAI_MODEL", "gpt-6-luna")

    print(f"=== {args.md.name}  —  modèle : {modele} (OpenAI)  —  Docling + prompt simple")
    print(f"Données masquées avant l'envoi : {masques or 'aucune'}")

    t0 = time.time()
    data, rep = appeler_openai(modele, construire_prompt(), construire_schema(), texte)
    usage = rep.get("usage") or {}
    # selon le modèle, les compteurs s'appellent prompt_/completion_tokens ou input_/output_tokens
    lus = usage.get("prompt_tokens") or usage.get("input_tokens")
    ecrits = usage.get("completion_tokens") or usage.get("output_tokens")
    if lus is None and ecrits is None:
        print(f"Temps : {time.time() - t0:.1f} s  (compteurs de tokens non reconnus, renvoyés par l'API : {usage})\n")
    else:
        print(f"Temps : {time.time() - t0:.1f} s  (lecture {lus} tokens, réponse {ecrits} tokens, "
              f"total {usage.get('total_tokens', (lus or 0) + (ecrits or 0))})\n")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    sortie = BASE_DIR / "Output" / f"{args.md.stem}_{modele.replace(':', '_').replace('/', '_')}_openai.json"
    sortie.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nRésultat enregistré : {sortie}")

    fichier_attendu = BASE_DIR / "attendu.json"
    attendu = json.loads(fichier_attendu.read_text(encoding="utf-8")) if fichier_attendu.is_file() else {}
    if args.md.stem in attendu:
        print("\n--- Comparaison avec les valeurs exactes ---")
        comparer(data, attendu[args.md.stem])


if __name__ == "__main__":
    main()
