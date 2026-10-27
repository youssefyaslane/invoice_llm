"""Étape 3 : appel du LLM, avec une réponse forcée au format du schéma JSON.

    openai : API OpenAI (clé OPENAI_API_KEY dans le .env du projet ; OPENAI_BASE_URL pour un service compatible)
    ollama : modèle local (OLLAMA_URL, par défaut http://localhost:11434) : aucune donnée ne sort du serveur
"""
import json
import os
import urllib.error
import urllib.request


def lire_env(chemin):
    """Charge les variables CLE=valeur du fichier .env (sans écraser celles déjà définies)."""
    if chemin.is_file():
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            if "=" in ligne and not ligne.strip().startswith("#"):
                cle, valeur = ligne.split("=", 1)
                os.environ.setdefault(cle.strip(), valeur.strip().strip('"'))


def _post(url, corps, entetes, delai):
    req = urllib.request.Request(url, data=json.dumps(corps).encode(),
                                 headers={"Content-Type": "application/json", **entetes})
    try:
        with urllib.request.urlopen(req, timeout=delai) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Erreur du LLM ({e.code}) : {e.read().decode(errors='replace')[:500]}")


def schema_strict(schema):
    """Le mode strict d'OpenAI exige "additionalProperties": false sur chaque objet."""
    if isinstance(schema, dict):
        schema = {k: schema_strict(v) for k, v in schema.items()}
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
    return schema


def appeler_openai(modele, prompt, schema, texte):
    rep = _post(f"{os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')}/chat/completions", {
        "model": modele,
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": texte}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "extraction", "strict": True, "schema": schema_strict(schema)}},
    }, {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, 600)
    usage = rep.get("usage") or {}
    return json.loads(rep["choices"][0]["message"]["content"]), {
        "lus": usage.get("prompt_tokens") or usage.get("input_tokens"),
        "ecrits": usage.get("completion_tokens") or usage.get("output_tokens")}


def appeler_ollama(modele, prompt, schema, texte):
    rep = _post(f"{os.environ.get('OLLAMA_URL', 'http://localhost:11434')}/api/chat", {
        "model": modele,
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": texte}],
        "format": schema, "stream": False, "think": False, "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 8192},
    }, {}, 3600)
    return json.loads(rep["message"]["content"]), {"lus": rep.get("prompt_eval_count"), "ecrits": rep.get("eval_count")}


def appeler(fournisseur, modele, prompt, schema, texte):
    return (appeler_openai if fournisseur == "openai" else appeler_ollama)(modele, prompt, schema, texte)
