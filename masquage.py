"""Masquage des données personnelles et sensibles avant d'envoyer un document à un LLM dans le cloud
(minimisation des données : RGPD, loi marocaine 09-08). Seul ce qui est utile à l'extraction est envoyé.

Masqué automatiquement :
    emails du client, téléphones, IBAN, numéros et dates d'expiration de carte bancaire
Masqué à partir de la liste locale masquage_local.json (jamais sur GitHub) :
    noms des personnes et adresses du client (NETIS)

Voir ce qui serait envoyé, sans rien envoyer :
    .venv-llm/bin/python masquage.py "Output/docling/facture.md"
"""
import json
import re
import sys
from pathlib import Path

FICHIER_LOCAL = Path(__file__).resolve().parent / "masquage_local.json"

EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[a-z]{2,}", re.I)
# "IBAN : FR76 1234 5678 9012 3456 7890 123" ou un IBAN seul (au moins 16 caractères en groupes de 4)
IBAN = re.compile(r"(?i)\bIBAN\b\s*:?\s*[A-Z]{2}\d{2}[A-Z0-9 ]{10,32}|\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b")
TELEPHONE = re.compile(r"(?i)\b(tel|tél|phone|téléphone|fax|mobile|gsm)\b\.?\s*:?\s*\+?\d[\d\s().-]{6,}\d"
                       r"|\+\d[\d\s().-]{7,}\d|\b0[5-7]\d{8}\b")
CARTE = re.compile(r"(?i)\b(ending in|ending|se terminant par|terminant par)\s+\d{4}\b|[*·•xX]{3,}\s*\d{4}\b")
EXPIRATION = re.compile(r"(?i)\b(expires|expire|exp\.?|expiration)\s*(le\s*)?\d{1,2}\s*/\s*\d{2,4}")


def config_locale():
    if FICHIER_LOCAL.is_file():
        return json.loads(FICHIER_LOCAL.read_text(encoding="utf-8"))
    return {"personnes": [], "adresses": [], "domaines_client": []}


def masquer(texte, config=None):
    """Renvoie (texte masqué, nombre de remplacements par type)."""
    config = config or config_locale()
    compte = {}

    def remplacer(motif, etiquette, texte, garder_libelle=False):
        def sub(m):
            compte[etiquette] = compte.get(etiquette, 0) + 1
            libelle = m.group(1) if garder_libelle and m.lastindex and m.group(1) else None
            return f"{libelle} [{etiquette}]" if libelle else f"[{etiquette}]"
        return motif.sub(sub, texte)

    # 1. Noms et adresses connus du client (liste locale), du plus long au plus court
    for etiquette, cle in (("ADRESSE", "adresses"), ("PERSONNE", "personnes")):
        for valeur in sorted(config.get(cle, []), key=len, reverse=True):
            texte = remplacer(re.compile(r"(?<!\w)" + re.escape(valeur).replace(r"\ ", r"\s+") + r"(?!\w)", re.I),
                              etiquette, texte)
    # 2. Emails du client seulement (ceux du fournisseur aident à le reconnaître)
    domaines = [d.lower() for d in config.get("domaines_client", [])]
    def email(m):
        if any(d in m.group(0).lower() for d in domaines):
            compte["EMAIL"] = compte.get("EMAIL", 0) + 1
            return "[EMAIL]"
        return m.group(0)
    texte = EMAIL.sub(email, texte)
    # 3. Données bancaires et téléphones (format reconnu, quel que soit le document)
    texte = remplacer(IBAN, "IBAN", texte)
    texte = remplacer(CARTE, "CARTE", texte, garder_libelle=True)
    texte = remplacer(EXPIRATION, "EXPIRATION", texte, garder_libelle=True)
    texte = remplacer(TELEPHONE, "TEL", texte, garder_libelle=True)
    return texte, compte


if __name__ == "__main__":
    for chemin in sys.argv[1:]:
        masque, compte = masquer(Path(chemin).read_text(encoding="utf-8"))
        print(f"===== {chemin}  —  remplacements : {compte or 'aucun'}\n{masque}\n")
