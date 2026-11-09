"""Étape 4 bis : mise en forme des champs après la remise des vraies valeurs, selon leur type dans champs.py.
Nécessaire quand la valeur était masquée : le LLM n'a pu que recopier le repère, sans la convertir.

    "date"    : "July 09, 2026", "05-MAR-2026", "14 Mars 2026", "2026-01-16"...  -> JJ/MM/AAAA
    "societe" : "Microsoft Ireland Operations Limited", "OVH SAS"                   -> sans forme juridique
Pour tous les textes : "&#124;" -> "|" (codé par Docling dans les tableaux), "null" écrit en texte -> vide.
"""
import html
import re
from datetime import datetime

from champs import CHAMPS, LISTE

FORMATS_DATE = ["%d/%m/%Y", "%d-%b-%Y", "%d/%b/%Y", "%d %b %Y", "%d %B %Y", "%d %b, %Y", "%d %B, %Y",
                "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d.%m.%Y", "%d-%m-%Y"]
MOIS_FR = {"janvier": "January", "février": "February", "fevrier": "February", "mars": "March", "avril": "April",
           "mai": "May", "juin": "June", "juillet": "July", "août": "August", "aout": "August",
           "septembre": "September", "octobre": "October", "novembre": "November", "décembre": "December",
           "decembre": "December"}
FORME_JURIDIQUE = re.compile(r"[\s,]+(Inc|Ltd|LLC|L\.L\.C|Limited|Corp|Corporation|Co|Pty|Plc|PBC|GmbH|SAS|SARL|SA|"
                             r"S\.A|SL|SLU|S\.L|S\.L\.U)\.?$", re.I)


def en_date(texte):
    """Date au format JJ/MM/AAAA ; inchangée si le format n'est pas reconnu."""
    if not isinstance(texte, str):
        return texte
    propre = re.sub(r"[A-Za-zéûè]+", lambda m: MOIS_FR.get(m[0].lower(), m[0]), texte.strip())
    propre = re.sub(r"\s+\d{1,2}:\d{2}.*$", "", propre)  # heure éventuelle : "July 09, 2026 07:26 AM"
    for fmt in FORMATS_DATE:
        try:
            return datetime.strptime(propre, fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return texte


def en_societe(texte):
    while isinstance(texte, str) and FORME_JURIDIQUE.search(texte):
        texte = FORME_JURIDIQUE.sub("", texte).strip(" ,")
    return texte


FONCTIONS = {"date": en_date, "societe": en_societe}


def nettoyer(valeur):
    """Tous les textes : caractères codés par Docling ("&#124;" -> "|"), "null" écrit en toutes lettres -> vide."""
    if isinstance(valeur, str):
        valeur = html.unescape(valeur).strip()
        return None if valeur.lower() in ("", "null", "none", "n/a") else valeur
    if isinstance(valeur, list):
        return [nettoyer(v) for v in valeur]
    if isinstance(valeur, dict):
        return {k: nettoyer(v) for k, v in valeur.items()}
    return valeur


def mettre_en_forme(data):
    data = nettoyer(data)
    for nom, (type_, _) in CHAMPS.items():
        if type_ in FONCTIONS and nom in data:
            data[nom] = FONCTIONS[type_](data[nom])
    if LISTE:
        nom_liste, _, champs = LISTE
        for ligne in data.get(nom_liste) or []:
            for nom, (type_, _) in champs.items():
                if type_ in FONCTIONS and nom in ligne:
                    ligne[nom] = FONCTIONS[type_](ligne[nom])
    return data
