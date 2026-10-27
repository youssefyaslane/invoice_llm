"""Les champs à extraire : c'est le seul fichier à modifier pour un autre type de document (contrat, CV, devis...).
Le prompt donné au LLM et le schéma JSON de sa réponse sont générés automatiquement à partir de ces champs.

Types : "texte", "nombre", "texte?" / "nombre?" (peut être vide : null),
        "date"    : texte remis au format JJ/MM/AAAA après le LLM (même si la date était masquée),
        "societe" : texte dont la forme juridique (Inc, Ltd, SAS...) est retirée après le LLM.
"""
TYPE_DOCUMENT = "une facture"

CHAMPS = {
    "fournisseur": ("societe", "société qui émet la facture et reçoit le paiement (NETIS est le client, jamais le "
                             "fournisseur), sans forme juridique (Inc, Ltd, LLC, SAS...)"),
    "date_paiement": ("date", "date de paiement, sinon date d'échéance, sinon date de la facture, au format JJ/MM/AAAA"),
    "devise": ("texte", "code ISO à 3 lettres (USD, EUR, MAD...)"),
    "total": ("nombre", "montant total écrit sur le document, taxes comprises"),
}

# Une liste de lignes (facultatif, None sinon) : nom de la liste, explication, champs de chaque ligne
LISTE = ("articles", "une entrée par ligne d'article des tableaux, dans l'ordre ; pas les sous-totaux, taxes, totaux "
                     "ni paiements", {
    "description": ("texte", "nom de l'article tel qu'écrit"),
    "quantite": ("nombre?", "quantité écrite, sinon null"),
    "prix_unitaire": ("nombre?", "prix unitaire écrit, sinon null"),
    "prix": ("nombre", "montant de la ligne (négatif s'il est écrit avec un signe -)"),
})

TYPES_JSON = {"texte": {"type": "string"}, "nombre": {"type": "number"},
              "date": {"type": "string"}, "societe": {"type": "string"},
              "texte?": {"type": ["string", "null"]}, "nombre?": {"type": ["number", "null"]}}


def construire_schema():
    proprietes = {nom: TYPES_JSON[t] for nom, (t, _) in CHAMPS.items()}
    if LISTE:
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
              "Certaines données personnelles sont remplacées par des repères entre crochets ([PERSONNE_1], [EMAIL_1]...) : "
              "si la valeur d'un champ est un repère (ou en contient un), recopie-le tel quel : il sera remplacé et "
              "mis en forme ensuite.",
              "Montants : nombres à point décimal, sans devise ni séparateur de milliers (\"1 234,56 €\" = 1234.56).",
              ""]
    lignes += [f"- {nom} : {explication}" for nom, (_, explication) in CHAMPS.items()]
    if LISTE:
        nom, explication, champs = LISTE
        lignes.append(f"- {nom} : {explication}")
        lignes += [f"  - {n} : {e}" for n, (_, e) in champs.items()]
    return "\n".join(lignes)
