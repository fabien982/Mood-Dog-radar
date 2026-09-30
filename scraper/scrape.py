#!/usr/bin/env python3
"""
Mood Dog Radar — robot de recherche d'opportunités pour food truck.

Sources :
  1. BOAMP (marchés publics, API ouverte de l'État)
  2. Alertes Google (flux RSS)
  3. Recherche web (API Brave Search ou Serper, si une clé est fournie)
  4. Pages surveillées (sites de mairies, offices de tourisme, flux RSS)

Résultat : data/events.json, lu par l'application.
Variables d'environnement facultatives :
  BRAVE_API_KEY, SERPER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, APP_URL
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
EVENTS_FILE = DATA_DIR / "events.json"
GEOCACHE_FILE = DATA_DIR / "geocache.json"
CONFIG_FILE = ROOT / "scraper" / "config.yaml"

TIMEOUT = 25
TODAY = date.today()
NOW = datetime.now(timezone.utc)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; MoodDogRadar/1.0; +https://mooddogevents.fr)",
    "Accept-Language": "fr-FR,fr;q=0.9",
})


def log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- texte ---

def norm(text: str) -> str:
    """minuscules, sans accents, espaces simples"""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.replace("’", "'").lower()
    return re.sub(r"\s+", " ", text).strip()


def clean(text: str) -> str:
    """retire le HTML et les espaces en trop"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def contains_any(normalized_text: str, words: list[str]) -> list[str]:
    found = []
    for w in words:
        nw = norm(w)
        if re.search(r"(?<![a-z0-9])" + re.escape(nw) + r"(?![a-z0-9])", normalized_text):
            found.append(w)
    return found


# ---------------------------------------------------------------- dates ---

MONTHS = {
    "janvier": 1, "janv": 1, "fevrier": 2, "fevr": 2, "mars": 3, "avril": 4, "avr": 4,
    "mai": 5, "juin": 6, "juillet": 7, "juil": 7, "aout": 8, "septembre": 9, "sept": 9,
    "octobre": 10, "oct": 10, "novembre": 11, "nov": 11, "decembre": 12, "dec": 12,
}
RE_TXT = re.compile(
    r"\b(\d{1,2})(?:er)?\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?(?:\s+(\d{4}))?\b"
)
RE_NUM = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\b")
RE_DEADLINE = re.compile(
    r"(avant le|jusqu'?au|date limite|au plus tard|clotur\w*|limite de (?:depot|reception|candidature)"
    r"|dossiers? a (?:rendre|deposer|renvoyer|retourner)|candidatures? (?:ouvertes? )?jusqu)"
)


def _mk_date(d: int, m: int, y: int | None) -> date | None:
    guessed = y is None
    if y is None:
        y = TODAY.year
    elif y < 100:
        y += 2000
    try:
        result = date(y, m, d)
    except ValueError:
        return None
    if guessed and result < TODAY - timedelta(days=60):
        try:
            result = date(y + 1, m, d)
        except ValueError:
            return None
    return result


def find_dates(text: str) -> tuple[str | None, str | None]:
    """renvoie (date_limite, date_evenement) au format AAAA-MM-JJ si on en trouve"""
    t = norm(text)
    found: list[tuple[int, date]] = []
    for m in RE_TXT.finditer(t):
        d = _mk_date(int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)) if m.group(3) else None)
        if d:
            found.append((m.start(), d))
    for m in RE_NUM.finditer(t):
        d = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((m.start(), d))
    found.sort()
    lo, hi = TODAY - timedelta(days=30), TODAY + timedelta(days=550)
    deadline = event_date = None
    for pos, d in found:
        if not (lo <= d <= hi):
            continue
        before = t[max(0, pos - 70):pos]
        if deadline is None and RE_DEADLINE.search(before):
            deadline = d
        elif event_date is None and d >= TODAY:
            event_date = d
    return (deadline.isoformat() if deadline else None,
            event_date.isoformat() if event_date else None)


# -------------------------------------------------------------- contacts ---

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
RE_PHONE = re.compile(r"(?<!\d)(?:(?:\+|00)33[\s.\-]?|0)[1-9](?:[\s.\-]?\d{2}){4}(?!\d)")
RE_FORM = re.compile(r"(formulaire|candidat|inscri|dossier|exposant|forms\.gle|docs\.google\.com/forms|"
                     r"framaforms|helloasso|typeform|jotform|tally\.so)", re.I)
BAD_EMAIL = ("example", "exemple", "sentry", "wixpress", "noreply", "no-reply", "domain", "votre", "email@",
             "nom@", "prenom", "@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
SKIP_FETCH = ("facebook.com", "fb.com", "instagram.com", "x.com", "twitter.com", "tiktok.com",
              "linkedin.com", "youtube.com", "leboncoin.fr")


def _phone_fmt(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0033"):
        digits = "0" + digits[4:]
    elif digits.startswith("33") and len(digits) == 11:
        digits = "0" + digits[2:]
    if len(digits) != 10 or not digits.startswith("0"):
        return None
    return " ".join(digits[i:i + 2] for i in range(0, 10, 2))


def extract_contacts(text: str = "", soup=None, base: str = "") -> dict:
    emails, phones, forms = [], [], []
    blob = text or ""
    if soup is not None:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                blob += " " + href[7:].split("?")[0]
            elif href.lower().startswith("tel:"):
                blob += " " + href[4:]
            else:
                label = clean(a.get_text(" "))
                if RE_FORM.search(href) or RE_FORM.search(label):
                    link = urljoin(base, href)
                    if link.startswith("http") and link not in forms and link.rstrip("/") != base.rstrip("/"):
                        forms.append(link)
        blob += " " + soup.get_text(" ")
    for m in RE_EMAIL.findall(blob):
        e = m.strip(".").lower()
        if not any(b in e for b in BAD_EMAIL) and e not in emails:
            emails.append(e)
    for m in RE_PHONE.findall(blob):
        ph = _phone_fmt(m)
        if ph and ph not in phones:
            phones.append(ph)
    return {"emails": emails[:3], "phones": phones[:3], "forms": forms[:3]}


def merge_contacts(a: dict | None, b: dict | None) -> dict:
    out = {"emails": [], "phones": [], "forms": []}
    for src in (a or {}, b or {}):
        for k in out:
            for v in src.get(k, []):
                if v not in out[k]:
                    out[k].append(v)
    return {k: v[:3] for k, v in out.items()}


def enrich_contacts(events: list[dict], budget: int = 60) -> None:
    """ouvre la page de chaque nouvelle annonce pour y chercher e-mail, téléphone, formulaire"""
    for ev in events:
        if ev.get("contact_checked") or budget <= 0:
            continue
        host = urlparse(ev["url"]).netloc.lower()
        ev["contact_checked"] = True
        if any(host.endswith(d) for d in SKIP_FETCH):
            continue
        budget -= 1
        try:
            r = session.get(ev["url"], timeout=12)
            if not r.ok or "html" not in r.headers.get("content-type", ""):
                continue
            soup = BeautifulSoup(r.content[:1_500_000], "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            ev["contact"] = merge_contacts(ev.get("contact"), extract_contacts(soup=soup, base=ev["url"]))
        except Exception:
            pass
        time.sleep(0.3)


# ---------------------------------------------------------- géolocalisation ---

DEPT_CENTER = {
    "04": (44.09, 6.24), "05": (44.66, 6.26), "06": (43.94, 7.18),
    "13": (43.54, 5.09), "83": (43.46, 6.22), "84": (44.00, 5.18),
}
# code postal suivi d'un nom de ville (évite de confondre avec « 10000 visiteurs »)
RE_DEPT_PAREN = re.compile(r"\((0[1-9]|[1-8]\d|9[0-5]|2[AB])\)")
REGION_WORDS = ["provence", "paca", "cote d'azur", "luberon", "camargue", "alpilles", "verdon",
                "sainte-baume", "calanques", "riviera", "haute-provence", "pays d'aix"]
RE_POSTCODE = re.compile(r"\b((?:0[1-9]|[1-8]\d|9[0-5])\d{3})\s+[A-ZÀ-Ý]")

try:
    GEOCACHE: dict = json.loads(GEOCACHE_FILE.read_text("utf-8"))
except Exception:
    GEOCACHE = {}


def geocode(query: str) -> dict | None:
    key = norm(query)
    if not key:
        return None
    if key in GEOCACHE:
        return GEOCACHE[key]
    answered = False
    for url in ("https://data.geopf.fr/geocodage/search", "https://api-adresse.data.gouv.fr/search/"):
        try:
            r = session.get(url, params={"q": query, "type": "municipality", "limit": 1}, timeout=15)
            if not r.ok:
                continue
            answered = True
            feats = r.json().get("features") or []
            if feats:
                lon, lat = feats[0]["geometry"]["coordinates"]
                p = feats[0].get("properties", {})
                res = {
                    "lat": round(lat, 5), "lon": round(lon, 5),
                    "city": p.get("city") or p.get("name") or query,
                    "dept": (p.get("context") or "").split(",")[0].strip() or None,
                }
                GEOCACHE[key] = res
                return res
            break
        except Exception:
            continue
    if answered:
        GEOCACHE[key] = None  # rien trouvé : on ne redemande pas
    return None


RE_PLACE = re.compile(
    r"(?:\b[àa]|\bde|\bd'|\bdu|\bau|\bsur)\s+"
    r"((?:Saint|Sainte|St|Ste|Le|La|Les|L')?[\s'-]?[A-ZÉÈÂÎ][\w'’-]+(?:[\s-](?:de|du|des|la|le|les|sur|en|d')?[\s-]?[A-ZÉÈÂÎ][\w'’-]+){0,3})"
)
NOT_PLACES = {"noel", "provence", "france", "musique", "jazz", "rock", "la", "le", "les", "fete", "festival",
              "l'ete", "printemps", "automne", "hiver", "food", "truck", "mairie", "ville", "office", "tourisme"}


def geocode_in_zone(name: str) -> dict | None:
    """cherche une commune de ce nom DANS tes départements (il y a plusieurs Saint-Maximin en France)"""
    key = "zone:" + norm(name)
    if key in GEOCACHE:
        return GEOCACHE[key]
    for url in ("https://data.geopf.fr/geocodage/search", "https://api-adresse.data.gouv.fr/search/"):
        try:
            r = session.get(url, params={"q": name, "type": "municipality", "limit": 10}, timeout=15)
            if not r.ok:
                continue
            for f in r.json().get("features") or []:
                p = f.get("properties", {})
                dept = (p.get("context") or "").split(",")[0].strip()
                if dept in DEPTS and p.get("score", 1) >= 0.6:
                    lon, lat = f["geometry"]["coordinates"]
                    res = {"lat": round(lat, 5), "lon": round(lon, 5), "city": p.get("city") or p.get("name"), "dept": dept}
                    GEOCACHE[key] = res
                    return res
            GEOCACHE[key] = None
            return None
        except Exception:
            continue
    return None


def guess_place(text: str) -> dict | None:
    tries = 0
    for m in RE_PLACE.finditer(text):
        name = m.group(1).strip(" -'’")
        if norm(name) in NOT_PLACES or len(name) < 3:
            continue
        g = geocode_in_zone(name)
        if g:
            return g
        tries += 1
        if tries >= 3:
            break
    return None


# ---------------------------------------------------------------- config ---

CFG = yaml.safe_load(CONFIG_FILE.read_text("utf-8")) or {}
KW = CFG.get("motscles", {})
KW_MAIN = KW.get("principaux", [])
KW_BONUS = KW.get("bonus", [])
KW_EXCL = KW.get("exclus", [])
DEPTS = [str(d).zfill(2) for d in CFG.get("departements", [])]
TOWNS = sorted(CFG.get("villes", []), key=len, reverse=True)

# Une annonce du web doit montrer qu'on CHERCHE un food truck (pas un food truck qui fait sa pub).
RE_SIGNAL = re.compile(
    r"appel a (candidature|manifestation|projet)|\bami\b|\baot\b|occupation (temporaire )?du domaine public"
    r"|dossier de candidature|candidatures? (ouvertes|jusqu|avant)|inscriptions? (ouvertes|exposants)"
    r"|(recherch|cherch)\w*\s+(\S+\s+){0,4}(food[\s-]?trucks?|foodtrucks?|restauration|traiteurs?|exposants|producteurs|commercants)"
    r"|(appel|recherche|place|emplacement)s?\s+(\S+\s+){0,3}(exposants|commercants ambulants)"
    r"|emplacements? (disponibles?|libres?|a pourvoir|pour (un |des )?food)"
)
RE_STRONG = re.compile(
    r"appel a (candidature|manifestation)|dossier de candidature|\baot\b|occupation (temporaire )?du domaine public"
    r"|(recherch|cherch)\w*\s+(\S+\s+){0,4}(food[\s-]?trucks?|foodtrucks?)"
)
RE_PROMO = re.compile(
    r"retrouvez[- ]nous|venez nous (voir|retrouver)|nous serons|on vous attend|suivez[- ]nous|notre (menu|carte)"
    r"|a bientot|commandez|tous les (lundis|mardis|mercredis|jeudis|vendredis|samedis|dimanches)"
)


def has_signal(nt: str) -> bool:
    extra = [norm(x) for x in CFG.get("signaux", [])]
    return bool(RE_SIGNAL.search(nt)) or any(x in nt for x in extra)


def is_promo(nt: str) -> bool:
    return bool(RE_PROMO.search(nt)) and not RE_STRONG.search(nt)


KW_EVENTS = KW.get("evenements", ["fete", "festival", "marche nocturne", "vide-grenier", "brocante",
                                    "feria", "foire", "corso", "carnaval", "concert", "guinguette"])
RE_EVENT_BAD = re.compile(r"\bannule|retour en images|en photos|bilan de|replay|resultats? du")


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    r = math.radians
    a = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def via_label(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    for dom, label in (("instagram.com", "Instagram"), ("facebook.com", "Facebook"), ("fb.com", "Facebook")):
        if host.endswith(dom):
            return label
    return None


SOURCE_WEIGHT = {"BOAMP": 50, "Alerte Google": 40, "Recherche web": 30, "Page surveillée": 45}


def classify(nt: str, source: str) -> str:
    if source == "BOAMP":
        return "Marché public"
    if re.search(r"appel a (candidature|manifestation|projet)|\bami\b|dossier de candidature"
                 r"|candidatures? (ouvertes|jusqu|avant)|\baot\b|occupation (temporaire )?du domaine public", nt):
        return "Appel à candidatures"
    if re.search(r"(recherch|cherch)\w*\s+(\S+\s+){0,4}(food[\s-]?trucks?|foodtrucks?|exposants|producteurs)", nt):
        return "Recherche un food truck"
    return "Événement"


def locate(text: str, city_hint: str = "", depts: list[str] | None = None) -> dict:
    loc: dict = {"city": None, "dept": None, "lat": None, "lon": None}
    nt = norm(text)
    query = city_hint
    if not query:
        for town in TOWNS:
            if re.search(r"(?<![a-z])" + re.escape(norm(town)) + r"(?![a-z])", nt):
                query = town
                break
    postcode_dept = None
    m = RE_POSTCODE.search(text)
    if m:
        postcode_dept = m.group(1)[:2].upper()
        if not query:
            query = m.group(1)
    if query:
        g = geocode(query)
        if g:
            loc.update(g)
    if not loc["dept"] and postcode_dept and not city_hint:
        loc["dept"] = postcode_dept
    m = RE_DEPT_PAREN.search(text)  # « Fronton (31) » : indice très fiable
    if m and not city_hint and (not loc["dept"] or m.group(1) not in DEPTS):
        loc["dept"] = m.group(1)
    if not loc["dept"] and depts:
        in_zone = [d for d in depts if d in DEPTS] or depts
        loc["dept"] = in_zone[0]
    if loc["lat"] is None and loc["dept"] in DEPT_CENTER:
        loc["lat"], loc["lon"] = DEPT_CENTER[loc["dept"]]
        loc["approx"] = True
    return loc


def make_event(source: str, title: str, url: str, summary: str = "", organizer: str = "",
               city_hint: str = "", depts: list[str] | None = None, deadline: str | None = None,
               event_date: str | None = None, published: str | None = None,
               required_terms: list[str] | None = None, mode: str = "demandes") -> dict | None:
    title, summary = clean(title), clean(summary)
    if not title or not url:
        return None
    full = f"{title}. {summary}. {organizer}"
    nt = norm(full)
    events_mode = mode == "evenements"

    main = contains_any(nt, required_terms or (KW_EVENTS if events_mode else KW_MAIN))
    if not main:
        return None
    if contains_any(nt, KW_EXCL):
        return None
    if events_mode:
        if is_promo(nt) or RE_EVENT_BAD.search(nt):
            return None
    elif source != "BOAMP" and (not has_signal(nt) or is_promo(nt)):
        return None

    if not deadline and not event_date:
        deadline, event_date = find_dates(full)
    if deadline and deadline < TODAY.isoformat():
        return None
    if not deadline and event_date and event_date < TODAY.isoformat():
        return None

    loc = locate(full, city_hint, depts)
    if DEPTS and loc["dept"] and loc["dept"] not in DEPTS:
        return None
    if DEPTS and source in ("Recherche web", "Alerte Google") and not loc["dept"]:
        g = guess_place(full)
        if g:
            loc.update(g)
            loc["approx"] = False
    if DEPTS and source in ("Recherche web", "Alerte Google") and loc["dept"] not in DEPTS:
        zone_words = [norm(z) for z in CFG.get("zones_recherche", []) + TOWNS] + REGION_WORDS
        if not contains_any(nt, zone_words):
            return None  # aucun lieu de ta zone dans l'annonce : trop incertain

    centre = CFG.get("centre")
    if centre and loc.get("lat") is not None and not loc.get("approx"):
        dist = km_between(centre["lat"], centre["lon"], loc["lat"], loc["lon"])
        if dist > float(CFG.get("rayon_km", 9999)):
            return None
    bonus = contains_any(nt, KW_BONUS)
    kind = classify(nt, source)
    if source in ("Recherche web", "Alerte Google") and kind == "Événement" and not events_mode:
        return None  # côté « demandes », le web ne garde que les appels et les recherches de food truck
    if events_mode and not (event_date or deadline):
        return None  # une fête sans date ne t'aide pas à remplir ton agenda
    score = SOURCE_WEIGHT.get(source, 30) + min(30, 15 * len(main)) + min(20, 5 * len(bonus))
    score += 15 if kind == "Appel à candidatures" else 10 if kind == "Recherche un food truck" else 0
    score += 10 if deadline else 0
    score += 10 if loc["dept"] in DEPTS else 0

    uid = hashlib.sha1(f"{url.strip()}|{norm(title)[:60]}".encode()).hexdigest()[:12]
    return {
        "id": uid,
        "title": title[:220],
        "summary": summary[:600],
        "type": kind,
        "source": source,
        "url": url,
        "organizer": clean(organizer)[:160] or None,
        "city": loc["city"], "dept": loc["dept"],
        "lat": loc["lat"], "lon": loc["lon"], "approx": bool(loc.get("approx")),
        "deadline": deadline, "event_date": event_date,
        "published": published,
        "contact": extract_contacts(full),
        "mode": mode,
        "via": via_label(url),
        "keywords": sorted(set(main + bonus))[:8],
        "score": min(100, score),
    }


# --------------------------------------------------------------- sources ---

def src_boamp() -> list[dict]:
    conf = CFG.get("boamp", {})
    if not conf.get("actif", True):
        return []
    base = "https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp/records"
    since = (TODAY - timedelta(days=int(conf.get("jours", 120)))).isoformat()
    out: list[dict] = []
    for term in conf.get("termes", []):
        attempts = [
            {"where": f"\"{term}\" AND dateparution >= date'{since}'", "order_by": "dateparution desc", "limit": 100},
            {"where": f"\"{term}\"", "order_by": "dateparution desc", "limit": 100},
        ]
        records = None
        for params in attempts:
            r = session.get(base, params=params, timeout=TIMEOUT)
            if r.ok:
                records = r.json().get("results", [])
                break
        if records is None:
            raise RuntimeError(f"BOAMP a répondu {r.status_code}")
        for rec in records:
            nature = str(rec.get("nature_libelle") or rec.get("nature") or "")
            if "sultat" in nature or "ttribution" in nature:
                continue
            pub = str(rec.get("dateparution") or "")[:10] or None
            if pub and pub < since:
                continue
            depts = rec.get("code_departement") or []
            if isinstance(depts, str):
                depts = [d.strip() for d in depts.split(",")]
            depts = [str(d).zfill(2) for d in depts if d]
            if DEPTS and depts and not set(depts) & set(DEPTS):
                continue
            buyer = rec.get("nomacheteur") or ""
            m = re.search(r"(?:commune|mairie|ville)\s+(?:de\s+la\s+|de\s+l'|de\s+|d')?([\w' -]+)", buyer, re.I)
            city_hint = m.group(1).strip() if m else ""
            desc = rec.get("descripteur_libelle") or ""
            if isinstance(desc, list):
                desc = ", ".join(map(str, desc))
            idweb = rec.get("idweb") or rec.get("id")
            url = rec.get("url_avis") or f"https://www.boamp.fr/pages/avis/?q=idweb:{idweb}"
            dl = str(rec.get("datelimitereponse") or "")[:10] or None
            ev = make_event("BOAMP", rec.get("objet") or "", url, summary=f"{nature}. {desc}",
                            organizer=buyer, city_hint=city_hint, depts=depts, deadline=dl,
                            published=pub, required_terms=[term] + KW_MAIN)
            if ev:
                out.append(ev)
        time.sleep(0.5)
    return out


def parse_feed(content: bytes) -> list[dict]:
    """lit un flux RSS ou Atom sans dépendance externe"""
    root = ET.fromstring(content)
    items = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        rec = {"title": "", "link": "", "summary": "", "published": None}
        for child in el:
            ct = child.tag.split("}")[-1]
            if ct == "title":
                rec["title"] = "".join(child.itertext())
            elif ct == "link":
                rec["link"] = child.get("href") or (child.text or "").strip()
            elif ct in ("description", "summary", "content") and not rec["summary"]:
                rec["summary"] = "".join(child.itertext())
            elif ct in ("pubDate", "published", "updated") and not rec["published"]:
                rec["published"] = (child.text or "").strip()
        items.append(rec)
    return items


def iso_or_none(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value[:10] if re.match(r"\d{4}-\d{2}-\d{2}", value) else None


def real_link(link: str) -> str:
    q = parse_qs(urlparse(link).query)
    return (q.get("url") or q.get("q") or [link])[0]


def src_google_alerts() -> list[dict]:
    out: list[dict] = []
    feeds = CFG.get("google_alertes") or []
    failed = []
    for feed_url in feeds:
        try:
            r = session.get(feed_url, timeout=TIMEOUT)
            r.raise_for_status()
            items = parse_feed(r.content)
        except Exception as exc:
            failed.append(str(exc)[:120])
            continue
        for it in items:
            ev = make_event("Alerte Google", it["title"], real_link(it["link"]), summary=it["summary"],
                            published=iso_or_none(it["published"]))
            if ev:
                out.append(ev)
    if feeds and len(failed) == len(feeds):
        raise RuntimeError(failed[0])
    return out


class SkipSource(Exception):
    pass


def _serper(q: str, key: str) -> list[tuple]:
    """Serper : on essaie avec le filtre « dernier mois », puis sans si l'API refuse."""
    attempts = [
        {"q": q, "gl": "fr", "hl": "fr", "tbs": "qdr:m"},
        {"q": q, "gl": "fr", "hl": "fr"},
        {"q": q},
    ]
    last = None
    for payload in attempts:
        r = session.post("https://google.serper.dev/search", json=payload,
                         headers={"X-API-KEY": key.strip(), "Content-Type": "application/json"},
                         timeout=TIMEOUT)
        if r.ok:
            return [(it.get("title"), it.get("link"), it.get("snippet"), it.get("date"))
                    for it in r.json().get("organic", [])]
        last = f"Serper {r.status_code} : {r.text[:160]}"
        if r.status_code in (401, 403):
            break  # clé refusée : inutile d'insister
    raise RuntimeError(last or "Serper : pas de réponse")


def _brave(q: str, key: str) -> list[tuple]:
    r = session.get("https://api.search.brave.com/res/v1/web/search",
                    params={"q": q, "country": "fr", "search_lang": "fr", "count": 20, "freshness": "pm"},
                    headers={"X-Subscription-Token": key.strip(), "Accept": "application/json"}, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"Brave {r.status_code} : {r.text[:160]}")
    return [(it.get("title"), it.get("url"), it.get("description"), it.get("page_age"))
            for it in (r.json().get("web") or {}).get("results", [])]


WEB_RUNS: dict = {}   # dernière date de passage de chaque groupe (gardée dans events.json)


def _web_groups(conf: dict) -> list[dict]:
    if conf.get("groupes"):
        return conf["groupes"]
    return [{"nom": "Candidatures", "type": "demandes", "tous_les_jours": 1,
             "max": conf.get("max_requetes", 18), "modeles": conf.get("modeles", [])}]


def src_web_search() -> list[dict]:
    conf = CFG.get("recherche_web", {})
    brave, serper = os.getenv("BRAVE_API_KEY"), os.getenv("SERPER_API_KEY")
    if not conf.get("actif", True) or not (brave or serper):
        return []
    scheduled = os.getenv("GITHUB_EVENT_NAME") == "schedule"
    if conf.get("une_fois_par_jour", True) and scheduled and datetime.now(timezone.utc).hour >= 12:
        raise SkipSource("recherche web faite le matin seulement (économise le quota)")
    years = f"{TODAY.year} OR {TODAY.year + 1}"
    out: list[dict] = []
    errors: list[str] = []
    done = 0
    for group in _web_groups(conf):
        name = group.get("nom", "web")
        every = int(group.get("tous_les_jours", 1))
        last = WEB_RUNS.get(name)
        if last and (TODAY - date.fromisoformat(last)).days < every:
            continue  # ce groupe a déjà tourné récemment
        mode = group.get("type", "demandes")
        queries = [tpl.replace("{annee}", years).format(zone=z)
                   for tpl in group.get("modeles", []) for z in CFG.get("zones_recherche", [])]
        group_done = 0
        for q in queries[: int(group.get("max", 30))]:
            try:
                results = _brave(q, brave) if brave else _serper(q, serper)
                done += 1
                group_done += 1
            except Exception as exc:
                errors.append(str(exc))
                if len(errors) >= 3 and done == 0:
                    raise RuntimeError(errors[0])
                continue
            for title, url, snippet, age in results:
                ev = make_event("Recherche web", title or "", url or "", summary=snippet or "",
                                published=iso_or_none(age), mode=mode)
                if ev:
                    out.append(ev)
            time.sleep(1.1)
        if group_done:
            WEB_RUNS[name] = TODAY.isoformat()
    if done == 0 and errors:
        raise RuntimeError(errors[0])
    return out


RE_LEAD_DATE = re.compile(r"^\s*(\d{1,2}\s+\w+\.?\s+\d{4}\s*(au|-|–)?\s*)+", re.I)


def scan_agenda(page: dict) -> list[dict]:
    """lit un agenda d'office de tourisme (plusieurs pages) et garde les fêtes datées"""
    base, name = page["url"], page.get("nom") or page["url"]
    pages = max(1, int(page.get("pages", 1)))
    out: list[dict] = []
    seen: set[str] = set()
    for n in range(1, pages + 1):
        url = base if n == 1 else urljoin(base if base.endswith("/") else base + "/", f"page/{n}/")
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code == 404:
            break
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form"]):
            tag.decompose()
        for a in soup.find_all("a", href=True):
            text = clean(a.get_text(" "))
            link = urljoin(url, a["href"])
            if len(text) < 25 or link in seen or not RE_TXT.search(norm(text)):
                continue
            seen.add(link)
            heading = a.find(["h2", "h3", "h4", "strong"])
            title = clean(heading.get_text(" ")) if heading else RE_LEAD_DATE.sub("", text)[:120]
            ev = make_event("Page surveillée", title or text[:120], link, summary=text, organizer=name,
                            mode="evenements", required_terms=KW_EVENTS + KW.get("evenements_agenda", []))
            if ev:
                out.append(ev)
        time.sleep(1)
    return out


def src_pages() -> tuple[list[dict], list[str]]:
    out: list[dict] = []
    errors: list[str] = []
    for page in CFG.get("pages_surveillees") or []:
        url, city = page.get("url"), page.get("ville", "")
        name = page.get("nom") or url
        if page.get("type") == "evenements":
            try:
                out += scan_agenda(page)
            except Exception as exc:
                errors.append(f"{name} : {exc.__class__.__name__}")
            continue
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "xml" in ctype or "rss" in ctype or r.content.lstrip()[:5] == b"<?xml":
                for it in parse_feed(r.content):
                    ev = make_event("Page surveillée", it["title"], it["link"] or url, summary=it["summary"],
                                    organizer=name, city_hint=city, published=iso_or_none(it["published"]))
                    if ev:
                        out.append(ev)
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form"]):
                tag.decompose()
            seen: set[str] = set()
            kept = 0
            for a in soup.find_all("a", href=True):
                text = clean(a.get_text(" "))
                context = clean((a.find_parent(["article", "li", "p", "div"]) or a).get_text(" "))[:500]
                if len(text) < 8 or not contains_any(norm(context), KW_MAIN):
                    continue
                link = urljoin(url, a["href"])
                if link in seen:
                    continue
                seen.add(link)
                ev = make_event("Page surveillée", text, link, summary=context, organizer=name, city_hint=city)
                if ev:
                    out.append(ev)
                    kept += 1
                if kept >= 15:
                    break
            if kept == 0:
                for block in soup.find_all(["h2", "h3", "h4", "p", "li"]):
                    text = clean(block.get_text(" "))
                    if 20 <= len(text) <= 600 and contains_any(norm(text), KW_MAIN):
                        ev = make_event("Page surveillée", text[:140], url, summary=text,
                                        organizer=name, city_hint=city)
                        if ev:
                            out.append(ev)
                            kept += 1
                    if kept >= 10:
                        break
        except Exception as exc:  # une page en panne ne bloque pas les autres
            errors.append(f"{name} : {exc.__class__.__name__}")
        time.sleep(1)
    return out, errors


# ------------------------------------------------------------ assemblage ---

def still_valid(ev: dict, keep_days: int) -> bool:
    t = TODAY.isoformat()
    if ev.get("deadline"):
        return ev["deadline"] >= t
    if ev.get("event_date"):
        return ev["event_date"] >= t
    found = ev.get("found_at", t)[:10]
    return found >= (TODAY - timedelta(days=keep_days)).isoformat()


def notify_telegram(new_events: list[dict]):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat and new_events):
        return
    app_url = os.getenv("APP_URL", "")
    lines = [f"🌭 {len(new_events)} nouvelle(s) opportunité(s) pour le food truck"]
    for ev in new_events[:10]:
        when = f" — clôture le {ev['deadline']}" if ev.get("deadline") else ""
        where = f" ({ev['city']})" if ev.get("city") else ""
        lines.append(f"\n• {ev['title'][:100]}{where}{when}\n{ev['url']}")
    if app_url:
        lines.append(f"\n📱 {app_url}")
    try:
        session.post(f"https://api.telegram.org/bot{token}/sendMessage",
                     data={"chat_id": chat, "text": "\n".join(lines)[:4000], "disable_web_page_preview": "true"},
                     timeout=TIMEOUT)
    except Exception as exc:
        log("Telegram :", exc)


def main() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    try:
        previous = json.loads(EVENTS_FILE.read_text("utf-8"))
    except Exception:
        previous = {}
    old = {e["id"]: e for e in previous.get("events", [])}
    WEB_RUNS.update(previous.get("web_runs", {}))

    status: dict[str, dict] = {}
    collected: list[dict] = []
    runs = [("BOAMP", src_boamp), ("Alertes Google", src_google_alerts), ("Recherche web", src_web_search)]
    for name, fn in runs:
        try:
            found = fn()
            collected += found
            status[name] = {"ok": True, "found": len(found)}
        except SkipSource as exc:
            status[name] = {"ok": True, "found": 0, "note": str(exc)}
        except Exception as exc:
            status[name] = {"ok": False, "found": 0, "error": f"{exc.__class__.__name__}: {exc}"[:200]}
        log(f"{name}: {status[name]}")
    try:
        found, errors = src_pages()
        collected += found
        status["Pages surveillées"] = {"ok": not errors or bool(found), "found": len(found), "errors": errors}
    except Exception as exc:
        status["Pages surveillées"] = {"ok": False, "found": 0, "error": str(exc)[:200]}
    log(f"Pages surveillées: {status['Pages surveillées']}")

    if not CFG.get("google_alertes"):
        status["Alertes Google"]["note"] = "Aucun flux configuré"
    if not (os.getenv("BRAVE_API_KEY") or os.getenv("SERPER_API_KEY")):
        status["Recherche web"]["note"] = "Aucune clé API configurée"

    now_iso = NOW.isoformat(timespec="seconds")
    merged: dict[str, dict] = {}
    seen_urls: set[str] = set()
    new_events: list[dict] = []
    for ev in sorted(collected, key=lambda e: -e["score"]):
        if ev["id"] in merged or ev["url"] in seen_urls:
            continue
        seen_urls.add(ev["url"])
        if ev["id"] in old:
            prev = old[ev["id"]]
            ev["found_at"] = prev.get("found_at", now_iso)
            ev["contact"] = merge_contacts(prev.get("contact"), ev.get("contact"))
            ev["contact_checked"] = prev.get("contact_checked", False)
        else:
            ev["found_at"] = now_iso
            new_events.append(ev)
        merged[ev["id"]] = ev

    keep_days = int(CFG.get("conserver_jours", 90))
    for uid, ev in old.items():
        if uid in merged or ev.get("url") in seen_urls or not still_valid(ev, keep_days):
            continue
        again = make_event(ev.get("source", ""), ev.get("title", ""), ev.get("url", ""),
                           summary=ev.get("summary", ""), organizer=ev.get("organizer") or "",
                           deadline=ev.get("deadline"), event_date=ev.get("event_date"),
                           required_terms=(KW_MAIN + CFG.get("boamp", {}).get("termes", [])) if ev.get("source") == "BOAMP" else None,
                           depts=[ev["dept"]] if ev.get("dept") else None, city_hint=ev.get("city") or "",
                           mode=ev.get("mode", "demandes"))
        if again is None:
            continue  # ne passe plus les filtres : on l'enlève
        for k in ("found_at", "contact", "contact_checked", "lat", "lon", "city", "dept", "approx"):
            if k in ev:
                again[k] = ev[k]
        again["id"] = uid
        merged[uid] = again

    events = [e for e in merged.values() if still_valid(e, keep_days)]
    events.sort(key=lambda e: (-(e.get("score") or 0), e.get("deadline") or "9999"))
    enrich_contacts(events, budget=int(CFG.get("pages_contacts_max", 60)))

    payload = {
        "updated": now_iso,
        "count": len(events),
        "new_count": len(new_events),
        "sources": status,
        "web_runs": WEB_RUNS,
        "events": events,
    }
    EVENTS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    GEOCACHE_FILE.write_text(json.dumps(GEOCACHE, ensure_ascii=False, indent=1), "utf-8")
    log(f"Terminé : {len(events)} opportunités, dont {len(new_events)} nouvelles.")
    notify_telegram(new_events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
