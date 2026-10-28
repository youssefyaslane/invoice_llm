// Page de l'application : dépôt des PDF, suivi de l'extraction, tableau modifiable du résultat.
// Les champs et les colonnes viennent de champs.py (via /api/champs) : rien à changer ici pour d'autres champs.

const $ = (id) => document.getElementById(id);

let meta = null;          // champs, liste et modèle
let factures = [];        // état renvoyé par le serveur
let choisie = null;       // id de la facture affichée en bas
let affichee = null;      // "id:statut" déjà dessiné (on ne redessine pas pendant une saisie)
const brouillons = {};    // id -> données modifiées non enregistrées

const ETATS = {
  attente: (f) => `En attente${f.rang ? ` (n° ${f.rang})` : ""}`,
  lecture: () => "Lecture du PDF…",
  llm: () => `Extraction par ${meta.modele}…`,
  fini: (f) => (f.modifie ? "Terminé · corrigé" : "Terminé"),
  erreur: () => "Erreur",
};

// ---------------------------------------------------------------------------------------------------------------
// Outils
// ---------------------------------------------------------------------------------------------------------------
const estNombre = (type) => type.startsWith("nombre");
const copie = (v) => JSON.parse(JSON.stringify(v));

function duree(s) {
  if (s == null) return "";
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function montant(n) {
  return n.toLocaleString("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// "1 234,56 €" -> 1234.56 ; "" -> null ; texte illisible -> NaN
function lireNombre(texte) {
  let s = texte.replace(/[\s  $€£]/g, "");
  if (s === "") return null;
  if (s.includes(",") && s.includes(".")) s = s.replace(/,/g, "");
  else s = s.replace(",", ".");
  return Number(s);
}

function element(balise, attributs = {}, enfants = []) {
  const e = document.createElement(balise);
  for (const [cle, valeur] of Object.entries(attributs)) {
    if (cle === "texte") e.textContent = valeur;
    else if (cle.startsWith("on")) e.addEventListener(cle.slice(2), valeur);
    else e.setAttribute(cle, valeur);
  }
  e.append(...enfants);
  return e;
}

function message(texte) {
  $("message").textContent = texte;
  $("message").hidden = !texte;
}

const facture = (id) => factures.find((f) => f.id === id);
const donnees = (f) => brouillons[f.id] ?? f.resultat;

// ---------------------------------------------------------------------------------------------------------------
// Dépôt des PDF
// ---------------------------------------------------------------------------------------------------------------
async function deposer(fichiers) {
  const pdfs = [...fichiers].filter((f) => f.type === "application/pdf" || /\.pdf$/i.test(f.name));
  message(pdfs.length < fichiers.length ? "Seuls les fichiers PDF sont acceptés : les autres ont été ignorés." : "");
  if (!pdfs.length) return;
  const formulaire = new FormData();
  pdfs.forEach((f) => formulaire.append("pdf", f));
  const rep = await fetch("/api/factures", { method: "POST", body: formulaire });
  const json = await rep.json().catch(() => ({}));
  if (!rep.ok) return message(json.erreur || `Envoi refusé (${rep.status})`);
  if (!choisie && json.length) choisie = json[0].id;
  actualiser();
}

const depot = $("depot");
["dragenter", "dragover"].forEach((t) =>
  depot.addEventListener(t, (e) => { e.preventDefault(); depot.classList.add("survol"); }));
["dragleave", "drop"].forEach((t) =>
  depot.addEventListener(t, (e) => { e.preventDefault(); depot.classList.remove("survol"); }));
depot.addEventListener("drop", (e) => deposer(e.dataTransfer.files));
$("choix").addEventListener("change", (e) => { deposer(e.target.files); e.target.value = ""; });
// un PDF lâché à côté de la zone ne doit pas remplacer la page
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

// ---------------------------------------------------------------------------------------------------------------
// Liste des factures déposées
// ---------------------------------------------------------------------------------------------------------------
async function actualiser() {
  try {
    factures = await (await fetch("/api/factures")).json();
  } catch {
    return; // serveur momentanément injoignable : on réessaiera
  }
  dessinerListe();
  const f = facture(choisie);
  if (f && affichee !== `${f.id}:${f.statut}`) dessinerResultat();
}

function dessinerListe() {
  $("bloc-liste").hidden = !factures.length;
  $("excel").disabled = !factures.some((f) => f.statut === "fini");
  $("liste").replaceChildren(...factures.map((f) => {
    const d = donnees(f);
    const resume = f.statut === "fini" && d
      ? [d.fournisseur, d.total != null ? `${montant(d.total)} ${d.devise || ""}` : null].filter(Boolean).join(" · ")
      : f.statut === "erreur" ? f.erreur : "";
    return element("tr", { class: f.id === choisie ? "choisie" : "", onclick: () => choisir(f.id) }, [
      element("td", {}, [
        element("span", { class: "nom", texte: f.nom }),
        resume ? element("span", { class: "resume", texte: resume }) : "",
      ]),
      element("td", {}, [element("span", { class: `etat ${f.statut}`, texte: ETATS[f.statut](f) })]),
      element("td", { class: "duree", texte: duree(f.duree) }),
      element("td", {}, [brouillons[f.id] ? element("span", { class: "etat attente", texte: "non enregistré" }) : ""]),
    ]);
  }));
}

function choisir(id) {
  choisie = id;
  affichee = null;
  dessinerListe();
  dessinerResultat();
}

// ---------------------------------------------------------------------------------------------------------------
// Résultat de la facture choisie : champs + tableau des lignes, modifiables
// ---------------------------------------------------------------------------------------------------------------
function saisie(valeur, type, original, quandChange) {
  const nombre = estNombre(type);
  const input = element("input", {
    class: `saisie${nombre ? " nombre" : ""}`,
    type: "text",
    inputmode: nombre ? "decimal" : "text",
  });
  input.value = valeur ?? "";
  const marquer = (v) => input.classList.toggle("modifiee", JSON.stringify(v ?? null) !== JSON.stringify(original ?? null));
  marquer(valeur);
  input.addEventListener("input", () => {
    let v = input.value.trim() === "" ? null : input.value;
    if (nombre) {
      v = lireNombre(input.value);
      input.setCustomValidity(Number.isNaN(v) ? "Nombre illisible" : "");
      if (Number.isNaN(v)) return input.reportValidity();
    }
    quandChange(v);
    marquer(v);
  });
  return input;
}

function dessinerResultat() {
  const f = facture(choisie);
  $("bloc-resultat").hidden = !f;
  if (!f) return;
  affichee = `${f.id}:${f.statut}`;
  $("titre-resultat").textContent = f.nom;
  $("lien-pdf").href = `/api/factures/${f.id}/pdf`;

  const pret = f.statut === "fini";
  ["ajouter"].forEach((id) => ($(id).disabled = !pret));
  if (!pret) {
    const texte = f.statut === "erreur" ? `Erreur : ${f.erreur}` : `${ETATS[f.statut](f)} — le tableau apparaîtra ici.`;
    $("champs").replaceChildren(element("p", { class: "sous-titre", texte }));
    $("entete-articles").replaceChildren();
    $("articles").replaceChildren();
    $("controle").textContent = "";
    $("controle").className = "controle";
    majBoutons();
    return;
  }

  const d = donnees(f);
  const original = f.resultat;
  const modifier = () => {
    if (!brouillons[f.id]) brouillons[f.id] = copie(d);
    return brouillons[f.id];
  };

  // champs simples (fournisseur, date, devise, total...)
  $("champs").replaceChildren(...meta.champs.map((c) =>
    element("div", { class: "champ" }, [
      element("label", { texte: c.nom.replace(/_/g, " "), title: c.aide }),
      saisie(d[c.nom], c.type, original[c.nom], (v) => { modifier()[c.nom] = v; apresModification(); }),
    ])));

  dessinerLignes();
}

function dessinerLignes() {
  const f = facture(choisie);
  const d = donnees(f);
  const { nom, champs } = meta.liste;
  const lignes = d[nom] || [];
  const originales = f.resultat[nom] || [];
  $("titre-liste").textContent = `${nom.charAt(0).toUpperCase()}${nom.slice(1)} (${lignes.length})`;

  $("entete-articles").replaceChildren(element("tr", {}, [
    element("th", { texte: "#" }),
    ...champs.map((c) => element("th", { class: estNombre(c.type) ? "nombre" : "", texte: c.nom.replace(/_/g, " "), title: c.aide })),
    element("th"),
  ]));

  const corps = lignes.map((ligne, i) => element("tr", {}, [
    element("td", { class: "numero", texte: String(i + 1) }),
    ...champs.map((c) => element("td", { class: estNombre(c.type) ? "col-nombre" : "" }, [
      saisie(ligne[c.nom], c.type, (originales[i] || {})[c.nom], (v) => {
        brouillon()[nom][i][c.nom] = v;
        apresModification();
      }),
    ])),
    element("td", { class: "supprimer" }, [
      element("button", { class: "icone", type: "button", title: "Supprimer la ligne", "aria-label": "Supprimer la ligne",
        texte: "✕", onclick: () => { brouillon()[nom].splice(i, 1); dessinerLignes(); apresModification(); } }),
    ]),
  ]));

  // ligne de somme sous la colonne prix
  const iPrix = champs.findIndex((c) => c.nom === "prix");
  if (iPrix >= 0) {
    corps.push(element("tr", { class: "pied" }, [
      element("td"),
      ...champs.map((c, j) => element("td", {
        class: j === iPrix ? "somme" : "",
        texte: j === iPrix ? montant(somme(lignes)) : j === 0 ? "Somme des lignes" : "",
      })),
      element("td"),
    ]));
  }
  $("articles").replaceChildren(...corps);
  majControle();
}

function brouillon() {
  const f = facture(choisie);
  if (!brouillons[f.id]) brouillons[f.id] = copie(f.resultat);
  const nom = meta.liste.nom;
  if (!brouillons[f.id][nom]) brouillons[f.id][nom] = [];
  return brouillons[f.id];
}

const somme = (lignes) => Math.round(lignes.reduce((s, l) => s + (Number(l.prix) || 0), 0) * 100) / 100;

// contrôle : la somme des lignes (TTC), ou la somme des lignes + taxe (lignes HT), doit faire le total de la facture
function majControle() {
  const d = donnees(facture(choisie));
  const total = d.total;
  const taxe = Number(d.taxe) || 0;
  const s = somme(d[meta.liste.nom] || []);
  const el = $("controle");
  if (total == null || !meta.liste.champs.some((c) => c.nom === "prix")) {
    el.textContent = "";
    el.className = "controle";
  } else if (Math.abs(s - total) < 0.01) {
    el.textContent = `✓ Somme des lignes = total (${montant(total)})`;
    el.className = "controle ok";
  } else if (taxe && Math.abs(s + taxe - total) < 0.01) {
    el.textContent = `✓ Somme des lignes ${montant(s)} + taxe ${montant(taxe)} = total (${montant(total)})`;
    el.className = "controle ok";
  } else {
    el.textContent = `⚠ Somme des lignes ${montant(s)}${taxe ? ` + taxe ${montant(taxe)}` : ""} ≠ total ${montant(total)} : à vérifier`;
    el.className = "controle ko";
  }
}

function apresModification() {
  majControle();
  majBoutons();
  dessinerListe();
  // le pied « somme » suit les prix saisis
  const pied = document.querySelector("#articles tr.pied td.somme");
  if (pied) pied.textContent = montant(somme(donnees(facture(choisie))[meta.liste.nom] || []));
  $("etat-enregistrement").textContent = brouillons[choisie] ? "Modifications non enregistrées" : "";
}

function majBoutons() {
  const modifie = Boolean(brouillons[choisie]);
  $("enregistrer").disabled = !modifie;
  $("annuler").disabled = !modifie;
}

$("ajouter").addEventListener("click", () => {
  const ligne = Object.fromEntries(meta.liste.champs.map((c) => [c.nom, null]));
  brouillon()[meta.liste.nom].push(ligne);
  dessinerLignes();
  apresModification();
  const saisies = document.querySelectorAll("#articles tr:not(.pied) input");
  saisies[saisies.length - meta.liste.champs.length]?.focus();
});

$("annuler").addEventListener("click", () => {
  delete brouillons[choisie];
  affichee = null;
  dessinerResultat();
  apresModification();
});

$("enregistrer").addEventListener("click", async () => {
  const id = choisie;
  const rep = await fetch(`/api/factures/${id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(brouillons[id]),
  });
  if (!rep.ok) return ($("etat-enregistrement").textContent = `Échec de l'enregistrement (${rep.status})`);
  const maj = await rep.json();
  factures = factures.map((f) => (f.id === id ? maj : f));
  delete brouillons[id];
  affichee = null;
  dessinerResultat();
  apresModification();
  $("etat-enregistrement").textContent = "✓ Enregistré";
});

// ---------------------------------------------------------------------------------------------------------------
// Export Excel de toutes les factures terminées (avec les modifications affichées, même non enregistrées)
// ---------------------------------------------------------------------------------------------------------------
$("excel").addEventListener("click", async () => {
  const liste = factures.filter((f) => f.statut === "fini").map((f) => ({ nom: f.nom, donnees: donnees(f) }));
  const rep = await fetch("/api/excel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(liste),
  });
  if (!rep.ok) return message(`Export Excel impossible (${rep.status})`);
  const nom = (rep.headers.get("Content-Disposition") || "").match(/filename="?([^";]+)/)?.[1] || "factures.xlsx";
  const lien = element("a", { href: URL.createObjectURL(await rep.blob()), download: nom });
  lien.click();
  setTimeout(() => URL.revokeObjectURL(lien.href), 1000);
});

window.addEventListener("beforeunload", (e) => {
  if (Object.keys(brouillons).length) e.preventDefault();
});

// ---------------------------------------------------------------------------------------------------------------
(async () => {
  meta = await (await fetch("/api/champs")).json();
  await actualiser();
  if (!choisie && factures.length) choisir(factures[factures.length - 1].id);
  setInterval(actualiser, 2000);
})();
