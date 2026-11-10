// Page de l'application : dépôt des PDF, suivi de l'extraction, tableau modifiable du résultat.
// Les champs et les colonnes viennent de app.py (via /api/champs) : rien à changer ici pour d'autres champs
// (LIBELLES et AIDES ne servent qu'à afficher des noms et des explications plus lisibles).

const $ = (id) => document.getElementById(id);

let meta = null;          // champs, liste et modèle
let factures = [];        // état renvoyé par le serveur
let choisie = null;       // id de la facture affichée en bas
let affichee = null;      // "id:statut" déjà dessiné (on ne redessine pas pendant une saisie)
const brouillons = {};    // id -> données modifiées non enregistrées

// texte et couleur de la pastille d'état
const ETATS = {
  attente: (f) => [`En file${f.rang ? ` · n° ${f.rang}` : ""}`, "attente"],
  lecture: () => ["Lecture du PDF", "encours"],
  llm: () => ["Extraction par l'IA", "encours"],
  fini: (f) => (f.doublon ? ["Doublon · déjà en base", "erreur"]
    : f.enregistree ? [f.modifie ? "Enregistrée · corrigée" : "Enregistrée", "ok"] : ["À vérifier", "averifier"]),
  erreur: () => ["Erreur", "erreur"],
};

// noms affichés et explications courtes sous chaque champ (les consignes du modèle restent dans app.py)
const LIBELLES = {
  fournisseur: "Fournisseur", numero_facture: "N° de facture", date_paiement: "Date de paiement", devise: "Devise",
  taxe: "Taxe", total: "Total TTC", description: "Description", quantite: "Quantité", prix_unitaire: "Prix unitaire",
  prix: "Prix",
};
const AIDES = {
  fournisseur: "Société qui émet la facture (jamais NETIS, qui est le client).",
  numero_facture: "Tel qu'écrit sur la facture : pas un n° de commande ni de client.",
  date_paiement: "Paiement, sinon échéance, sinon date de facture. Sert à ranger le PDF.",
  devise: "Code à 3 lettres : EUR, USD, MAD…",
  taxe: "Total des taxes (TVA…). Vide s'il n'y en a pas.",
  total: "Montant total, taxes comprises.",
};
const libelle = (nom) => LIBELLES[nom] ?? `${nom.charAt(0).toUpperCase()}${nom.slice(1).replace(/_/g, " ")}`;
const MOIS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

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

// notification en bas à droite, quelques secondes
function notifier(texte, classe) {
  const t = element("div", { class: `toast ${classe}`, role: "status", texte });
  $("toasts").append(t);
  setTimeout(() => t.classList.add("sortie"), 5000);
  setTimeout(() => t.remove(), 5500);
}

const pluriel = (n, un, plusieurs) => `${n} ${n > 1 ? plusieurs : un}`;

// début estimé d'une facture en file : rang × durée moyenne des factures déjà lues (1 min par défaut)
function attenteEstimee(f) {
  const durees = factures.filter((g) => g.statut === "fini" && g.duree).map((g) => g.duree);
  const moyenne = durees.length ? durees.reduce((a, b) => a + b, 0) / durees.length : 60;
  return `≈ ${Math.max(1, Math.round(((f.rang || 1) * moyenne) / 60))} min`;
}

// dossier où l'enregistrement rangera le PDF (même règle que app.py : JJ/MM/AAAA ou AAAA-MM-JJ)
function dossierPrevu(date) {
  const t = String(date ?? "").trim();
  const m = t.match(/^(\d{2})\/(\d{2})\/(\d{4})$/) || t.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return null;
  const [j, mo, a] = t.includes("/") ? [+m[1], +m[2], +m[3]] : [+m[3], +m[2], +m[1]];
  const d = new Date(a, mo - 1, j);
  if (d.getFullYear() !== a || d.getMonth() !== mo - 1 || d.getDate() !== j) return null;
  return `Data/${a}/${String(mo).padStart(2, "0")}-${MOIS[mo - 1]}`;
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
  depot.classList.add("envoi");
  $("depot-texte").textContent = `Envoi de ${pluriel(pdfs.length, "PDF", "PDF")}…`;
  const rep = await fetch("/api/factures", { method: "POST", body: formulaire }).finally(() => {
    depot.classList.remove("envoi");
    $("depot-texte").textContent = "Glissez vos PDF ici";
  });
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
  else if (f && f.statut !== "fini") {   // pendant la lecture : rang et durée à jour (rien à saisir, on peut redessiner)
    $("etat-resultat").textContent = ETATS[f.statut](f)[0];
    dessinerProgression(f);
  }
}

function dessinerListe() {
  $("liste-vide").hidden = factures.length > 0;
  $("tableau-liste").hidden = !factures.length;
  $("excel").disabled = !factures.some((f) => f.statut === "fini");

  // compteurs au-dessus de la liste
  const nombre = (test) => factures.filter(test).length;
  const compteurs = [
    [nombre((f) => ["attente", "lecture", "llm"].includes(f.statut)), "en cours", "en cours", "encours"],
    [nombre((f) => f.statut === "fini" && !f.enregistree), "à vérifier", "à vérifier", "averifier"],
    [nombre((f) => f.enregistree), "enregistrée", "enregistrées", "ok"],
    [nombre((f) => f.statut === "erreur"), "en erreur", "en erreur", "erreur"],
  ].filter(([n]) => n);
  $("compteurs").replaceChildren(...compteurs.map(([n, un, plusieurs, classe]) =>
    element("span", { class: `compteur ${classe}`, texte: pluriel(n, un, plusieurs) })));

  $("liste").replaceChildren(...factures.map((f) => {
    const d = donnees(f);
    const resume = f.statut === "fini" && d
      ? [d.fournisseur, d.total != null ? `${montant(d.total)} ${d.devise || ""}` : null].filter(Boolean).join(" · ")
      : f.statut === "erreur" ? f.erreur : "";
    const [texte, classe] = ETATS[f.statut](f);
    const detail = brouillons[f.id] ? element("span", { class: "detail non-enregistre", texte: "Modifications non enregistrées" })
      : f.statut === "attente" ? element("span", { class: "detail", texte: `Début ${attenteEstimee(f)}` }) : "";
    return element("tr", { class: f.id === choisie ? "choisie" : "", onclick: () => choisir(f.id) }, [
      element("td", {}, [
        element("span", { class: "nom", texte: f.nom }),
        resume ? element("span", { class: "resume", texte: resume }) : "",
      ]),
      element("td", {}, [element("span", { class: `etat ${classe}`, texte, title: f.doublon || "" }), detail]),
      element("td", { class: "duree", texte: duree(f.duree) }),
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
  $("resultat-vide").hidden = !!f;
  $("contenu-resultat").hidden = !f;
  $("lien-pdf").hidden = $("basculer-pdf").hidden = !f;
  if (!f) {
    $("barre-actions").hidden = true;
    return;
  }
  affichee = `${f.id}:${f.statut}`;
  $("titre-resultat").textContent = f.nom;
  const [texte, classe] = ETATS[f.statut](f);
  $("etat-resultat").textContent = texte;
  $("etat-resultat").className = `etat ${classe}`;
  const pdf = `/api/factures/${f.id}/pdf`;
  $("lien-pdf").href = pdf;
  if ($("apercu-pdf").dataset.src !== pdf) {   // pas de rechargement de l'aperçu si c'est déjà ce PDF
    $("apercu-pdf").src = pdf;
    $("apercu-pdf").dataset.src = pdf;
  }

  const pret = f.statut === "fini";
  ["ajouter"].forEach((id) => ($(id).disabled = !pret));
  $("formulaire").hidden = !pret;
  $("progression").hidden = pret;
  $("barre-actions").hidden = !pret;
  if (!pret) {
    dessinerProgression(f);
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
  $("champs").replaceChildren(...meta.champs.map((c) => {
    const input = saisie(d[c.nom], c.type, original[c.nom], (v) => { modifier()[c.nom] = v; apresModification(); });
    input.id = `champ-${c.nom}`;
    if (c.nom === "date_paiement") input.placeholder = "JJ/MM/AAAA";
    const aide = element("small", { class: "aide-champ" });
    surveiller(input, c, aide);
    return element("div", { class: "champ" }, [
      element("label", { for: input.id, texte: libelle(c.nom), title: c.aide }),
      input,
      aide,
    ]);
  }));

  dessinerLignes();
  majBoutons();
  majAide();
}

// pendant la lecture : étapes et explication de ce qui se passe
const ETAPES = [["attente", "En file"], ["lecture", "Lecture du PDF"], ["llm", "Extraction par l'IA"], ["fini", "À vérifier"]];

function dessinerProgression(f) {
  const bloc = $("progression");
  bloc.className = `progression${f.statut === "erreur" ? " erreur-lecture" : ""}`;
  if (f.statut === "erreur") {
    bloc.replaceChildren(
      element("p", { class: "explication", texte: `La lecture a échoué : ${f.erreur}` }),
      element("p", { texte: "Vérifiez que le PDF s'ouvre, qu'il n'est pas protégé par un mot de passe ni scanné, puis déposez-le à nouveau." }),
    );
    return;
  }
  const rang = ETAPES.findIndex(([s]) => s === f.statut);
  const etapes = element("ol", { class: "etapes" }, ETAPES.map(([, nom], i) =>
    element("li", { class: `etape${i < rang ? " faite" : i === rang ? " active" : ""}` }, [
      element("span", { class: "etape-rond", texte: i < rang ? "✓" : i === rang ? "" : String(i + 1) }), nom,
    ])));
  const explications = {
    attente: `Les factures sont lues une par une : celle-ci est n° ${f.rang ?? "?"} dans la file. Début ${attenteEstimee(f)}.`,
    lecture: "Le texte et les tableaux du PDF sont extraits (quelques secondes).",
    llm: `Le modèle ${meta.modele} lit le texte et remplit les champs : 30 s à 1 min en général.`,
  };
  const lignes = [etapes, element("p", { class: "explication", texte: explications[f.statut] })];
  if (f.duree != null) lignes.push(element("p", { texte: `Depuis ${duree(f.duree)}. Le tableau apparaîtra ici dès la fin de la lecture.` }));
  if (f.duree > 120) {
    lignes.push(element("p", { class: "lent", texte: "Plus long que d'habitude : après une pause ou un redémarrage, l'outil de "
      + "lecture et le modèle doivent d'abord être chargés en mémoire (quelques minutes). Les factures suivantes seront plus rapides." }));
  }
  bloc.replaceChildren(...lignes);
}

// résumé des points à vérifier et dossier de rangement prévu (affichage seulement)
function majAide() {
  const f = facture(choisie);
  if (!f || f.statut !== "fini") return;
  const d = donnees(f);
  const points = meta.champs
    .map((c) => [c, probleme(c, d[c.nom] == null ? "" : String(d[c.nom]))])
    .filter(([, m]) => m)
    .map(([c, m]) => `${libelle(c.nom)} : ${m}`);
  if ($("controle").classList.contains("ko")) points.push($("controle").textContent.replace(/^⚠\s*/, ""));
  const alertes = $("alertes");
  if (points.length) {
    alertes.className = "alertes ko";
    alertes.replaceChildren(element("strong", { texte: `${pluriel(points.length, "point", "points")} à vérifier avant d'enregistrer` }),
      element("ul", {}, points.map((p) => element("li", { texte: p }))));
  } else {
    alertes.className = "alertes bon";
    alertes.replaceChildren(element("strong", { texte: "✓ Aucun problème détecté" }),
      element("span", { texte: "Comparez quand même avec le PDF, puis enregistrez." }));
  }

  const dossier = dossierPrevu(d.date_paiement);
  const destination = $("destination");
  destination.parentElement.classList.toggle("probleme", !dossier && !(f.enregistree && f.dossier));
  destination.textContent = f.enregistree && f.dossier && !brouillons[f.id] ? `PDF rangé dans ${f.dossier}`
    : dossier ? `À l'enregistrement, le PDF sera rangé dans ${dossier}`
    : "Date de paiement non valable : le PDF ne sera pas rangé à l'enregistrement";
}

// valeurs que le modèle écrit quand il ne trouve rien : le champ reste en rouge tant qu'il n'est pas corrigé
const NON_TROUVE = /^(n\/?a|null|none|unknown|inconnu|-)$/i;

function probleme(champ, texte) {
  const v = (texte || "").trim();
  if (NON_TROUVE.test(v)) return "Valeur non trouvée par le modèle : à compléter";
  if (!v && !champ.type.endsWith("?")) return "Champ vide : à compléter";
  // NETIS est le client : un fournisseur « NETIS » est une erreur du modèle
  if (champ.nom === "fournisseur" && /netis/i.test(v)) return "NETIS est le client, pas le fournisseur : à corriger";
  // numéro déjà enregistré en base (vérifié par le serveur) : rouge tant que le numéro n'est pas changé
  const f = facture(choisie);
  const normal = (n) => String(n ?? "").replace(/\s+/g, "").toUpperCase();
  if (champ.nom === "numero_facture" && f?.doublon && normal(v) === normal(f.resultat?.numero_facture)) return f.doublon;
  return "";
}

function surveiller(input, champ, aide) {
  const verifier = () => {
    const message = probleme(champ, input.value);
    input.classList.toggle("suspecte", !!message);
    input.title = message;
    // sous le champ : le problème s'il y en a un, sinon l'explication du champ
    aide.textContent = message || AIDES[champ.nom] || "";
    aide.classList.toggle("probleme", !!message);
  };
  verifier();
  input.addEventListener("input", verifier);
}

function dessinerLignes() {
  const f = facture(choisie);
  const d = donnees(f);
  const { nom, champs } = meta.liste;
  const lignes = d[nom] || [];
  const originales = f.resultat[nom] || [];
  $("titre-liste").textContent = `${libelle(nom)} (${lignes.length})`;

  $("entete-articles").replaceChildren(element("tr", {}, [
    element("th", { texte: "#" }),
    ...champs.map((c) => element("th", { class: estNombre(c.type) ? "nombre" : "", texte: libelle(c.nom), title: c.aide })),
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
  $("etat-enregistrement").className = `etat-enregistrement${brouillons[choisie] ? " averifier" : ""}`;
  majAide();
}

function majBoutons() {
  const modifie = Boolean(brouillons[choisie]);
  const f = facture(choisie);
  // actif aussi sans modification tant que la facture n'est pas enregistrée (le PDF n'est rangé qu'à l'enregistrement)
  $("enregistrer").disabled = !(f && f.statut === "fini" && (modifie || !f.enregistree));
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
    body: JSON.stringify(brouillons[id] ?? facture(id).resultat),
  });
  if (!rep.ok) {
    $("etat-enregistrement").textContent = `Échec de l'enregistrement (${rep.status})`;
    $("etat-enregistrement").className = "etat-enregistrement erreur";
    return notifier(`Échec de l'enregistrement (${rep.status})`, "erreur");
  }
  const maj = await rep.json();
  factures = factures.map((f) => (f.id === id ? maj : f));
  delete brouillons[id];
  affichee = null;
  dessinerResultat();
  apresModification();
  $("etat-enregistrement").textContent = maj.doublon ? `⚠ Non enregistré · ${maj.doublon}` : (maj.dossier
    ? `✓ Enregistré · PDF rangé dans ${maj.dossier}`
    : "✓ Enregistré · date de paiement non valable : PDF non rangé") + (maj.erreur_bd ? ` · ⚠ ${maj.erreur_bd}` : "");
  const classe = maj.doublon || maj.erreur_bd ? "erreur" : maj.dossier ? "ok" : "averifier";
  $("etat-enregistrement").className = `etat-enregistrement ${classe}`;
  notifier($("etat-enregistrement").textContent, classe);
});

// aperçu du PDF à côté du tableau : affiché ou masqué
$("basculer-pdf").addEventListener("click", () => {
  const masque = $("contenu-resultat").classList.toggle("sans-pdf");
  $("basculer-pdf").textContent = masque ? "Afficher le PDF" : "Masquer le PDF";
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
  else dessinerResultat();
  setInterval(actualiser, 2000);
})();
