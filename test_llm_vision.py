"""Test d'extraction d'une facture par un modèle VISION : le modèle reçoit l'image de chaque page du PDF
(et non le texte extrait), avec le même prompt et le même schéma que test_llm_prompt.py, sans correction par le code.

Usage (voir docker-compose.yml) :
    docker compose run --rm test-vision "Data/facture.pdf"
    docker compose run --rm test-vision "Data/facture.pdf" --dpi 130     -> image plus nette (plus lent)
ou en local :
    .venv-llm/bin/python test_llm_vision.py facture.pdf [-m qwen3-vl:4b] [--url http://localhost:11434]
"""
import argparse
import base64
import io
import json
import os
import time
import urllib.request
from pathlib import Path

import pdfplumber

from test_llm_prompt import BASE_DIR, PROMPT, SCHEMA, comparer

# Même prompt que le test "prompt seul", mais le modèle lit les pages en image au lieu d'un texte aplati
PROMPT_VISION = PROMPT.replace(
    "Le texte vient d'un PDF à colonnes mélangées : une étiquette et sa valeur peuvent être sur des lignes différentes,\n"
    "et une même ligne peut coller deux colonnes.\n",
    "Les images jointes sont les pages de la facture : lis les tableaux et les colonnes comme ils apparaissent.\n")


def pages_en_images(pdf_path, dpi):
    """Chaque page du PDF en image PNG encodée en base64 (format attendu par Ollama)."""
    images = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tampon = io.BytesIO()
            page.to_image(resolution=dpi).original.save(tampon, format="PNG")
            images.append(base64.b64encode(tampon.getvalue()).decode())
    return images


def appeler_ollama(url, modele, images):
    corps = {
        "model": modele,
        "messages": [{"role": "system", "content": PROMPT_VISION},
                     {"role": "user", "content": "Voici la facture.", "images": images}],
        "format": SCHEMA,
        "stream": False,
        "think": False,  # pas de raisonnement écrit avant la réponse : beaucoup trop lent sur CPU
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 8192},  # une page en image prend environ 1 000 à 2 000 tokens
    }
    req = urllib.request.Request(f"{url}/api/chat", data=json.dumps(corps).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        rep = json.load(r)
    return json.loads(rep["message"]["content"]), rep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-m", "--modele", default=os.environ.get("MODELE", "qwen3-vl:4b"))
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--dpi", type=int, default=100, help="résolution des images (100 par défaut ; plus = plus net et plus lent)")
    args = ap.parse_args()

    images = pages_en_images(args.pdf, args.dpi)
    print(f"Facture : {args.pdf.name}  ({len(images)} page(s) en image, {args.dpi} dpi)  —  modèle : {args.modele}  —  vision")

    t0 = time.time()
    data, rep = appeler_ollama(args.url, args.modele, images)
    print(f"Temps : {time.time() - t0:.1f} s  (chargement {rep.get('load_duration', 0) / 1e9:.1f} s, "
          f"lecture {rep.get('prompt_eval_count', 0)} tokens en {rep.get('prompt_eval_duration', 0) / 1e9:.1f} s, "
          f"génération {rep.get('eval_count', 0)} tokens en {rep.get('eval_duration', 0) / 1e9:.1f} s)\n")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    sortie = BASE_DIR / "Output" / f"{args.pdf.stem}_{args.modele.replace(':', '_')}_vision.json"
    sortie.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nRésultat enregistré : {sortie}")

    fichier_attendu = BASE_DIR / "attendu.json"
    attendu = json.loads(fichier_attendu.read_text(encoding="utf-8")) if fichier_attendu.is_file() else {}
    if args.pdf.stem in attendu:
        print("\n--- Comparaison avec les valeurs exactes ---")
        comparer(data, attendu[args.pdf.stem])


if __name__ == "__main__":
    main()
