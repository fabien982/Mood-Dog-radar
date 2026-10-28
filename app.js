/* Mood Dog Radar — application mobile (PWA) */
"use strict";

const STATUS = {
  etudier: "À étudier",
  revoir: "À revoir",
  postuler: "À postuler",
  envoye: "Candidature envoyée",
  relancer: "À relancer",
  retenu: "Retenu",
  refuse: "Refusé",
  masque: "Masqué",
};
const TYPE_CLASS = { "Appel à candidatures": "cand", "Recherche un food truck": "cand", "Marché public": "marche", "Événement": "event" };
const MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."];

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let DATA = { events: [], sources: {} };
let FESTS = [];
let view = "liste", filter = "demandes", query = "", dept = "", sort = "echeance";
let me = null, map = null, layer = null;

/* ---------- mémoire du téléphone (favoris, statuts, notes) ---------- */
const STORE_KEY = "mooddog-radar-v1";
let state = { fav: {}, status: {}, notes: {}, seen: {} };
try { state = Object.assign(state, JSON.parse(localStorage.getItem(STORE_KEY) || "{}")); } catch (e) { /* rien */ }
function save() { try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) { /* stockage plein ou bloqué */ } }

/* ---------- message de candidature ---------- */
const DEFAULT_SUBJECT = "Candidature food truck : {titre}";
const DEFAULT_BODY = `Bonjour,

Je vous contacte au sujet de « {titre} ».

Je m'appelle Fabien Ronsain et je gère le food truck Mood Dog Events. Nous serions ravis de participer à votre événement et d'y apporter notre bonne humeur.

Vous pouvez découvrir notre univers sur notre site : mooddogevents.fr
Je peux vous envoyer sur demande notre menu, des photos du camion, notre Kbis et notre attestation d'assurance.

Pourriez-vous m'indiquer les modalités de participation (dossier à fournir, tarif de l'emplacement, électricité disponible) ?

Bien cordialement,
Fabien Ronsain
Mood Dog Events
[ton numéro de téléphone]`;
function tpl() {
  return { subject: state.subject || DEFAULT_SUBJECT, body: state.body || DEFAULT_BODY };
}
function fill(text, ev) {
  return text.replaceAll("{titre}", ev.title.slice(0, 120)).replaceAll("{lien}", ev.url)
    .replaceAll("{lieu}", ev.city || "").replaceAll("{date}", ev.event_date ? fmtDate(ev.event_date) : "");
}
function hasContact(ev) {
  const c = ev.contact || {};
  return !!((c.emails && c.emails.length) || (c.phones && c.phones.length) || (c.forms && c.forms.length));
}
function contactBlock(ev) {
  const c = ev.contact || {};
  const t = tpl();
  const subject = encodeURIComponent(fill(t.subject, ev));
  const body = encodeURIComponent(fill(t.body, ev));
  const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return "lien"; } };
  const rows = [
    ...(c.emails || []).map((m) => `<a class="btn main" href="mailto:${esc(m)}?subject=${subject}&body=${body}">Envoyer ma demande à ${esc(m)}</a>`),
    ...(c.phones || []).map((p) => `<a class="btn" href="tel:${esc(p.replace(/\s/g, ""))}">Appeler le ${esc(p)}</a>`),
    ...(c.forms || []).map((f) => `<a class="btn" href="${esc(f)}" target="_blank" rel="noopener">Formulaire ou dossier (${esc(host(f))})</a>`),
  ];
  const none = rows.length ? "" : `<p class="help">Aucun contact trouvé automatiquement. Ouvre l'annonce : le contact est souvent en bas de la page ou dans la publication. Tu peux copier ton message et le coller dans un e-mail ou sur Messenger.</p>`;
  return `<p class="status-title">Candidater</p>${none}<div class="contact-list">${rows.join("")}
    <button class="btn" data-act="copy">Copier mon message</button></div>`;
}

/* ---------- dates ---------- */
function daysUntil(iso) {
  if (!iso) return null;
  const d = new Date(iso + "T23:59:59");
  return Math.ceil((d - new Date()) / 86400000) - 1;
}
function fmtDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${d} ${MONTHS[m - 1]} ${y}`;
}
function ago(iso) {
  if (!iso) return "jamais";
  const h = Math.round((Date.now() - new Date(iso)) / 3600000);
  if (h < 1) return "à l'instant";
  if (h < 24) return `il y a ${h} h`;
  const j = Math.round(h / 24);
  return `il y a ${j} jour${j > 1 ? "s" : ""}`;
}
function isDemande(ev) { return TYPE_CLASS[ev.type] === "cand" || TYPE_CLASS[ev.type] === "marche"; }
function isNew(ev) {
  return !state.seen[ev.id] && ev.found_at && Date.now() - new Date(ev.found_at) < 4 * 86400000;
}
function km(a, b) {
  const R = 6371, r = Math.PI / 180;
  const dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
  const x = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}

/* ---------- données ---------- */
async function load() {
  try {
    const r = await fetch("data/events.json?v=" + Date.now(), { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    DATA = await r.json();
    try {
      const rf = await fetch("data/festivals.json?v=" + Date.now(), { cache: "no-store" });
      if (rf.ok) FESTS = Object.values((await rf.json()).festivals || {});
    } catch (e) { /* pas encore de liste */ }
  } catch (e) {
    $("#subtitle").textContent = "Pas de connexion : dernière liste enregistrée";
  }
  fillDepts();
  render();
}

function fillDepts() {
  const set = [...new Set(DATA.events.map((e) => e.dept).filter(Boolean))].sort();
  $("#dept").innerHTML = '<option value="">Tous les départements</option>' +
    set.map((d) => `<option value="${esc(d)}"${d === dept ? " selected" : ""}>Département ${esc(d)}</option>`).join("");
}

function visible() {
  const q = query.trim().toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  let list = DATA.events.filter((ev) => {
    const st = state.status[ev.id] || "etudier";
    if (filter === "masque") return st === "masque";
    if (st === "masque") return false;
    if (filter === "demandes" && !isDemande(ev)) return false;
    if (filter === "verif" && !ev.fiable) return false;
    if (filter === "nouv" && !(isNew(ev) && isDemande(ev))) return false;
    if (filter === "revoir" && !["revoir", "postuler"].includes(st)) return false;
    if (filter === "envoye" && st !== "envoye") return false;
    if (filter === "relancer" && st !== "relancer") return false;
    if (filter === "fav" && !state.fav[ev.id]) return false;
    if (filter === "suivi" && st === "etudier" && !state.notes[ev.id]) return false;
    if (filter === "contact" && !hasContact(ev)) return false;
    if (filter === "date" && !ev.deadline && !ev.event_date) return false;
    if (["cand", "marche", "event"].includes(filter) && TYPE_CLASS[ev.type] !== filter) return false;
    if (dept && ev.dept !== dept) return false;
    if (q) {
      const hay = `${ev.title} ${ev.summary} ${ev.city} ${ev.organizer}`.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
      if (!q.split(/\s+/).every((w) => hay.includes(w))) return false;
    }
    return true;
  });
  const key = {
    echeance: (e) => e.deadline || e.event_date || "9999",
    recent: (e) => -new Date(e.found_at || 0),
    score: (e) => -(e.score || 0),
    distance: (e) => (me && e.lat != null ? km(me, e) : 1e9),
  }[sort];
  list.sort((a, b) => (filter === "demandes" && !!b.fiable - !!a.fiable) ||
    (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : (b.score || 0) - (a.score || 0)));
  return list;
}

/* ---------- affichage ---------- */
function stubFor(ev) {
  const dl = daysUntil(ev.deadline);
  if (dl != null) {
    const cls = dl <= 7 ? "urgent" : "";
    if (dl <= 0) return { cls: "urgent", big: "!", small: "Dernier jour pour postuler" };
    return { cls, big: dl, small: dl > 1 ? "jours pour postuler" : "jour pour postuler" };
  }
  if (ev.event_date) {
    const [, m, d] = ev.event_date.split("-").map(Number);
    return { cls: "jour", big: d, small: MONTHS[m - 1] };
  }
  return { cls: "calme", big: "?", small: "date à vérifier" };
}

function card(ev) {
  const s = stubFor(ev);
  const st = state.status[ev.id];
  const place = [ev.city, ev.dept && `(${ev.dept})`].filter(Boolean).join(" ");
  const dist = me && ev.lat != null ? ` à ${Math.round(km(me, ev))} km` : "";
  return `<button class="ticket ${s.cls}" data-id="${esc(ev.id)}">
    <div class="stub"><b>${esc(s.big)}</b><small>${esc(s.small)}</small></div>
    <div class="body">
      <div class="meta">
        ${isNew(ev) ? '<span class="tag new">Nouveau</span>' : ""}
        ${ev.fiable ? '<span class="tag ok">✓ Vérifiée</span>' : isDemande(ev) ? '<span class="tag warn">À vérifier</span>' : '<span class="tag">À démarcher</span>'}
        <span class="tag ${TYPE_CLASS[ev.type] || ""}">${esc(ev.type === "Événement" ? "Fête / événement" : ev.type)}</span>
        ${st && st !== "etudier" ? `<span class="tag st">${esc(STATUS[st])}</span>` : ""}
        ${hasContact(ev) ? '<span class="tag">Contact</span>' : ""}
        ${ev.hors_zone ? '<span class="tag">Festival suivi · hors zone</span>' : ""}
      </div>
      <h2>${esc(ev.title)}</h2>
      ${ev.summary && ev.summary !== ev.title ? `<p class="snip">${esc(ev.summary)}</p>` : ""}
      <p class="where">${esc(place || ev.organizer || "Lieu non précisé")}${esc(dist)} <span class="src">${esc(ev.via || ev.source)}</span></p>
    </div>
    ${state.fav[ev.id] ? '<span class="fav-mark" aria-label="Favori">★</span>' : ""}
  </button>`;
}

function emptyState() {
  if (!DATA.events.length) {
    return `<div class="empty"><strong>Aucune opportunité pour l'instant</strong>
      La recherche automatique passe deux fois par jour. Tu peux la lancer tout de suite depuis l'onglet
      Actions de GitHub (voir le bouton ⓘ en haut).</div>`;
  }
  const tips = {
    verif: ["Aucune demande vérifiée pour l'instant", "Les demandes vérifiées viennent des sites officiels (mairies, marchés publics). Regarde aussi « Demandes »."],
    nouv: ["Aucune nouvelle demande", "Le robot repasse vers 7 h et 17 h. En attendant, regarde « Toutes »."],
    revoir: ["Rien à revoir", "Dans une fiche, choisis « À revoir » pour la retrouver ici."],
    envoye: ["Aucune candidature en attente", "Quand tu as postulé, choisis « Candidature envoyée » dans la fiche."],
    relancer: ["Personne à relancer", "Choisis « À relancer » dans une fiche quand un organisateur ne répond pas."],
  }[filter];
  if (tips && !query) return `<div class="empty"><strong>${tips[0]}</strong>${tips[1]}</div>`;
  return `<div class="empty"><strong>Rien avec ces filtres</strong>Essaie « Toutes » ou vide la recherche.</div>`;
}

function render() {
  const list = visible();
  const total = DATA.events.filter((e) => state.status[e.id] !== "masque").length;
  const dem = DATA.events.filter((e) => isDemande(e) && state.status[e.id] !== "masque");
  const ok = dem.filter((e) => e.fiable).length;
  $("#subtitle").textContent = `${dem.length} demande${dem.length > 1 ? "s" : ""} de food truck, dont ${ok} vérifiée${ok > 1 ? "s" : ""} · ${total - dem.length} fêtes à démarcher. Mise à jour ${ago(DATA.updated)}`;
  const alive = DATA.events.filter((e) => state.status[e.id] !== "masque");
  const count = (k) => alive.filter((e) => state.status[e.id] === k).length;
  $("#n-nouv").textContent = alive.filter((e) => isNew(e) && isDemande(e)).length;
  $("#n-relancer").textContent = count("relancer");
  $("#n-envoye").textContent = count("envoye");
  $("#n-fest").textContent = FESTS.length || "–";
  const fo = FESTS.filter((f) => f.appel).length;
  $("#n-fest-open").textContent = fo ? `${fo} avec candidatures ouvertes` : "Touche pour voir la liste";
  if (filter === "festivals") { $("#liste").innerHTML = festivalList(); if (view === "carte") drawMap([]); return; }
  $("#liste").innerHTML = list.length ? list.map(card).join("") : emptyState();
  if (view === "carte") drawMap(list);
}

/* ---------- tous les festivals de PACA ---------- */
function festivalList() {
  const q = query.trim().toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  const today = new Date().toISOString().slice(0, 10);
  let list = FESTS.filter((f) => !dept || f.dept === dept);
  const past = (f) => !!f.date && f.date < today && !f.appel;
  if (q) list = list.filter((f) => `${f.name} ${f.town}`.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").includes(q));
  list.sort((a, b) => (!!b.appel - !!a.appel) || (past(a) - past(b))
    || (past(a) ? String(a.date).slice(5).localeCompare(String(b.date).slice(5)) : String(a.date || "9999").localeCompare(String(b.date || "9999"))));
  if (!list.length) {
    return `<div class="empty"><strong>${FESTS.length ? "Aucun festival avec ces filtres" : "Liste des festivals en préparation"}</strong>
      ${FESTS.length ? "Vide la recherche ou change de département." : "Le robot récupère la liste de tous les festivals de PACA à son prochain passage."}</div>`;
  }
  const open = list.filter((f) => f.appel).length;
  const nPast = list.filter(past).length;
  const head = `<p class="help">${list.length} festivals en PACA (${list.length - nPast} à venir, ${nPast} dont l'édition ${new Date().getFullYear()} est passée : ils sont plus bas, on guette leur prochaine édition). Le robot vérifie chaque matin quelques festivals à tour de rôle : dès que l'un ouvre ses candidatures restauration / food truck, il passe en haut de la liste.${open ? ` <b>${open} avec un appel ouvert.</b>` : ""}</p>`;
  return head + list.map((f) => {
    const [y, m, d] = (f.date || "").split("-").map(Number);
    const stub = past(f) ? { cls: "calme", big: MONTHS[m - 1], small: `édition ${y} passée` }
      : f.date ? { cls: "jour", big: d, small: `${MONTHS[m - 1]} ${y}` } : { cls: "calme", big: "?", small: "date à venir" };
    const status = f.appel
      ? `<span class="tag ok">✓ Candidatures ouvertes</span>${f.appel.deadline ? `<span class="tag">clôture ${esc(fmtDate(f.appel.deadline))}</span>` : ""}`
      : f.checked ? `<span class="tag">Pas encore d'appel · vérifié le ${esc(fmtDate(f.checked))}</span>` : `<span class="tag">Vérification à venir</span>`;
    const target = f.appel ? `data-id="${esc(f.appel.id)}"` : `data-url="${esc(f.url)}"`;
    return `<button class="ticket ${f.appel ? "" : stub.cls}" ${target}>
      <div class="stub"><b>${esc(f.appel ? "✓" : stub.big)}</b><small>${esc(f.appel ? "candidatures ouvertes" : stub.small)}</small></div>
      <div class="body"><div class="meta">${status}</div>
        <h2>${esc(f.name)}</h2>
        <p class="where">${esc([f.town, f.dept && `(${f.dept})`].filter(Boolean).join(" ") || "PACA")}</p></div>
    </button>`;
  }).join("");
}

/* ---------- carte ---------- */
function drawMap(list) {
  if (!window.L) { $("#map").textContent = "La carte a besoin d'une connexion internet."; return; }
  if (!map) {
    map = L.map("map", { zoomControl: false }).setView([43.8, 5.9], 8);
    L.control.zoom({ position: "bottomright" }).addTo(map);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18, attribution: "© OpenStreetMap",
    }).addTo(map);
    layer = L.layerGroup().addTo(map);
  }
  layer.clearLayers();
  const colors = { cand: "#E8A317", marche: "#3A64C8", event: "#5E7A3A" };
  const pts = [];
  list.filter((e) => e.lat != null).forEach((ev) => {
    const c = colors[TYPE_CLASS[ev.type]] || "#C8322B";
    const m = L.circleMarker([ev.lat, ev.lon], {
      radius: ev.approx ? 7 : 10, color: "#16243F", weight: 2, fillColor: c, fillOpacity: ev.approx ? 0.55 : 0.95,
      dashArray: ev.approx ? "3 3" : null,
    });
    const div = document.createElement("div");
    div.innerHTML = `<b>${esc(ev.title.slice(0, 90))}</b><br>${esc(ev.city || "Lieu approximatif")}<br>`;
    const b = document.createElement("button");
    b.textContent = "Voir la fiche";
    b.onclick = () => openEvent(ev.id);
    div.appendChild(b);
    m.bindPopup(div);
    m.addTo(layer);
    pts.push([ev.lat, ev.lon]);
  });
  if (me) {
    L.circleMarker([me.lat, me.lon], { radius: 8, color: "#fff", weight: 3, fillColor: "#C8322B", fillOpacity: 1 })
      .bindPopup("Vous êtes ici").addTo(layer);
    pts.push([me.lat, me.lon]);
  }
  setTimeout(() => {
    map.invalidateSize();
    if (pts.length) map.fitBounds(pts, { padding: [30, 30], maxZoom: 11 });
  }, 60);
}

/* ---------- fiche ---------- */
function openEvent(id) {
  const ev = DATA.events.find((e) => e.id === id);
  if (!ev) return;
  state.seen[id] = true; save();
  const st = state.status[id] || "etudier";
  const dest = ev.lat != null && !ev.approx ? `${ev.lat},${ev.lon}` : encodeURIComponent(ev.city || "");
  const facts = [
    ["Clôture", ev.deadline && `${fmtDate(ev.deadline)} (${Math.max(0, daysUntil(ev.deadline))} j)`],
    ["Date", ev.event_date && fmtDate(ev.event_date)],
    ["Lieu", [ev.city, ev.dept && `(${ev.dept})`].filter(Boolean).join(" ")],
    ["Organisateur", ev.organizer],
    ["Source", ev.via ? `${ev.via} (${ev.source})` : ev.source],
    ["Trouvé", ago(ev.found_at)],
  ].filter(([, v]) => v);

  $("#sheet-body").innerHTML = `
    <div class="grip"></div>
    <div class="meta"><span class="tag ${TYPE_CLASS[ev.type] || ""}">${esc(ev.type)}</span></div>
    <h2>${esc(ev.title)}</h2>
    ${ev.summary && ev.summary !== ev.title ? `<p class="summary">${esc(ev.summary)}</p>` : ""}
    <dl class="facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <div class="actions">
      <a class="btn main" href="${esc(ev.url)}" target="_blank" rel="noopener">Ouvrir l'annonce</a>
      <button class="btn ${state.fav[id] ? "on" : ""}" data-act="fav">${state.fav[id] ? "★ En favori" : "☆ Favori"}</button>
      <button class="btn" data-act="ics" ${ev.deadline || ev.event_date ? "" : "disabled"}>Mettre dans l'agenda</button>
      ${dest ? `<a class="btn" href="https://www.google.com/maps/dir/?api=1&destination=${dest}" target="_blank" rel="noopener">Itinéraire</a>` : ""}
      <button class="btn" data-act="share">Partager</button>
    </div>
    ${contactBlock(ev)}
    <p class="status-title">Où j'en suis</p>
    <div class="statuses">${Object.entries(STATUS).map(([k, v]) =>
      `<button data-st="${k}" class="${k === st ? "on" : ""}">${esc(v)}</button>`).join("")}</div>
    <p class="status-title">Mes notes</p>
    <textarea class="note" id="note" placeholder="Contact, tarif de l'emplacement, pièces à fournir…">${esc(state.notes[id] || "")}</textarea>
    <button class="btn close" data-act="close">Fermer</button>`;

  const body = $("#sheet-body");
  body.onclick = async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.dataset.st) {
      state.status[id] = t.dataset.st; save();
      body.querySelectorAll(".statuses button").forEach((b) => b.classList.toggle("on", b === t));
      render();
    } else if (t.dataset.act === "fav") {
      state.fav[id] = !state.fav[id]; if (!state.fav[id]) delete state.fav[id]; save();
      t.classList.toggle("on", !!state.fav[id]);
      t.textContent = state.fav[id] ? "★ En favori" : "☆ Favori";
      render();
    } else if (t.dataset.act === "ics") {
      downloadIcs(ev);
    } else if (t.dataset.act === "share") {
      const data = { title: ev.title, text: `${ev.title} ${ev.deadline ? "(clôture " + fmtDate(ev.deadline) + ")" : ""}`, url: ev.url };
      try { if (navigator.share) await navigator.share(data); else { await navigator.clipboard.writeText(ev.url); t.textContent = "Lien copié"; } } catch (err) { /* partage annulé */ }
    } else if (t.dataset.act === "copy") {
      const t2 = tpl();
      const text = fill(t2.subject, ev) + "\n\n" + fill(t2.body, ev);
      try { await navigator.clipboard.writeText(text); t.textContent = "Message copié"; } catch (err) { t.textContent = "Copie impossible"; }
    } else if (t.dataset.act === "close") {
      $("#sheet").close();
    }
  };
  $("#note").oninput = (e) => {
    const v = e.target.value.trim();
    if (v) state.notes[id] = e.target.value; else delete state.notes[id];
    save();
  };
  $("#sheet").showModal();
  render();
}

function downloadIcs(ev) {
  const x = (s) => String(s || "").replace(/\\/g, "\\\\").replace(/[,;]/g, (c) => "\\" + c).replace(/\n/g, "\\n");
  const day = (iso) => iso.replaceAll("-", "");
  const next = (iso) => { const d = new Date(iso + "T12:00:00"); d.setDate(d.getDate() + 1); return d.toISOString().slice(0, 10).replaceAll("-", ""); };
  const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 15) + "Z";
  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Mood Dog Radar//FR", "CALSCALE:GREGORIAN"];
  const add = (uid, date, title) => lines.push("BEGIN:VEVENT", `UID:${uid}@mooddog-radar`, `DTSTAMP:${stamp}`,
    `DTSTART;VALUE=DATE:${day(date)}`, `DTEND;VALUE=DATE:${next(date)}`, `SUMMARY:${x(title)}`,
    `DESCRIPTION:${x(ev.url)}`, `LOCATION:${x(ev.city || "")}`,
    "BEGIN:VALARM", "TRIGGER:-P2D", "ACTION:DISPLAY", `DESCRIPTION:${x(title)}`, "END:VALARM", "END:VEVENT");
  if (ev.deadline) add(ev.id + "-cloture", ev.deadline, "Clôture candidature : " + ev.title.slice(0, 80));
  if (ev.event_date) add(ev.id + "-jour", ev.event_date, "Food truck : " + ev.title.slice(0, 80));
  lines.push("END:VCALENDAR");
  const blob = new Blob([lines.join("\r\n")], { type: "text/calendar;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "mood-dog-" + ev.id + ".ics";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

/* ---------- infos sur la recherche ---------- */
function openInfo() {
  const [owner] = location.hostname.split(".");
  const repo = location.pathname.split("/").filter(Boolean)[0];
  const actions = location.hostname.endsWith("github.io") && repo ? `https://github.com/${owner}/${repo}/actions` : null;
  const src = Object.entries(DATA.sources || {}).map(([name, s]) => {
    const cls = !s.ok ? "ko" : s.note ? "off" : "";
    const detail = s.note || (s.ok ? `${s.found} trouvée${s.found > 1 ? "s" : ""}` : "En panne : " + (s.error || (s.errors || []).join(", ")));
    const extra = s.ok && s.errors && s.errors.length ? `<br><small>Pages en erreur : ${esc(s.errors.join(", "))}</small>` : "";
    return `<li><b><span class="dot ${cls}"></span>${esc(name)}</b>${esc(detail)}${extra}</li>`;
  }).join("");
  $("#sheet-body").innerHTML = `
    <div class="grip"></div>
    <h2>Recherche automatique</h2>
    <p class="help">Dernier passage ${esc(ago(DATA.updated))}. Le robot tourne tous les jours vers 7 h et 17 h.</p>
    <ul class="src-list">${src || "<li>Aucune recherche effectuée pour l'instant.</li>"}</ul>
    ${actions ? `<a class="btn main" href="${actions}" target="_blank" rel="noopener">Lancer une recherche maintenant</a>` : ""}
    <p class="help">Sur GitHub : ouvre « Recherche automatique », puis « Run workflow ». Les résultats arrivent ici en 2 à 3 minutes.</p>
    <p class="status-title">Mon message de candidature</p>
    <p class="help">Utilisé par le bouton « Envoyer ma demande ». {titre} est remplacé par le nom de l'annonce.</p>
    <input class="note subject" id="tpl-subject" value="${esc(tpl().subject)}" aria-label="Objet du message">
    <textarea class="note tpl" id="tpl-body" aria-label="Texte du message">${esc(tpl().body)}</textarea>
    <button class="btn" data-act="reset-tpl">Rétablir le message d'origine</button>
    <p class="help">Tes favoris, statuts, notes et ton message restent sur ce téléphone.</p>
    <button class="btn close" data-act="close">Fermer</button>`;
  $("#tpl-subject").oninput = (e) => { state.subject = e.target.value; save(); };
  $("#tpl-body").oninput = (e) => { state.body = e.target.value; save(); };
  $("#sheet-body").onclick = (e) => {
    if (e.target.closest("[data-act=close]")) $("#sheet").close();
    if (e.target.closest("[data-act=reset-tpl]")) {
      delete state.subject; delete state.body; save();
      $("#tpl-subject").value = DEFAULT_SUBJECT; $("#tpl-body").value = DEFAULT_BODY;
    }
  };
  $("#sheet").showModal();
}

/* ---------- interactions ---------- */
$("#liste").addEventListener("click", (e) => {
  const t = e.target.closest(".ticket");
  if (!t) return;
  if (t.dataset.id) openEvent(t.dataset.id);
  else if (t.dataset.url) window.open(t.dataset.url, "_blank", "noopener");
});
$("#chips").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  filter = b.dataset.f;
  document.querySelectorAll("#chips button").forEach((x) => x.classList.toggle("on", x === b));
  render();
});
document.querySelectorAll(".switch button").forEach((b) => b.addEventListener("click", () => {
  view = b.dataset.view;
  document.querySelectorAll(".switch button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  $("#liste").hidden = view !== "liste";
  $("#carte").hidden = view !== "carte";
  render();
}));
let timer;
$("#q").addEventListener("input", (e) => { clearTimeout(timer); timer = setTimeout(() => { query = e.target.value; render(); }, 150); });
$("#dept").addEventListener("change", (e) => { dept = e.target.value; render(); });
$("#sort").addEventListener("change", (e) => {
  sort = e.target.value;
  if (sort === "distance" && !me) {
    if (!navigator.geolocation) { alert("La localisation n'est pas disponible sur cet appareil."); return; }
    navigator.geolocation.getCurrentPosition(
      (p) => { me = { lat: p.coords.latitude, lon: p.coords.longitude }; render(); },
      () => { alert("Autorise la localisation pour trier par distance."); e.target.value = sort = "echeance"; render(); },
      { enableHighAccuracy: false, timeout: 10000 },
    );
  }
  render();
});
$("#btn-info").addEventListener("click", openInfo);
document.querySelectorAll(".stat").forEach((b) => b.addEventListener("click", () => {
  const target = document.querySelector(`#chips button[data-f="${b.dataset.go}"]`);
  if (target) { target.click(); target.scrollIntoView({ block: "nearest", inline: "center" }); }
  $("#chips").scrollIntoView({ behavior: "smooth", block: "start" });
}));
(() => {
  const d = new Date();
  $("#today span").textContent = d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit", year: "numeric" });
})();
$("#sheet").addEventListener("click", (e) => { if (e.target.id === "sheet") e.target.close(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });

if ("serviceWorker" in navigator) {
  const hadSW = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.register("sw.js").then((r) => r.update()).catch(() => {});
  // nouvelle version installée : on recharge une fois pour l'afficher tout de suite
  navigator.serviceWorker.addEventListener("controllerchange", () => { if (hadSW && !window.__reloaded) { window.__reloaded = true; location.reload(); } });
}
load();
