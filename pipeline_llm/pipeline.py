"""Pipeline d'extraction confidentielle de documents :

    PDF ──► 1. Docling      : Markdown avec de vrais tableaux                        (lecture.py)
        ──► 2. Presidio     : données personnelles remplacées par des repères        (confidentialite.py)
        ──► 3. LLM          : prompt simple généré à partir des champs voulus        (champs.py, llm.py)
        ──► 4. Restauration : vraies valeurs remises à la place des repères          (confidentialite.py)
                            puis mises en forme selon le type du champ (date, société)  (mise_en_forme.py)
        ──► 5. Résultat     : Output/pipeline/<document>.json

Pour un autre type de document, on modifie seulement champs.py.
La correspondance repère -> vraie valeur reste en mémoire sur le serveur : jamais envoyée au LLM ni enregistrée.
Le texte réellement envoyé est gardé dans Output/pipeline/<document>_envoye.md, pour contrôle.

Usage (depuis le dossier du projet, avec l'environnement Docling qui contient aussi Presidio) :
    .venv-docling/bin/python pipeline_llm/pipeline.py "Data/2026/07-JUL/Microsoft/facture.pdf"
    .venv-docling/bin/python pipeline_llm/pipeline.py Data/2026/07-JUL            -> tous les PDF du dossier
    .venv-docling/bin/python pipeline_llm/pipeline.py facture.pdf --voir-envoi    -> texte masqué, sans LLM
    .venv-docling/bin/python pipeline_llm/pipeline.py facture.pdf --llm ollama    -> LLM local (rien ne sort) :
                                                                                     pas de masquage (étapes 2 et 4)
"""
import argparse
import json
import os
import time
from pathlib import Path

from champs import LISTE, construire_prompt, construire_schema
from confidentialite import analyseur, pseudonymiser, restaurer
from lecture import convertisseur, pdf_en_markdown
from llm import appeler, lire_env
from mise_en_forme import mettre_en_forme

PROJET = Path(__file__).resolve().parent.parent  # dossier facture/ : Data, Output, .env, attendu.json
SORTIE = PROJET / "Output" / "pipeline"


def norm(v):
    return round(float(v), 2) if isinstance(v, (int, float)) else str(v or "").strip().lower()


def comparer(data, attendu):
    """Compare au fichier attendu.json (valeurs exactes des factures de test), s'il existe."""
    ok = 0
    for champ, val in attendu["champs"].items():
        bon = norm(data.get(champ)) in {norm(v) for v in (val if isinstance(val, list) else [val])}
        ok += bon
        print(f"{'✅' if bon else '❌'} {champ:<15} attendu={val!r:<40} obtenu={data.get(champ)!r}")
    lignes = data.get(LISTE[0]) or [] if LISTE else []
    for i, (desc, prix) in enumerate(attendu.get("articles", [])):
        art = lignes[i] if i < len(lignes) else {}
        bon = norm(art.get("prix")) == norm(prix) and (desc is None or norm(art.get("description")) == norm(desc))
        ok += bon
        print(f"{'✅' if bon else '❌'} article {i + 1:<7} attendu={(desc or '') + ' ' + str(prix):<40} "
              f"obtenu={art.get('description') or ''} {art.get('prix')!r}")
    total = len(attendu["champs"]) + len(attendu.get("articles", []))
    print(f"\nScore : {ok}/{total} = {ok / total:.0%}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chemins", nargs="+", type=Path, help="PDF ou dossiers (sous-dossiers compris)")
    ap.add_argument("--llm", choices=["openai", "ollama"], default="openai")
    ap.add_argument("-m", "--modele", help="modèle (défaut : OPENAI_MODEL du .env ou gpt-6-luna ; qwen2.5:3b pour ollama)")
    ap.add_argument("--voir-envoi", action="store_true", help="affiche le texte masqué et les repères, sans appeler le LLM")
    args = ap.parse_args()

    lire_env(PROJET / ".env")
    modele = args.modele or (os.environ.get("OPENAI_MODEL", "gpt-6-luna") if args.llm == "openai" else "qwen2.5:3b")
    if args.llm == "openai" and not args.voir_envoi and not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Clé manquante : ajoutez OPENAI_API_KEY=... dans le fichier .env du projet")

    pdfs = sorted(p for c in args.chemins for p in ([c] if c.is_file() else c.rglob("*")) if p.suffix.lower() == ".pdf")
    if not pdfs:
        raise SystemExit("Aucun PDF trouvé")
    SORTIE.mkdir(parents=True, exist_ok=True)
    fichier_attendu = PROJET / "attendu.json"
    attendu = json.loads(fichier_attendu.read_text(encoding="utf-8")) if fichier_attendu.is_file() else {}
    prompt, schema = construire_prompt(), construire_schema()
    # LLM local : le texte ne quitte pas le serveur, le masquage est inutile (et Presidio n'est pas chargé)
    masquer = args.llm == "openai" or args.voir_envoi
    conv, analyzer = convertisseur(), analyseur() if masquer else None

    for pdf in pdfs:
        print(f"\n=== {pdf.name}")
        t0 = time.time()
        texte = pdf_en_markdown(conv, pdf)                       # 1. Docling
        t1 = time.time()
        masque, correspondance = pseudonymiser(texte, analyzer) if masquer else (texte, {})  # 2. Presidio
        t2 = time.time()
        (SORTIE / f"{pdf.stem}_envoye.md").write_text(masque, encoding="utf-8")
        types = {}
        for repere in correspondance:
            t = repere.strip("[]").rsplit("_", 1)[0]
            types[t] = types.get(t, 0) + 1
        print(f"1. Docling : {t1 - t0:.1f} s   2. Presidio : "
              + (f"{t2 - t1:.1f} s, masqué : {types or 'rien'}" if masquer else "non (LLM local, rien ne sort)"))
        if args.voir_envoi:
            print("\nRepères (restent sur le serveur) :")
            for repere, valeur in correspondance.items():
                print(f"   {repere:<14} = {valeur!r}")
            print(f"\nTexte envoyé au LLM :\n{masque}")
            continue

        t0 = time.time()
        brut, tokens = appeler(args.llm, modele, prompt, schema, masque)  # 3. LLM
        data = mettre_en_forme(restaurer(brut, correspondance))             # 4. vraies valeurs remises, mises en forme
        remis = sum(json.dumps(brut, ensure_ascii=False).count(r) for r in correspondance)
        print(f"3. LLM ({args.llm}, {modele}) : {time.time() - t0:.1f} s, {tokens['lus']} tokens lus, "
              f"{tokens['ecrits']} écrits   4. Restauration : {remis} repère(s) remplacé(s)\n")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        (SORTIE / f"{pdf.stem}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        if pdf.stem in attendu:
            print("\n--- Comparaison avec les valeurs exactes ---")
            comparer(data, attendu[pdf.stem])
    print(f"\nRésultats : {SORTIE}")


if __name__ == "__main__":
    main()
