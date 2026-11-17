"""Test : télécharge tous les PDF d'un dossier OneDrive partagé (Microsoft 365) dans Output/onedrive/.

Deux connexions possibles :
- par certificat (application Entra ID créée par l'administrateur, Sites.Selected) : rien à saisir. Utilisée dès que
  OD_CLIENT_ID est dans le fichier .env du projet (facture/.env, jamais dans le dépôt) :
      OD_TENANT=netisgroup.net                    (ID ou domaine du tenant, donné par l'administrateur)
      OD_CLIENT_ID=00000000-0000-0000-0000-000000000000
      OD_CERTIFICAT=app_invoice/certificat/certificat.pem
      OD_CLE=app_invoice/certificat/cle_privee.pem
  (chemins relatifs au dossier du projet ; l'empreinte du certificat est calculée par le script)
- sinon, avec votre propre compte NETIS (« device code ») : le script affiche une adresse et un code, vous les
  saisissez dans un navigateur, vous vous connectez, puis le téléchargement commence (approbation de
  l'administrateur nécessaire chez NETIS).
Lecture seule : rien n'est modifié ni supprimé dans OneDrive. Aucun mot de passe ni jeton n'est enregistré.
Bibliothèque standard Python et commande openssl uniquement (rien à installer). N'utilise pas l'application app_invoice.

Usage (depuis le dossier du projet) :
    python3 test/recuperer_onedrive.py "<lien du dossier copié depuis le navigateur>"
    python3 test/recuperer_onedrive.py "<lien>" --liste        -> affiche les PDF sans rien télécharger
Les sous-dossiers sont parcourus ; leur arborescence est gardée dans Output/onedrive/. Un PDF déjà téléchargé
(même nom, même taille) n'est pas retéléchargé.
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]  # dossier facture/ : Output, .env
GRAPH = "https://graph.microsoft.com/v1.0"
# application publique de Microsoft « Microsoft Graph Command Line Tools » : connexion sans application à créer
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
SCOPES = "Files.Read.All Sites.Read.All offline_access"


def requete(url, donnees=None, jeton=None):
    """GET (ou POST de formulaire si donnees) -> JSON."""
    entetes = {"Authorization": f"Bearer {jeton}"} if jeton else {}
    corps = urllib.parse.urlencode(donnees).encode() if donnees else None
    with urllib.request.urlopen(urllib.request.Request(url, data=corps, headers=entetes), timeout=60) as r:
        return json.load(r)


def lire_env(chemin):
    """Charge les variables KEY=valeur du fichier .env (sans écraser celles déjà définies)."""
    if not chemin.exists():
        return
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if ligne and not ligne.startswith("#") and "=" in ligne:
            cle, valeur = ligne.split("=", 1)
            os.environ.setdefault(cle.strip(), valeur.strip())


def b64url(octets):
    return base64.urlsafe_b64encode(octets).rstrip(b"=").decode()


def connexion_certificat(tenant, client_id, certificat, cle):
    """Connexion de l'application par certificat (jeton signé avec la clé privée) : renvoie le jeton Microsoft Graph."""
    pem = certificat.read_text()
    der = base64.b64decode("".join(pem.split("-----BEGIN CERTIFICATE-----")[1].split("-----END")[0].split()))
    url_jeton = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    maintenant = int(time.time())
    entete = {"alg": "RS256", "typ": "JWT", "x5t": b64url(hashlib.sha1(der).digest())}  # empreinte du certificat
    contenu = {"aud": url_jeton, "iss": client_id, "sub": client_id, "jti": str(uuid.uuid4()),
               "nbf": maintenant, "exp": maintenant + 600}
    a_signer = f"{b64url(json.dumps(entete).encode())}.{b64url(json.dumps(contenu).encode())}"
    signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(cle)], input=a_signer.encode(),
                               capture_output=True, check=True).stdout
    try:
        return requete(url_jeton, {"grant_type": "client_credentials", "client_id": client_id,
                                   "scope": "https://graph.microsoft.com/.default",
                                   "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                                   "client_assertion": f"{a_signer}.{b64url(signature)}"})["access_token"]
    except urllib.error.HTTPError as e:
        erreur = json.loads(e.read() or b"{}")
        raise SystemExit(f"Connexion refusée : {erreur.get('error')} — {erreur.get('error_description', '')[:400]}")


def connexion(tenant):
    """Connexion par code : renvoie le jeton d'accès Microsoft Graph."""
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    code = requete(f"{base}/devicecode", {"client_id": CLIENT_ID, "scope": SCOPES})
    print(f"\n>>> Ouvrez {code['verification_uri']} et saisissez le code : {code['user_code']}\n"
          f"    (connectez-vous avec votre compte NETIS ; le code expire dans {code['expires_in'] // 60} min)\n",
          flush=True)
    attente = code.get("interval", 5)
    while True:
        time.sleep(attente)
        try:
            return requete(f"{base}/token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                             "client_id": CLIENT_ID, "device_code": code["device_code"]})["access_token"]
        except urllib.error.HTTPError as e:
            erreur = json.loads(e.read() or b"{}")
            if erreur.get("error") == "authorization_pending":
                continue
            if erreur.get("error") == "slow_down":
                attente += 5
                continue
            raise SystemExit(f"Connexion refusée : {erreur.get('error')} — {erreur.get('error_description', '')[:400]}")


def lire_lien(lien):
    """Lien du dossier (vue « Partagés » ou lien direct) -> (hôte, /personal/<compte>, chemin dans Documents)."""
    url = urllib.parse.urlparse(lien)
    params = urllib.parse.parse_qs(url.query)
    chemin = urllib.parse.unquote(params["id"][0] if "id" in params else url.path)
    morceaux = chemin.strip("/").split("/")          # personal / <compte> / Documents / dossier / ...
    if len(morceaux) < 3 or morceaux[0] != "personal":
        raise SystemExit(f"Lien non reconnu (chemin attendu /personal/<compte>/Documents/...) : {chemin}")
    return url.hostname, f"/personal/{morceaux[1]}", "/".join(morceaux[3:])


def enfants(jeton, drive, element):
    """Contenu d'un dossier (toutes les pages)."""
    url = f"{GRAPH}/drives/{drive}/items/{element}/children?$top=200"
    while url:
        page = requete(url, jeton=jeton)
        yield from page["value"]
        url = page.get("@odata.nextLink")


def pdfs(jeton, drive, element, relatif=Path()):
    """Tous les PDF du dossier et de ses sous-dossiers : (chemin relatif, élément)."""
    for e in enfants(jeton, drive, element):
        if "folder" in e:
            yield from pdfs(jeton, drive, e["id"], relatif / e["name"])
        elif e["name"].lower().endswith(".pdf"):
            yield relatif / e["name"], e


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lien", help="lien du dossier OneDrive, copié depuis le navigateur")
    ap.add_argument("--liste", action="store_true", help="affiche les PDF sans rien télécharger")
    ap.add_argument("--dest", default=BASE_DIR / "Output" / "onedrive", type=Path)
    ap.add_argument("--tenant", default="organizations", help="domaine Microsoft 365 (défaut : compte professionnel)")
    args = ap.parse_args()

    lire_env(BASE_DIR / ".env")

    hote, compte, dossier = lire_lien(args.lien)
    print(f"Dossier : {dossier or '(racine)'}  —  OneDrive {compte} sur {hote}")
    if os.environ.get("OD_CLIENT_ID"):
        print("Connexion par certificat (application", os.environ["OD_CLIENT_ID"], ")")
        jeton = connexion_certificat(os.environ["OD_TENANT"], os.environ["OD_CLIENT_ID"],
                                     BASE_DIR / os.environ["OD_CERTIFICAT"], BASE_DIR / os.environ["OD_CLE"])
    else:
        jeton = connexion(args.tenant)

    try:
        site = requete(f"{GRAPH}/sites/{hote}:{compte}", jeton=jeton)
        drive = requete(f"{GRAPH}/sites/{site['id']}/drive", jeton=jeton)["id"]
        racine = requete(f"{GRAPH}/drives/{drive}/root:/{urllib.parse.quote(dossier)}", jeton=jeton)
        liste = list(pdfs(jeton, drive, racine["id"]))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Accès au dossier refusé ({e.code}) : {e.read().decode(errors='replace')[:400]}")

    total = sum(e["size"] for _, e in liste)
    print(f"{len(liste)} PDF trouvés ({total / 1e6:.1f} Mo)")
    nouveaux = 0
    for relatif, e in liste:
        cible = args.dest / relatif
        deja = cible.exists() and cible.stat().st_size == e["size"]
        print(f"  {'=' if deja else '+'} {relatif}  ({e['size'] / 1e3:.0f} Ko)")
        if args.liste or deja:
            continue
        cible.parent.mkdir(parents=True, exist_ok=True)
        # lien de téléchargement temporaire fourni par Graph (sans jeton)
        url = e.get("@microsoft.graph.downloadUrl") or requete(
            f"{GRAPH}/drives/{drive}/items/{e['id']}?select=id,@microsoft.graph.downloadUrl",
            jeton=jeton)["@microsoft.graph.downloadUrl"]
        with urllib.request.urlopen(url, timeout=300) as r:
            cible.write_bytes(r.read())
        nouveaux += 1
    if not args.liste:
        print(f"\n{nouveaux} PDF téléchargés, {len(liste) - nouveaux} déjà présents, dans {args.dest}")


if __name__ == "__main__":
    main()
