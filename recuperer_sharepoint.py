"""Télécharge les nouvelles factures PDF d'un dossier SharePoint dans Data/ (Office365-REST-Python-Client).

Installation :
    .venv-llm/bin/pip install Office365-REST-Python-Client

Configuration dans un fichier .env (à côté de ce script, à ne jamais partager) :
    SP_SITE_URL=https://netis.sharepoint.com/sites/Finance
    SP_DOSSIER=/sites/Finance/Shared Documents/Factures
    # Connexion par application Entra ID + certificat (recommandé) :
    SP_TENANT=netis.onmicrosoft.com
    SP_CLIENT_ID=00000000-0000-0000-0000-000000000000
    SP_CERT_THUMBPRINT=ABCDEF...
    SP_CERT_PATH=/home/y_yaslane/facture/certificat.pem
    # ou, si le compte n'a pas de MFA (déconseillé) :
    SP_USER=prenom.nom@netis.com
    SP_PASSWORD=...

Usage :
    .venv-llm/bin/python recuperer_sharepoint.py            -> télécharge les PDF absents de Data/
    .venv-llm/bin/python recuperer_sharepoint.py --liste    -> affiche seulement les PDF du dossier
"""
import argparse
import os
from pathlib import Path

from office365.runtime.auth.user_credential import UserCredential
from office365.sharepoint.client_context import ClientContext

BASE_DIR = Path(__file__).resolve().parent


def lire_env(chemin):
    """Charge les variables KEY=valeur du fichier .env (sans écraser celles déjà définies)."""
    if not chemin.exists():
        return
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if ligne and not ligne.startswith("#") and "=" in ligne:
            cle, valeur = ligne.split("=", 1)
            os.environ.setdefault(cle.strip(), valeur.strip())


def connexion():
    ctx = ClientContext(os.environ["SP_SITE_URL"])
    if os.environ.get("SP_CLIENT_ID"):
        return ctx.with_client_certificate(os.environ["SP_TENANT"], os.environ["SP_CLIENT_ID"],
                                           os.environ["SP_CERT_THUMBPRINT"], cert_path=os.environ["SP_CERT_PATH"])
    return ctx.with_credentials(UserCredential(os.environ["SP_USER"], os.environ["SP_PASSWORD"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--liste", action="store_true", help="affiche les PDF du dossier sans rien télécharger")
    ap.add_argument("--dest", default=BASE_DIR / "Data", type=Path)
    args = ap.parse_args()
    lire_env(BASE_DIR / ".env")

    ctx = connexion()
    fichiers = ctx.web.get_folder_by_server_relative_url(os.environ["SP_DOSSIER"]).files.get().execute_query()
    pdfs = [f for f in fichiers if f.name.lower().endswith(".pdf")]
    print(f"{len(pdfs)} PDF dans {os.environ['SP_DOSSIER']}")

    args.dest.mkdir(exist_ok=True)
    nouveaux = 0
    for f in pdfs:
        cible = args.dest / f.name
        if cible.exists():
            continue
        print(f"  nouveau : {f.name}")
        if not args.liste:
            with open(cible, "wb") as sortie:
                f.download(sortie).execute_query()
        nouveaux += 1
    print(f"{nouveaux} nouvelle(s) facture(s)" + ("" if args.liste else f" téléchargée(s) dans {args.dest}"))


if __name__ == "__main__":
    main()
