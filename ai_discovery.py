"""
ai_discovery.py - makes Perk Miner public business profiles findable and answerable by
AI assistants, search engines, Siri (Applebot) and Bixby (web search).

Everything here is READ-ONLY and exposes only fields that are already public on
/public_profiles/<slug>. No member data, e-mail addresses, balances or live GPS positions.

Adds
  GET  /api/public/search              ?q= &category= &city= &state= &lat= &lng= &radius_miles= &limit= &offset=
  GET  /api/public/businesses/<slug>   one full public profile (JSON)
  GET  /api/public/categories          category list with counts
  GET  /public_profiles/<slug>.json    same profile as JSON
  GET  /public_profiles/<slug>.md      same profile as Markdown
  GET  /openapi.json                   OpenAPI 3.1 description (ChatGPT Actions, other agents)
  POST /mcp                            Model Context Protocol server (Claude, ChatGPT, Cursor, ...)
  GET  /<INDEXNOW_KEY>.txt             only when the INDEXNOW_KEY environment variable is set
  flask indexnow-submit-all            one-off CLI: send every public profile to IndexNow (Bing etc.)

Wire-up (app.py):  ai = init_ai_discovery(app, csrf=csrf, Business=Business, ...)
"""
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import service_area as _sa
from urllib.request import Request, urlopen

from flask import Response, request

try:  # always present in production; only missing in stripped-down test environments
    from sqlalchemy.orm import load_only
except ImportError:  # pragma: no cover
    load_only = None

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------
MCP_LATEST = "2025-06-18"
MCP_SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
SNAPSHOT_TTL = 60            # seconds the in-memory directory snapshot is reused
RATE_LIMIT_PER_MIN = 60      # per client IP, per worker (soft limit against runaway bots)
MAX_LIMIT = 25

NOTICE = ("Listings are advertisers on Perk Miner. Perk Miner LLC verifies business registration before "
          "approving a listing but does not vouch for quality or results; check reviews, licensing, "
          "bonding and insurance directly with the business.")
CASHBACK = ("Perk Miner members earn 2% cash back on up to $2,500 of each purchase from this business "
            "(paid by Perk Miner LLC, not by the business).")

# Only categories where the schema.org type is unambiguous. Everything else stays LocalBusiness.
CATEGORY_SCHEMA_TYPE = {
    "Restaurant-Food-and-Drink": "FoodEstablishment",
    "Auto-Service-Repair-Parts": "AutomotiveBusiness",
    "Casino": "Casino",
    "Education-and-Training": "EducationalOrganization",
    "Financial-Services": "FinancialService",
    "Handyman-Contractor": "HomeAndConstructionBusiness",
    "Health-and-Beauty": "HealthAndBeautyBusiness",
    "Legal-Services": "LegalService",
    "Liquor-Store": "LiquorStore",
    "Professional-Services": "ProfessionalService",
    "Real-Estate": "RealEstateAgent",
    "Shopping-and-Retail": "Store",
    "Smoke-Shop": "Store",
    "Dispensary": "Store",
    "Sports-and-Recreation": "SportsActivityLocation",
}

# schema.org servesCuisine is only emitted when the owner's own text names the cuisine.
CUISINES = [("mexican", "Mexican"), ("italian", "Italian"), ("chinese", "Chinese"), ("japanese", "Japanese"),
            ("sushi", "Sushi"), ("thai", "Thai"), ("indian", "Indian"), ("korean", "Korean"),
            ("vietnamese", "Vietnamese"), ("french", "French"), ("greek", "Greek"),
            ("mediterranean", "Mediterranean"), ("american", "American"), ("bbq", "Barbecue"),
            ("barbecue", "Barbecue"), ("pizza", "Pizza"), ("seafood", "Seafood"),
            ("steakhouse", "Steakhouse"), ("burger", "Burgers"), ("vegan", "Vegan"),
            ("vegetarian", "Vegetarian"), ("caribbean", "Caribbean"), ("peruvian", "Peruvian"),
            ("ethiopian", "Ethiopian"), ("middle eastern", "Middle Eastern")]

STOPWORDS = {
    "the", "a", "an", "in", "at", "on", "near", "nearby", "nearest", "closest", "close", "me", "my", "to",
    "for", "of", "and", "or", "best", "top", "good", "great", "find", "show", "search", "looking", "want",
    "need", "place", "places", "local", "around", "from", "with", "some", "any", "please", "what", "where",
    "is", "are", "i", "you", "can", "get", "who", "that", "this",
}
PROXIMITY_WORDS = {"nearest", "closest", "near", "nearby", "close", "around"}

# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------
def _clean(value, limit=None):
    text = " ".join(str(value or "").split())
    if limit and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:- ") + "…"
    return text


def _float(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def norm_key(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def category_label(cat):
    return _clean((cat or "").replace("-", " "))


def haversine_miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 3959 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


_STATE_RE = re.compile(r"^([A-Za-z]{2})(?:\s+(\d{5}(?:-\d{4})?))?$")
_COUNTRY_WORDS = {"usa", "us", "u.s.a.", "u.s.", "united states", "united states of america"}


def split_address(address):
    """'254 Chapman Road, Ste 208, Newark, DE 19711, USA' -> street/city/state/postal/country.
    Works with or without a trailing country or ZIP code."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    out = {"street": "", "city": "", "state": "", "postal": "", "country": ""}
    if parts and parts[-1].lower() in _COUNTRY_WORDS:
        out["country"] = parts.pop()
    if parts:
        m = _STATE_RE.match(parts[-1])
        if m:
            out["state"] = m.group(1).upper()
            out["postal"] = m.group(2) or ""
            parts.pop()
    if parts:
        out["city"] = parts.pop()
    out["street"] = ", ".join(parts)
    return out


# ---- opening hours ------------------------------------------------------------------
_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_DAY_KEY = {"mon": "Mo", "tue": "Tu", "wed": "We", "thu": "Th", "fri": "Fr", "sat": "Sa", "sun": "Su"}
_DAY_TOKEN = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*$", re.I)
_TIME_PART = r"\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?"
_HOURS_SEG = re.compile(
    r"^(?P<days>[A-Za-z]+(?:\s*[-–—]\s*[A-Za-z]+)?(?:\s*,\s*[A-Za-z]+(?:\s*[-–—]\s*[A-Za-z]+)?)*)"
    r"[\s:,]+(?P<open>" + _TIME_PART + r")\s*(?:-|–|—|to)\s*(?P<close>" + _TIME_PART + r")\s*$",
    re.I,
)


def _parse_clock(text):
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?m\.?)?$", text.strip(), re.I)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = (hour % 12) + (12 if ampm == "p" else 0)
    elif hour > 24:
        return None
    return hour, minute, ampm


def _expand_days(text):
    pieces = []
    for chunk in re.split(r"\s*,\s*", text.strip()):
        if chunk.lower() in ("daily", "everyday", "every day", "7days"):
            pieces.append("Mo-Su")
            continue
        ends = [e.strip() for e in re.split(r"\s*[-–—]\s*", chunk)]
        if not ends or len(ends) > 2 or not all(_DAY_TOKEN.match(e) for e in ends):
            return None
        keys = [_DAY_KEY[e[:3].lower()] for e in ends]
        pieces.append(keys[0] if len(keys) == 1 or keys[0] == keys[1] else f"{keys[0]}-{keys[1]}")
    return ",".join(pieces) or None


def parse_hours(text):
    """Best effort: 'Mon-Fri 9am-5pm; Sat 10-2pm' -> ['Mo-Fr 09:00-17:00', 'Sa 10:00-14:00'].
    All-or-nothing: returns [] when any part is missing or ambiguous (the free text is then kept as text only)."""
    out = []
    for seg in re.split(r"[;\n|]+", text or ""):
        seg = seg.strip()
        if not seg:
            continue
        m = _HOURS_SEG.match(seg)
        if not m:
            return []
        days = _expand_days(m.group("days"))
        o, c = _parse_clock(m.group("open")), _parse_clock(m.group("close"))
        if not days or not o or not c:
            return []
        (oh, om, oampm), (ch, cm, campm) = o, c
        if oampm and not campm:                       # '9am-5' is ambiguous
            return []
        if campm and not oampm:                       # '9-5pm': opening borrows the closing am/pm...
            if campm == "p" and oh < 12:
                oh += 12
            if (oh, om) >= (ch, cm) and oh >= 12:     # ...unless that opens after closing ('10-2pm')
                oh -= 12
        elif not oampm and not campm:                 # bare numbers must be 24-hour times
            if (ch, cm) <= (oh, om) and oh < 13:      # '9-5' could mean anything
                return []
        if oh > 24 or ch > 24:
            return []
        out.append(f"{days} {oh % 24:02d}:{om:02d}-{ch % 24:02d}:{cm:02d}")
    return out


def tel_href(phone, country="US"):
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return "+" + digits
    if (country or "US").upper() == "US":
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
    return digits


def tokenize(text):
    tokens = []
    for t in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if t in STOPWORDS:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        if len(t) >= 2 and t not in tokens:
            tokens.append(t)
    return tokens


def build_summary(rec):
    """One plain-language paragraph an assistant can read aloud or quote."""
    name, label = rec["name"], rec["category_label"]
    where = rec["service_area"]
    if rec.get("service_area_scope") and where:
        sentence = f"{name} is a Perk Miner advertiser in the {label} category serving {where}"
        sentence += (f", headquartered in {rec['hq_area']}." if rec.get("hq_area") else ".")
    elif rec["has_public_address"] and where:
        sentence = f"{name} is a Perk Miner advertiser in the {label} category, located in {where}."
    elif where:
        sentence = (f"{name} is a Perk Miner advertiser in the {label} category serving {where}"
                    + (" (mobile / on-location)." if rec["mobile"] else "."))
    else:
        sentence = f"{name} is a Perk Miner advertiser in the {label} category."
    bits = [sentence]
    if rec["services"]:
        bits.append("Services: " + ", ".join(rec["services"][:6]) + ".")
    if rec["hours_text"]:
        bits.append(f"Hours: {rec['hours_text']}.")
    if rec["member_perk"]:
        bits.append(f"Perk Miner member perk: {_clean(rec['member_perk'], 160).rstrip('.')}.")
    bits.append("Perk Miner members also earn 2% cash back on up to $2,500 per purchase.")
    return " ".join(bits)


def _loc_phrase(rec):
    """' in Hemet, CA' for local businesses; ' – Nationwide (USA)' when the service area is wider than local."""
    if not rec["service_area"]:
        return ""
    return f" \u2013 {rec['service_area']}" if rec.get("service_area_scope") else f" in {rec['service_area']}"


def _meta_trim(value, limit):
    """Shorten for a meta description: cut at a word boundary, no trailing ellipsis, end like a sentence."""
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0]
    return text.rstrip(".,;:-\u2013\u2014 ")


def build_page_title(rec):
    primary = rec["services"][0] if rec["services"] else rec["category_label"]
    core = rec["name"] + (f" | {primary}" if primary else "")
    loc = _loc_phrase(rec)
    if len(core) + len(loc) <= 60:
        core += loc
    if len(core) > 65:
        core = core[:65].rsplit(" ", 1)[0].rstrip(" -|")
    return core + " | Perk Miner"


def build_meta_description(rec):
    what = rec["services"][0] if rec["services"] else rec["category_label"]
    loc = _loc_phrase(rec)
    text = f"{rec['name']}: {what}{loc}."
    extra = f" Member perk: {_meta_trim(rec['member_perk'], 70)}." if rec["member_perk"] else (
        f" {_meta_trim(rec['description'], 90)}." if rec["description"] else "")
    text += extra
    tail = " Earn 2% cash back with Perk Miner."
    if len(text) + len(tail) <= 155:
        text += tail
    text = text.replace("..", ".")
    if len(text) > 155:
        text = _meta_trim(text, 154) + "."
    return text


# --------------------------------------------------------------------------------------
# rate limiter (soft, per worker)
# --------------------------------------------------------------------------------------
class _RateLimiter:
    def __init__(self, per_minute):
        self.per_minute, self.hits, self.lock = per_minute, {}, threading.Lock()

    def allow(self, key):
        now = time.time()
        with self.lock:
            if len(self.hits) > 5000:
                self.hits.clear()
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False, int(60 - (now - q[0])) + 1
            q.append(now)
            return True, 0


# --------------------------------------------------------------------------------------
# main object
# --------------------------------------------------------------------------------------
class AIDiscovery:
    # attribute names we need from Business; checked against the model at init time
    BUSINESS_FIELDS = (
        ["id", "business_name", "store_slug", "category", "perks", "profile_photo", "phone_number", "address",
         "latitude", "longitude", "hours_of_operation", "website_url", "about_us", "search_keywords", "rank",
         "location_varies", "country", "updated_at", "google_reviews_url", "facebook_url", "instagram_url",
         "x_url", "linkedin_url", "tiktok_url", "youtube_url", "photo1_url", "photo2_url", "photo3_url",
         "photo4_url", "photo5_url", "photo6_url", "listing_type", "licensed", "license_number",
         "license_state", "lic_verified", "bonded", "bond_amount", "bond_company", "bond_number",
         "bond_verified", "insured", "ins_amount", "ins_company", "ins_number", "ins_verified"]
        + [f"service_{i}" for i in range(1, 11)]
    )

    def __init__(self, *, Business, BusinessFAQ, categories, public_businesses_query, public_profile_url, base_url):
        self.Business, self.BusinessFAQ = Business, BusinessFAQ
        self.categories = list(categories)
        self.public_businesses_query = public_businesses_query
        self.public_profile_url = public_profile_url
        self.base_url = base_url.rstrip("/")
        self._snap, self._snap_at, self._lock = [], 0.0, threading.Lock()
        self.limiter = _RateLimiter(RATE_LIMIT_PER_MIN)

    # ---- records ------------------------------------------------------------------
    def record(self, biz):
        """Plain-dict view of one Business row. Keys starting with '_' are internal."""
        name = _clean(biz.business_name) or "Perk Miner advertiser"
        slug = biz.store_slug or ""
        url = self.public_profile_url(slug) if slug else self.base_url + "/public_profiles"
        addr = _clean(biz.address)
        a = split_address(addr)
        mobile = bool(getattr(biz, "location_varies", False))
        public_address = bool(addr) and not mobile
        lat, lng = _float(biz.latitude), _float(biz.longitude)
        services = [s for s in (_clean(getattr(biz, f"service_{i}", None)) for i in range(1, 11)) if s]
        category = _clean(biz.category)
        area = ", ".join(x for x in (a["city"], a["state"]) if x)
        sa_scope = (getattr(biz, "service_area_scope", None) or "local").lower()
        sa_regions = getattr(biz, "service_area_regions", None) or ""
        sa_expanded = _sa.is_expanded(sa_scope)
        sa_label = _sa.label(sa_scope, sa_regions, a["state"]) if sa_expanded else ""
        country = (getattr(biz, "country", None) or "US").upper()
        phone = _clean(getattr(biz, "phone_number", None))
        photos = [p for p in (getattr(biz, f"photo{i}_url", None) for i in range(1, 7)) if p]
        social = {k: v for k, v in (
            (k, (getattr(biz, f"{k}_url", None) or "").strip()) for k in
            ("facebook", "instagram", "x", "linkedin", "tiktok", "youtube")) if v.startswith("https://")}
        hours_text = _clean(getattr(biz, "hours_of_operation", None))
        updated = getattr(biz, "updated_at", None)
        if isinstance(updated, datetime):
            updated = (updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)).date().isoformat()
        about = _clean(biz.about_us, 1500)
        perks = _clean(biz.perks, 400)
        keywords = _clean(biz.search_keywords, 500)
        rec = {
            "name": name, "slug": slug, "url": url,
            "json_url": f"{url}.json" if slug else None, "markdown_url": f"{url}.md" if slug else None,
            "category": category, "category_label": category_label(category) or "local",
            "description": about, "services": services, "member_perk": perks, "member_cashback": CASHBACK,
            "city": a["city"], "state": a["state"], "postal_code": a["postal"], "country": country,
            "service_area": sa_label or area, "service_area_scope": sa_scope if sa_expanded else "",
            "hq_area": area if sa_expanded else "", "_sa_nodes": _sa.schema_areas(sa_scope, sa_regions, a["state"]) if sa_expanded else None,
            "mobile": mobile, "has_public_address": public_address,
            "street": a["street"] if public_address else "", "address": addr if public_address else "",
            "phone": phone, "tel": tel_href(phone, country), "website": _clean(getattr(biz, "website_url", None)),
            "hours_text": hours_text, "hours": parse_hours(hours_text),
            "latitude": lat if public_address else None, "longitude": lng if public_address else None,
            "google_reviews_url": _clean(getattr(biz, "google_reviews_url", None)),
            "social": social, "keywords": keywords, "updated_at": updated,
            "trust": {"licensed_verified": bool(getattr(biz, "badge_licensed", False)),
                      "bonded_verified": bool(getattr(biz, "badge_bonded", False)),
                      "insured_verified": bool(getattr(biz, "badge_insured", False))},
            # ---- internal (never serialised) ----
            "_id": biz.id, "_lat": lat, "_lng": lng, "_rank": _float(getattr(biz, "rank", 0)) or 0.0,
            "_photo": (getattr(biz, "profile_photo", None) or "").strip(), "_photos": photos,
            "_name_l": name.lower(), "_kw_l": keywords.lower(), "_svc_l": " ".join(services).lower(),
            "_cat_l": category_label(category).lower(), "_about_l": about.lower(), "_perk_l": perks.lower(),
            "_city_key": norm_key(a["city"]), "_state": a["state"].upper(),
        }
        rec["summary"] = build_summary(rec)
        rec["apple_maps_url"] = self._apple_maps(rec)
        rec["page_title"] = build_page_title(rec)
        rec["meta_description"] = build_meta_description(rec)
        return rec

    @staticmethod
    def _apple_maps(rec):
        if rec["latitude"] is not None and rec["longitude"] is not None:
            return f"https://maps.apple.com/?q={quote(rec['name'])}&ll={rec['latitude']},{rec['longitude']}"
        if rec["address"]:
            return f"https://maps.apple.com/?q={quote(rec['name'])}&address={quote(rec['address'])}"
        return ""

    @staticmethod
    def public(rec, detail=False, faqs=None, distance=None, matched=None):
        """Serialisable public subset of a record (no internal '_' fields)."""
        keys = ["name", "slug", "url", "services", "member_perk", "city", "state", "service_area", "service_area_scope", "hq_area", "mobile",
                "address", "phone", "website", "hours_text"]
        out = {k: rec[k] for k in keys if rec.get(k) not in (None, "", [], False)}
        out["category"] = rec["category_label"]
        if rec["latitude"] is not None:
            out["latitude"], out["longitude"] = rec["latitude"], rec["longitude"]
        if distance is not None:
            out["distance_miles"] = round(distance, 1)
        if matched is not None:
            out["matched_terms"] = sorted(matched)
        if not detail:
            if rec["description"]:
                out["description"] = _clean(rec["description"], 220)
            return out
        extra = ["summary", "description", "postal_code", "google_reviews_url", "social", "keywords", "updated_at",
                 "json_url", "markdown_url", "member_cashback", "apple_maps_url"]
        out.update({k: rec[k] for k in extra if rec.get(k) not in (None, "", [], {})})
        if rec["hours"]:
            out["hours_structured"] = rec["hours"]
        out["trust"] = rec["trust"]
        out["faqs"] = [{"question": _clean(_g(f, "question")), "answer": str(_g(f, "answer") or "").strip()}
                       for f in (faqs or [])]
        out["connect"] = ("Members connect privately with this business through the 'View Listing' button on its "
                          f"Perk Miner profile (free member account required): {rec['url']}")
        out["notice"] = NOTICE
        return out

    # ---- data access --------------------------------------------------------------
    def _fetch_rows(self):
        q = self.public_businesses_query().filter(self.Business.store_slug.isnot(None), self.Business.store_slug != "")
        if load_only is not None:
            q = q.options(load_only(*[getattr(self.Business, f) for f in self.BUSINESS_FIELDS]))
        return q.all()

    def snapshot(self):
        now = time.time()
        with self._lock:
            if self._snap and now - self._snap_at < SNAPSHOT_TTL:
                return self._snap
        recs = [self.record(b) for b in self._fetch_rows()]
        with self._lock:
            self._snap, self._snap_at = recs, time.time()
            return self._snap

    def faqs_for(self, business_id):
        rows = (self.BusinessFAQ.query.filter_by(business_id=business_id)
                .order_by(self.BusinessFAQ.position).limit(10).all())
        return [r for r in rows if (r.question or "").strip() and (r.answer or "").strip()]

    # ---- queries ------------------------------------------------------------------
    def resolve_categories(self, text):
        """-> (matching category slugs, leftover keyword tokens)."""
        key = norm_key(text)
        if not key:
            return [], []
        exact = [c for c in self.categories if norm_key(c) == key]
        if exact:
            return exact, []
        partial = [c for c in self.categories if key in norm_key(c)] if len(key) >= 3 else []
        if partial:
            return partial, []
        return [], tokenize(text)

    def search(self, q="", category="", city="", state="", lat=None, lng=None,
               radius_miles=None, limit=10, offset=0):
        limit = max(1, min(int(limit or 10), MAX_LIMIT))
        offset = max(0, int(offset or 0))
        lat, lng = _float(lat), _float(lng)
        if (lat is not None and not -90 <= lat <= 90) or (lng is not None and not -180 <= lng <= 180):
            raise ValueError("latitude must be between -90 and 90 and longitude between -180 and 180")
        if (lat is None) != (lng is None):
            raise ValueError("latitude and longitude must be provided together")
        radius = _float(radius_miles)
        city_text, state_text = _clean(city), _clean(state).upper()
        if "," in city_text and not state_text:          # 'Las Vegas, NV'
            city_text, _, st = city_text.rpartition(",")
            city_text, state_text = city_text.strip(), st.strip().split(" ")[0].upper()
        cats, extra_tokens = self.resolve_categories(category)
        tokens = tokenize(q) + [t for t in extra_tokens if t not in tokenize(q)]
        wants_nearest = bool(PROXIMITY_WORDS & set(re.findall(r"[a-z]+", (q or "").lower())))
        warnings = []

        scored = []
        for r in self.snapshot():
            if cats and r["category"] not in cats:
                continue
            if city_text and r["_city_key"] != norm_key(city_text):
                continue
            if state_text and r["_state"] != state_text:
                continue
            matched, score = set(), 0
            for t in tokens:
                w = (5 if t in r["_name_l"] else 0) + (3 if t in r["_kw_l"] else 0) + (3 if t in r["_svc_l"] else 0) \
                    + (2 if t in r["_cat_l"] else 0) + (1 if t in r["_perk_l"] else 0) + (1 if t in r["_about_l"] else 0)
                if w:
                    matched.add(t)
                    score += w
            dist = None
            if lat is not None and r["_lat"] is not None and r["_lng"] is not None:
                dist = haversine_miles(lat, lng, r["_lat"], r["_lng"])
            scored.append((r, matched, score, dist))

        if tokens:
            full = [x for x in scored if len(x[1]) == len(tokens)]
            if full:
                pool, mode = full, "all_terms"
            else:
                pool = [x for x in scored if x[1]]
                mode = "partial_terms" if pool else "no_match"
                if pool:
                    warnings.append("No listing matched every search term; showing partial matches. "
                                    "Check matched_terms before presenting a result as a match.")
        else:
            pool, mode = scored, "filters_only"

        if radius is not None and lat is not None:
            pool = [x for x in pool if x[3] is not None and x[3] <= radius]

        if lat is not None:
            pool.sort(key=lambda x: (x[3] is None, x[3] if x[3] is not None else 0, -x[2]))
            sorted_by = "distance"
        else:
            pool.sort(key=lambda x: (-x[2], -x[0]["_rank"], x[0]["name"].lower()))
            sorted_by = "relevance"
            if wants_nearest and not city_text and not state_text:
                warnings.append("Distance sorting needs latitude and longitude (or at least city and state). "
                                "Ask the user where they are; results are currently sorted by relevance.")

        page = pool[offset: offset + limit]
        return {
            "query": {k: v for k, v in {"q": q or None, "category": category or None, "city": city_text or None,
                                        "state": state_text or None, "latitude": lat, "longitude": lng,
                                        "radius_miles": radius}.items() if v is not None},
            "match_mode": mode, "sorted_by": sorted_by,
            "total_matches": len(pool), "returned": len(page), "offset": offset,
            "results": [self.public(r, distance=d, matched=(m if tokens else None)) for r, m, s, d in page],
            **({"warnings": warnings} if warnings else {}),
            "distance_note": "Distances are straight-line miles from the given point." if lat is not None else None,
            "notice": NOTICE,
        }

    def find(self, identifier):
        """slug, profile URL or business name -> ('one', rec) | ('many', [recs]) | ('none', None)."""
        text = _clean(identifier)
        if not text:
            return "none", None
        if text.startswith(("http://", "https://")):
            path = urlparse(text).path.rstrip("/")
            text = re.sub(r"\.(json|md)$", "", path.rsplit("/", 1)[-1])
        snap = self.snapshot()
        low = text.lower()
        for r in snap:
            if r["slug"].lower() == low:
                return "one", r
        key = norm_key(text)
        exact = [r for r in snap if norm_key(r["name"]) == key]
        if len(exact) == 1:
            return "one", exact[0]
        hits = exact or [r for r in snap if key and key in norm_key(r["name"])]
        if len(hits) == 1:
            return "one", hits[0]
        return ("many", hits[:10]) if hits else ("none", None)

    def category_counts(self):
        counts = {}
        for r in self.snapshot():
            counts[r["category"]] = counts.get(r["category"], 0) + 1
        return [{"category": c, "label": category_label(c), "listings": counts.get(c, 0)} for c in self.categories]

    # ---- renderers ----------------------------------------------------------------
    def markdown(self, rec, faqs=None):
        lines = [f"# {rec['name']}", "", rec["summary"], ""]
        facts = [("Category", rec["category_label"]),
                 ("Address (HQ)" if rec.get("service_area_scope") and rec["has_public_address"] else
                  "Address" if rec["has_public_address"] else "Area served",
                  rec["address"] or (rec["service_area"] + (" (mobile / on-location)" if rec["mobile"] else ""))),
                 ("Service area", rec["service_area"] if rec.get("service_area_scope") else ""),
                 ("Phone", rec["phone"]), ("Website", rec["website"]), ("Hours", rec["hours_text"]),
                 ("Google reviews", rec["google_reviews_url"]), ("Member perk", rec["member_perk"]),
                 ("Profile", rec["url"])]
        lines += [f"- **{k}:** {v}" for k, v in facts if v]
        if rec["description"]:
            lines += ["", "## About", "", rec["description"]]
        if rec["services"]:
            lines += ["", "## Services", ""] + [f"- {s}" for s in rec["services"]]
        badges = [k.split("_")[0].title() for k, v in rec["trust"].items() if v]
        if badges:
            lines += ["", f"Verified by Perk Miner LLC: {', '.join(badges)}."]
        if faqs:
            lines += ["", "## FAQ"]
            for f in faqs:
                lines += ["", f"### {_clean(_g(f, 'question'))}", "", str(_g(f, 'answer')).strip()]
        lines += ["", "---", rec["member_cashback"], NOTICE]
        if rec["updated_at"]:
            lines.append(f"Last updated: {rec['updated_at']}.")
        return "\n".join(lines) + "\n"

    def search_markdown(self, data):
        if not data["results"]:
            return "No matching Perk Miner businesses were found.\n\n" + NOTICE
        head = (f"Found {data['total_matches']} match(es); showing {data['returned']}, sorted by "
                f"{data['sorted_by']} (match mode: {data['match_mode']}).")
        lines = [head, ""]
        for i, r in enumerate(data["results"], 1):
            bits = [r.get("category"), r.get("service_area")]
            if "distance_miles" in r:
                bits.append(f"{r['distance_miles']} mi")
            lines.append(f"{i}. **{r['name']}** - " + " · ".join(b for b in bits if b))
            if r.get("services"):
                lines.append("   Services: " + ", ".join(r["services"][:5]))
            if r.get("member_perk"):
                lines.append("   Member perk: " + r["member_perk"])
            for label, key in (("Address", "address"), ("Phone", "phone"), ("Hours", "hours_text"),
                               ("Website", "website"), ("Profile", "url")):
                if r.get(key):
                    lines.append(f"   {label}: {r[key]}")
            if r.get("matched_terms") is not None and data["match_mode"] == "partial_terms":
                lines.append("   Matched only: " + ", ".join(r["matched_terms"]))
        for w in data.get("warnings", []):
            lines += ["", f"Note: {w}"]
        lines += ["", NOTICE]
        return "\n".join(lines)

    def jsonld(self, rec, faqs=None, photos=None):
        """schema.org @graph: ProfilePage + (Local)Business/Organization + BreadcrumbList + WebSite."""
        base, url = self.base_url, rec["url"]
        biz_id = url + "#business"
        node = {
            "@type": (CATEGORY_SCHEMA_TYPE.get(rec["category"], "LocalBusiness") if rec["has_public_address"]
                      else "Organization"),
            "@id": biz_id, "name": rec["name"], "url": url,
        }
        photos = photos if photos is not None else rec["_photos"]
        if rec["_photo"]:
            node["logo"] = rec["_photo"]
        if photos:
            node["image"] = photos
        same_as = ([rec["website"]] if rec["website"].startswith(("https://", "http://")) else []) + \
            list(rec["social"].values())
        if same_as:
            node["sameAs"] = list(dict.fromkeys(same_as))
        if rec["has_public_address"]:
            addr = {"@type": "PostalAddress", "addressCountry": rec["country"]}
            if rec["street"]:
                addr["streetAddress"] = rec["street"]
            for k, v in (("addressLocality", rec["city"]), ("addressRegion", rec["state"]),
                         ("postalCode", rec["postal_code"])):
                if v:
                    addr[k] = v
            node["address"] = addr
        if rec["description"] or rec["member_perk"]:
            node["description"] = _clean(rec["description"] or rec["member_perk"], 300)
        if rec["phone"]:
            node["telephone"] = rec["phone"]
        know = [rec["category_label"]] + rec["services"][:5] if rec["category"] else rec["services"][:5]
        if know:
            node["knowsAbout"] = know
        if rec["services"]:
            node["hasOfferCatalog"] = {
                "@type": "OfferCatalog", "name": f"Services offered by {rec['name']}",
                "itemListElement": [{"@type": "Offer", "itemOffered": {"@type": "Service", "name": s,
                                                                         "provider": {"@id": biz_id}}}
                                    for s in rec["services"]]}
        if rec["hours"]:
            node["openingHours"] = rec["hours"]
        if rec["has_public_address"] and rec["latitude"] is not None and rec["longitude"] is not None:
            node["geo"] = {"@type": "GeoCoordinates", "latitude": rec["latitude"], "longitude": rec["longitude"]}
            node["hasMap"] = f"https://www.google.com/maps/search/?api=1&query={rec['latitude']},{rec['longitude']}"
        if rec.get("_sa_nodes"):
            node["areaServed"] = rec["_sa_nodes"]
        elif rec["city"] or rec["state"]:
            area = {"@type": "City" if rec["city"] else "AdministrativeArea",
                    "address": {"@type": "PostalAddress", "addressCountry": rec["country"]}}
            if rec["city"]:
                area["name"] = rec["city"]
                area["address"]["addressLocality"] = rec["city"]
            if rec["state"]:
                area["address"]["addressRegion"] = rec["state"]
            node["areaServed"] = area
        if rec["keywords"]:
            node["keywords"] = rec["keywords"]
        if node["@type"] == "FoodEstablishment":
            text = " ".join([rec["name"], rec["keywords"], " ".join(rec["services"]), rec["description"]]).lower()
            cuisines = list(dict.fromkeys(label for needle, label in CUISINES
                                          if re.search(r"\b" + re.escape(needle) + r"\b", text)))
            if cuisines:
                node["servesCuisine"] = cuisines
        faq_pairs = [(_clean(_g(f, "question")), str(_g(f, "answer") or "").strip()) for f in (faqs or [])]
        faq_pairs = [(q, a) for q, a in faq_pairs if q and a]
        if faq_pairs:
            node["subjectOf"] = {
                "@type": "FAQPage", "@id": url + "#faqs", "url": url,
                "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                               for q, a in faq_pairs]}
        page = {
            "@type": "ProfilePage", "@id": url + "#profilepage", "url": url, "name": rec["page_title"],
            "description": rec["meta_description"], "inLanguage": "en-US",
            "isPartOf": {"@id": base + "/#website"}, "mainEntity": {"@id": biz_id},
            "breadcrumb": {"@id": url + "#breadcrumb"},
            "speakable": {"@type": "SpeakableSpecification", "cssSelector": ["#pm-summary"]},
        }
        if rec["updated_at"]:
            page["dateModified"] = rec["updated_at"]
        crumbs = {"@type": "BreadcrumbList", "@id": url + "#breadcrumb", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Perk Miner", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Public Profiles", "item": base + "/public_profiles"},
            {"@type": "ListItem", "position": 3, "name": rec["name"], "item": url}]}
        site = {"@type": "WebSite", "@id": base + "/#website", "name": "Perk Miner", "url": base + "/"}
        return {"@context": "https://schema.org", "@graph": [page, node, crumbs, site]}

    # ---- IndexNow (Bing, Yandex, Naver, Seznam; feeds Copilot/ChatGPT search/DuckDuckGo) ----
    def indexnow_key(self):
        key = (os.environ.get("INDEXNOW_KEY") or "").strip()
        return key if re.fullmatch(r"[A-Za-z0-9-]{8,128}", key) else ""

    def indexnow_ping(self, urls):
        key = self.indexnow_key()
        urls = [u for u in urls if u]
        if not key or not urls:
            return False
        payload = json.dumps({"host": urlparse(self.base_url).netloc, "key": key,
                              "keyLocation": f"{self.base_url}/{key}.txt", "urlList": urls[:10000]}).encode()

        def _send():
            try:
                req = Request("https://api.indexnow.org/IndexNow", data=payload, method="POST",
                              headers={"Content-Type": "application/json; charset=utf-8"})
                with urlopen(req, timeout=8) as resp:
                    log.info("IndexNow accepted %d url(s): HTTP %s", len(urls), resp.status)
            except Exception as exc:  # never let a ping break the request that triggered it
                log.warning("IndexNow ping failed: %s", exc)

        threading.Thread(target=_send, daemon=True).start()
        return True

    def indexnow_ping_profile(self, biz):
        slug = getattr(biz, "store_slug", None)
        return self.indexnow_ping([self.public_profile_url(slug)] if slug else [])


def _g(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


# --------------------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------------------
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization, Mcp-Protocol-Version, Mcp-Session-Id",
        "Access-Control-Max-Age": "86400"}


def _respond(body, status=200, mimetype="application/json", max_age=300):
    if not isinstance(body, (str, bytes)):
        body = json.dumps(body, ensure_ascii=False)
    resp = Response(body, status=status, mimetype=mimetype)
    resp.headers.update(CORS)
    if max_age:
        resp.headers["Cache-Control"] = f"public, max-age={max_age}"
    return resp


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "unknown")


def openapi_spec(base_url):
    loc = {"name": "latitude", "in": "query", "schema": {"type": "number"},
           "description": "User latitude; with longitude gives distance-sorted results."}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Perk Miner Business Directory", "version": "1.0.0",
                 "description": "Read-only search of approved Perk Miner advertisers (local businesses). "
                                "Public data only. " + NOTICE},
        "servers": [{"url": base_url}],
        "paths": {
            "/api/public/search": {"get": {
                "operationId": "searchBusinesses",
                "summary": "Search businesses, optionally nearest first",
                "description": "Use for requests like 'nearest Mexican restaurant'. Pass latitude and longitude when "
                               "known, otherwise city and state.",
                "parameters": [
                    {"name": "q", "in": "query", "schema": {"type": "string"},
                     "description": "Free text: business name, cuisine, service or keyword (e.g. 'mexican')."},
                    {"name": "category", "in": "query", "schema": {"type": "string"},
                     "description": "Category such as 'Restaurant-Food-and-Drink' (see /api/public/categories)."},
                    {"name": "city", "in": "query", "schema": {"type": "string"}},
                    {"name": "state", "in": "query", "schema": {"type": "string"},
                     "description": "Two-letter state code, e.g. NV."},
                    loc, {**loc, "name": "longitude", "description": "User longitude."},
                    {"name": "radius_miles", "in": "query", "schema": {"type": "number"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
                                                                 "default": 10}},
                    {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 0, "default": 0}}],
                "responses": {"200": {"description": "Matching businesses",
                                      "content": {"application/json": {"schema": {"type": "object"}}}}}}},
            "/api/public/businesses/{slug}": {"get": {
                "operationId": "getBusiness",
                "summary": "Everything public about one business",
                "parameters": [{"name": "slug", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "Business profile",
                                      "content": {"application/json": {"schema": {"type": "object"}}}},
                              "404": {"description": "Not found"}}}},
            "/api/public/categories": {"get": {
                "operationId": "listCategories", "summary": "List categories with listing counts",
                "responses": {"200": {"description": "Categories",
                                      "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        },
    }


# ---- MCP ----------------------------------------------------------------------------
MCP_TOOLS = [
    {"name": "search_businesses", "title": "Search Perk Miner businesses",
     "description": ("Search approved Perk Miner advertisers (restaurants, home services, retail and more). Use for "
                     "requests like 'nearest Mexican restaurant' or 'plumbers in Las Vegas'. Pass latitude and "
                     "longitude when you know the user's position to get nearest-first results; otherwise pass "
                     "city and state. Returns public listing data only."),
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Cuisine, service, keyword or business name, e.g. 'mexican'."},
         "category": {"type": "string", "description": "Optional category, e.g. 'Restaurant-Food-and-Drink'."},
         "city": {"type": "string"}, "state": {"type": "string", "description": "Two-letter code, e.g. NV."},
         "latitude": {"type": "number"}, "longitude": {"type": "number"},
         "radius_miles": {"type": "number"},
         "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 5}}},
     "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}},
    {"name": "get_business", "title": "Get a Perk Miner business profile",
     "description": ("Everything public about one business: summary, services, hours, contact, member perk, FAQs. "
                     "Use for 'what can you tell me about this business?'. Accepts a slug, a perkminer.com profile "
                     "URL, or the business name."),
     "inputSchema": {"type": "object", "required": ["business"], "properties": {
         "business": {"type": "string", "description": "Profile slug, profile URL or business name."}}},
     "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}},
    {"name": "list_categories", "title": "List business categories",
     "description": "List Perk Miner business categories and how many listings each has.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}},
]
MCP_INSTRUCTIONS = ("Perk Miner LLC is a privacy-first cash-back marketplace connecting members with local business "
                    "advertisers. Use search_businesses for 'nearest/best X' questions and get_business for details "
                    "on one business. Only public listing data is available; do not invent details that are not "
                    "returned. " + NOTICE)


def _rpc_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_result(text, structured=None, error=False):
    out = {"content": [{"type": "text", "text": text}], "isError": error}
    if structured is not None:
        out["structuredContent"] = structured
    return out


def init_ai_discovery(app, *, csrf=None, Business, BusinessFAQ, categories,
                      public_businesses_query, public_profile_url, base_url):
    """Register routes on `app` and return the AIDiscovery helper."""
    ai = AIDiscovery(Business=Business, BusinessFAQ=BusinessFAQ, categories=categories,
                     public_businesses_query=public_businesses_query,
                     public_profile_url=public_profile_url, base_url=base_url)

    def limited():
        ok, retry = ai.limiter.allow(_client_ip())
        if ok:
            return None
        resp = _respond({"error": "rate_limited", "message": "Too many requests; slow down."}, 429, max_age=0)
        resp.headers["Retry-After"] = str(retry)
        return resp

    def preflight():
        resp = Response(status=204)
        resp.headers.update(CORS)
        return resp

    # ---- REST ----
    def api_search():
        if request.method == "OPTIONS":
            return preflight()
        if (r := limited()) is not None:
            return r
        a = request.args
        try:
            data = ai.search(q=a.get("q", ""), category=a.get("category", ""), city=a.get("city", ""),
                             state=a.get("state", ""), lat=a.get("lat") or a.get("latitude"),
                             lng=a.get("lng") or a.get("longitude"), radius_miles=a.get("radius_miles"),
                             limit=a.get("limit", 10), offset=a.get("offset", 0))
        except ValueError as exc:
            return _respond({"error": "bad_request", "message": str(exc)}, 400, max_age=0)
        return _respond({k: v for k, v in data.items() if v is not None})

    def _profile_payload(slug):
        kind, rec = ai.find(slug)
        if kind != "one" or rec["slug"].lower() != slug.lower():
            return None, None, None
        faqs = ai.faqs_for(rec["_id"])
        return rec, faqs, ai.public(rec, detail=True, faqs=faqs)

    def api_business(slug):
        if request.method == "OPTIONS":
            return preflight()
        if (r := limited()) is not None:
            return r
        rec, faqs, payload = _profile_payload(slug)
        if rec is None:
            return _respond({"error": "not_found", "message": "No public business with that slug."}, 404, max_age=60)
        return _respond(payload)

    def profile_json(slug):
        return api_business(slug)

    def profile_md(slug):
        if (r := limited()) is not None:
            return r
        rec, faqs, _ = _profile_payload(slug)
        if rec is None:
            return _respond("Not found\n", 404, "text/plain", max_age=60)
        return _respond(ai.markdown(rec, faqs), mimetype="text/markdown")

    def api_categories():
        if request.method == "OPTIONS":
            return preflight()
        if (r := limited()) is not None:
            return r
        return _respond({"categories": ai.category_counts()})

    def openapi():
        return _respond(openapi_spec(ai.base_url), max_age=3600)

    # ---- MCP ----
    def mcp_call(name, args):
        args = args if isinstance(args, dict) else {}
        try:
            if name == "search_businesses":
                data = ai.search(q=args.get("query", ""), category=args.get("category", ""),
                                 city=args.get("city", ""), state=args.get("state", ""),
                                 lat=args.get("latitude"), lng=args.get("longitude"),
                                 radius_miles=args.get("radius_miles"), limit=args.get("limit", 5))
                data = {k: v for k, v in data.items() if v is not None}
                return _tool_result(ai.search_markdown(data), data)
            if name == "get_business":
                kind, rec = ai.find(args.get("business", ""))
                if kind == "none":
                    return _tool_result("No public Perk Miner business matched that name or slug. "
                                        "Try search_businesses.", error=True)
                if kind == "many":
                    cands = [{"name": r["name"], "slug": r["slug"], "city": r["city"], "state": r["state"]}
                             for r in rec]
                    text = "Several businesses match; call get_business again with one slug:\n" + "\n".join(
                        f"- {c['name']} ({c['city']}, {c['state']}) slug: {c['slug']}" for c in cands)
                    return _tool_result(text, {"candidates": cands})
                faqs = ai.faqs_for(rec["_id"])
                return _tool_result(ai.markdown(rec, faqs), ai.public(rec, detail=True, faqs=faqs))
            if name == "list_categories":
                cats = ai.category_counts()
                return _tool_result("\n".join(f"- {c['category']} ({c['listings']})" for c in cats),
                                    {"categories": cats})
        except ValueError as exc:
            return _tool_result(str(exc), error=True)
        except Exception:
            log.exception("MCP tool %s failed", name)
            return _tool_result("The directory is temporarily unavailable. Please try again.", error=True)
        return None

    def mcp_handle(msg):
        if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
            return None                          # a client reply to a server request; nothing to answer
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        method, id_, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if "id" not in msg:                      # notification: no response
            return None
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return {"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": wanted if wanted in MCP_SUPPORTED else MCP_LATEST,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "perkminer-directory", "title": "Perk Miner Business Directory",
                               "version": "1.0.0"},
                "instructions": MCP_INSTRUCTIONS}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": id_, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": id_, "result": {"tools": MCP_TOOLS}}
        if method == "tools/call":
            if params.get("name") not in {t["name"] for t in MCP_TOOLS}:
                return _rpc_error(id_, -32602, f"Unknown tool: {params.get('name')}")
            return {"jsonrpc": "2.0", "id": id_, "result": mcp_call(params["name"], params.get("arguments"))}
        if method in ("resources/list", "prompts/list"):
            return {"jsonrpc": "2.0", "id": id_, "result": {method.split("/")[0]: []}}
        return _rpc_error(id_, -32601, f"Method not found: {method}")

    def mcp_endpoint():
        if request.method == "OPTIONS":
            return preflight()
        if request.method != "POST":              # no server-initiated streams; stateless server
            resp = _respond({"error": "method_not_allowed", "message": "POST JSON-RPC messages to /mcp."},
                            405, max_age=0)
            resp.headers["Allow"] = "POST, OPTIONS"
            return resp
        if (r := limited()) is not None:
            return r
        try:
            payload = json.loads(request.get_data(as_text=True) or "null")
        except ValueError:
            return _respond(_rpc_error(None, -32700, "Parse error"), 400, max_age=0)
        if isinstance(payload, list):
            replies = [x for x in (mcp_handle(m) for m in payload[:20]) if x is not None]
            return _respond(replies, max_age=0) if replies else _respond("", 202, max_age=0)
        reply = mcp_handle(payload)
        return _respond(reply, max_age=0) if reply is not None else _respond("", 202, max_age=0)

    # ---- routes ----
    rules = [
        ("/api/public/search", "ai_api_search", api_search, ["GET", "OPTIONS"]),
        ("/api/public/businesses/<slug>", "ai_api_business", api_business, ["GET", "OPTIONS"]),
        ("/api/public/categories", "ai_api_categories", api_categories, ["GET", "OPTIONS"]),
        ("/public_profiles/<slug>.json", "ai_profile_json", profile_json, ["GET"]),
        ("/public_profiles/<slug>.md", "ai_profile_md", profile_md, ["GET"]),
        ("/openapi.json", "ai_openapi", openapi, ["GET"]),
        ("/mcp", "ai_mcp", mcp_endpoint, ["GET", "POST", "DELETE", "OPTIONS"]),
    ]
    for rule, endpoint, view, methods in rules:
        app.add_url_rule(rule, endpoint, view, methods=methods)
    if csrf is not None:
        csrf.exempt(mcp_endpoint)               # JSON-RPC clients have no CSRF token; endpoint is read-only

    key = ai.indexnow_key()
    if key:
        app.add_url_rule(f"/{key}.txt", "ai_indexnow_key", lambda: Response(key, mimetype="text/plain"))
    elif os.environ.get("INDEXNOW_KEY"):
        log.warning("INDEXNOW_KEY must be 8-128 characters (letters, digits, dashes); IndexNow disabled.")

    @app.cli.command("indexnow-submit-all")
    def indexnow_submit_all():
        """Send every public profile URL to IndexNow (set INDEXNOW_KEY first)."""
        import click
        if not ai.indexnow_key():
            click.echo("Set INDEXNOW_KEY (8-128 letters/digits/dashes) first.")
            return
        urls = [r["url"] for r in ai.snapshot()]
        ai.indexnow_ping(urls)
        click.echo(f"Submitted {len(urls)} profile URL(s) to IndexNow.")
        time.sleep(3)  # let the background thread finish before the process exits

    app.extensions["ai_discovery"] = ai
    return ai
