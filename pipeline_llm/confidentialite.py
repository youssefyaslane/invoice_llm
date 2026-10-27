"""Étapes 2 et 4 : masquage réversible des données personnelles (pseudonymisation) avant l'envoi au LLM, puis remise
des vraies valeurs dans son résultat.

Détection automatique (sans liste à tenir à jour) :
    - Presidio (Microsoft) avec les modèles spaCy anglais et français : noms de personnes, lieux, organisations,
      sites web, emails, téléphones, IBAN, numéros de carte (les dates restent visibles) ;
    - formats sûrs : IBAN, téléphones, fin de numéro de carte ("ending in 1234"), date d'expiration de carte.
Complément facultatif : masquage_local.json (noms et adresses que Presidio ne reconnaîtrait pas ; jamais sur GitHub).

Chaque donnée est remplacée par un repère ([PERSONNE_1], [EMAIL_1]...) ; la même valeur garde le même repère dans tout le
document. La correspondance repère -> vraie valeur reste en mémoire : elle n'est ni envoyée ni enregistrée.
"""
import json
import logging
import re
from pathlib import Path

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider

logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)

FICHIER_LOCAL = Path(__file__).resolve().parent / "masquage_local.json"

# Types de données personnelles détectés par Presidio -> nom du repère
ENTITES = {"PERSON": "PERSONNE", "LOCATION": "LIEU", "EMAIL_ADDRESS": "EMAIL", "PHONE_NUMBER": "TEL",
           "IBAN_CODE": "IBAN", "CREDIT_CARD": "CARTE",
           "ORGANIZATION": "ORGANISATION", "URL": "SITE"}  # les dates restent visibles (non masquées)
# Étiquettes des modèles spaCy -> types Presidio (les organisations ne sont pas actives par défaut)
NER_SPACY = {"PER": "PERSON", "PERSON": "PERSON", "LOC": "LOCATION", "GPE": "LOCATION", "FAC": "LOCATION",
             "ORG": "ORGANIZATION"}

# Formats sûrs (le groupe 1, s'il existe, est un libellé gardé visible : seule la valeur est masquée)
MOTIFS = {
    "IBAN": re.compile(r"(?i)\bIBAN\b\s*:?\s*[A-Z]{2}\d{2}[A-Z0-9 ]{10,32}"
                       r"|\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b"),
    "TEL": re.compile(r"(?i)\b(tel|tél|phone|téléphone|fax|mobile|gsm)\b\.?\s*:?\s*\+?\d[\d\s().-]{6,}\d"
                      r"|\+\d[\d\s().-]{7,}\d|\b0[5-7]\d{8}\b"),
    "CARTE": re.compile(r"(?i)\b(ending in|ending|se terminant par|terminant par)\s+\d{4}\b|[*·•xX]{3,}\s*\d{4}\b"),
    "EXPIRATION": re.compile(r"(?i)\b(expires|expire|exp\.?|expiration)\s*(le\s*)?\d{1,2}\s*/\s*\d{2,4}"),
}

# Nom de la société cliente : jamais masqué (le LLM doit savoir que NETIS est le client, pas le fournisseur)
CLIENT = re.compile(r"(?i)\bnetis\b(\s+(holding|group))?(\s+(ltd|sa|sarl|sas))?\.?")

# Vocabulaire des factures que spaCy prend parfois pour une organisation, un lieu ou une personne ("Subtotal",
# "Unit Price", "Total de la facture TTC", "Référence") :
# jamais masqué, sinon le LLM ne comprend plus les tableaux ni les totaux
VOCABULAIRE = re.compile(r"(?i)\b(sub-?total|total|summary|price|invoice|tax|vat|tva|date|credit|charges?|amount|"
                         r"quantity|qty|description|section|billing|payment|balance|due|discount|reference|unit|"
                         r"indicator|statement|details?|order|receipt|prix|montant|quantité|référence|facture|"
                         r"sous-total|taxe|remise|abonnement|installation|rubrique|paiement|échéance)\b")

# Une détection de Presidio qui n'est qu'un montant, une date ou un nombre n'est jamais masquée
PROTEGE = re.compile(r"^[\s$€£]*-?[\d\s.,/:-]+[\s$€£%]*(USD|EUR|MAD|GBP|CHF)?$|^\d{1,2}\s+\w+\.?,?\s+\d{4}$", re.I)


def config_locale():
    if FICHIER_LOCAL.is_file():
        return json.loads(FICHIER_LOCAL.read_text(encoding="utf-8"))
    return {"personnes": [], "adresses": []}


def analyseur():
    nlp = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_lg"},
                   {"lang_code": "fr", "model_name": "fr_core_news_lg"}],
        "ner_model_configuration": {"model_to_presidio_entity_mapping": NER_SPACY}}).create_engine()
    return AnalyzerEngine(nlp_engine=nlp, supported_languages=["en", "fr"])


def zones_a_masquer(texte, analyzer):
    """Toutes les détections : (début, fin, repère, score, protégeable)."""
    zones = []
    for langue in ("en", "fr"):  # documents en anglais ou en français : on cherche avec les deux modèles
        entites = list(ENTITES) if langue == "en" else ["PERSON", "LOCATION", "ORGANIZATION"]
        for r in analyzer.analyze(text=texte, language=langue, entities=entites, score_threshold=0.35):
            if r.entity_type in ("ORGANIZATION", "PERSON", "LOCATION") and VOCABULAIRE.search(texte[r.start:r.end]):
                continue
            if "|" in texte[r.start:r.end]:  # jamais à cheval sur des cellules : la structure du tableau doit rester
                continue
            zones.append((r.start, r.end, ENTITES[r.entity_type], r.score, True))
    for etiquette, motif in MOTIFS.items():
        for m in motif.finditer(texte):
            debut, fin = m.start(), m.end()
            if m.lastindex:  # libellé capturé ("ending in", "Tel", "expires") : seule la valeur est masquée
                apres = m.end(m.lastindex)
                valeur = re.search(r"[+\d][\d\s().*/-]*\d", texte[apres:fin])
                if valeur:
                    debut, fin = apres + valeur.start(), apres + valeur.end()
            zones.append((debut, fin, etiquette, 1.0, False))
    config = config_locale()
    for etiquette, cle in (("PERSONNE", "personnes"), ("ADRESSE", "adresses")):
        for valeur in config.get(cle, []):
            for m in re.finditer(r"(?<!\w)" + re.escape(valeur).replace(r"\ ", r"\s+") + r"(?!\w)", texte, re.I):
                zones.append((m.start(), m.end(), etiquette, 1.0, False))

    # Une détection qui contient le nom du client ("Jean Dupont NETIS Holding Ltd") est coupée autour de lui,
    # puis chaque zone est resserrée sur son texte (sans espaces ni sauts de ligne autour)
    nettes = []
    for debut, fin, etiquette, score, protegeable in zones:
        client = CLIENT.search(texte, debut, fin)
        morceaux = ([(debut, client.start()), (client.end(), fin)] if client and client.end() <= fin
                    else [(debut, fin)])
        for d, f in morceaux:
            while d < f and texte[d] in " \t\n,;:":
                d += 1
            while f > d and texte[f - 1] in " \t\n,;:":
                f -= 1
            if re.search(r"[\w@+]", texte[d:f]):
                nettes.append((d, f, etiquette, score, protegeable))
    return nettes


def pseudonymiser(texte, analyzer):
    """Remplace chaque donnée personnelle par un repère ; renvoie (texte masqué, {repère: vraie valeur})."""
    retenues = []
    # zones qui se chevauchent : on garde la plus longue (puis la plus sûre)
    for debut, fin, etiquette, score, protegeable in sorted(zones_a_masquer(texte, analyzer),
                                                            key=lambda z: (-(z[1] - z[0]), -z[3])):
        valeur = texte[debut:fin]
        if (len(valeur) < 2 or (protegeable and PROTEGE.match(valeur))
                or any(d < fin and debut < f for d, f, _ in retenues)):
            continue
        retenues.append((debut, fin, etiquette))

    # Une valeur masquée une fois l'est partout dans le document (spaCy peut la rater à un autre endroit,
    # ex. un nom de ville) ; seulement pour les valeurs d'au moins 4 caractères, pour ne pas propager "MA" ou "US"
    for valeur, etiquette in {texte[d:f]: e for d, f, e in retenues if len(texte[d:f].strip()) >= 4}.items():
        for m in re.finditer(r"(?<!\w)" + re.escape(valeur) + r"(?!\w)", texte):
            if not any(d < m.end() and m.start() < f for d, f, _ in retenues):
                retenues.append((m.start(), m.end(), etiquette))

    correspondance, inverse, compteurs, morceaux, curseur = {}, {}, {}, [], 0
    for debut, fin, etiquette in sorted(retenues):
        valeur = texte[debut:fin]
        if valeur not in inverse:  # une même valeur garde le même repère dans tout le document
            compteurs[etiquette] = compteurs.get(etiquette, 0) + 1
            inverse[valeur] = f"[{etiquette}_{compteurs[etiquette]}]"
            correspondance[inverse[valeur]] = valeur
        morceaux += [texte[curseur:debut], inverse[valeur]]
        curseur = fin
    morceaux.append(texte[curseur:])
    return "".join(morceaux), correspondance


def restaurer(valeur, correspondance):
    """Remet les vraies valeurs à la place des repères, partout dans le résultat du LLM."""
    if isinstance(valeur, str):
        for repere, original in correspondance.items():
            valeur = valeur.replace(repere, original)
        return valeur
    if isinstance(valeur, list):
        return [restaurer(v, correspondance) for v in valeur]
    if isinstance(valeur, dict):
        return {k: restaurer(v, correspondance) for k, v in valeur.items()}
    return valeur
