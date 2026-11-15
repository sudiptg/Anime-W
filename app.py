# -*- coding: utf-8 -*-
"""
Anime-W  -  Telegram Mini App + Blogger widget backend (Flask)
===============================================================
Kya karta hai:
  * index.html (Mini App) serve karta hai + AniList se anime data deta hai (/api/...)
  * Telegram bot (webhook): /start /app /id  +  owner ke liye /set_link /set_ep /del_link /links /status /refresh
  * /set_link wale links MongoDB me save hote hain (Render restart par bhi bache rehte hain)

Chalane ka tareeka (Render Start Command):   python app.py
"""
import copy
import hmac
import html
import json
import os
import random
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config, tg_url

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")
START_TIME = time.time()
MAX_EPISODES = 2000


def log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:  # pragma: no cover
        pass


def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x) -> list:
    return x if isinstance(x, list) else []


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def _scrub(text: str) -> str:
    """Logs / replies me kabhi token ya DB password na jaye."""
    text = str(text)
    if Config.BOT_TOKEN:
        text = text.replace(Config.BOT_TOKEN, "***")
    return re.sub(r"mongodb(\+srv)?://[^\s'\"]+", "mongodb://***", text)


app = Flask(__name__, static_folder=None)  # static_folder=None: project ki files (config.py, .json ...) bahar nahi dikhengi
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Render proxy ke peeche sahi https URL
try:
    app.json.sort_keys = False
    app.json.ensure_ascii = False
except Exception:  # pragma: no cover
    pass

# =============================================================================
# 1. HINDI DUB LIST  (AniList ID -> title ka keyword)
#    Keyword se app khud check karta hai ki ID sahi anime ki hai ya nahi.
#    Galat ID wala anime "Hindi Dub" nahi dikhaya jaata.
# =============================================================================
HINDI_DUB_SEEDS = {
    151807: "solo leveling|ore dake level",
    101922: "kimetsu|demon slayer",
    142329: "kimetsu|demon slayer",
    166240: "kimetsu|demon slayer",
    113415: "jujutsu",
    145064: "jujutsu",
    20: "naruto",
    1735: "naruto",
    21: "one piece",
    102976: "jojo",
    21459: "boku no hero|my hero",
    97940: "black clover",
    1535: "death note",
    16498: "shingeki|attack on titan",
    127230: "chainsaw",
    140960: "spy x family|spy×family|spy family",
    131586: "blue lock",
    154587: "frieren|sousou no",
    163134: "re:zero|re zero",
    171018: "dandadan|dan da dan",
    162804: "kaiju|kaijuu",
    153288: "mashle",
    108465: "mushoku",
    161645: "kusuriya|apothecary",
    156822: "blue box|ao no hako",
    139518: "classroom of the elite|youkoso jitsuryoku",
    146065: "classroom of the elite|youkoso jitsuryoku",
    125367: "tokyo revengers",
    20605: "tokyo ghoul",
    11061: "hunter x hunter|hunter×hunter",
    269: "bleach",
    116674: "bleach",
    20958: "haikyu",
    104578: "vinland",
    98659: "classroom of the elite|youkoso jitsuryoku",
}
HINDI_DUB_ANILIST_IDS = set(HINDI_DUB_SEEDS)  # purana naam


def seed_matches(aid: int, media: dict) -> bool:
    kws = HINDI_DUB_SEEDS.get(aid)
    if not kws:
        return False
    t = _d(_d(media).get("title"))
    hay = " ".join(str(t.get(k) or "") for k in ("romaji", "english", "native")).lower()
    return any(k and k in hay for k in kws.split("|"))


# =============================================================================
# 2. CACHE  (AniList ki limit bachane ke liye; fail hone par purana data dikhata hai)
# =============================================================================
API_CACHE = {}
_CACHE_LOCK = threading.Lock()
_STRIPES = [threading.Lock() for _ in range(32)]


def _stripe(key: str):
    return _STRIPES[hash(key) % len(_STRIPES)]


def get_cached(key: str, allow_stale: bool = False):
    with _CACHE_LOCK:
        entry = API_CACHE.get(key)
    if not entry:
        return None
    age = time.time() - entry["ts"]
    if age <= entry["ttl"]:
        return entry["data"]
    if age > Config.STALE_MAX_SECONDS:
        with _CACHE_LOCK:
            API_CACHE.pop(key, None)
        return None
    return entry["data"] if allow_stale else None


def set_cached(key: str, data, ttl: int = None) -> None:
    with _CACHE_LOCK:
        if len(API_CACHE) >= 400:
            for k, _ in sorted(API_CACHE.items(), key=lambda kv: kv[1]["ts"])[:120]:
                API_CACHE.pop(k, None)
        API_CACHE[key] = {"ts": time.time(), "ttl": ttl or Config.CACHE_TTL_SECONDS, "data": data}


def invalidate_dub_caches() -> None:
    """Link badalne par Hindi rail / dub-search purane na rahein."""
    with _CACHE_LOCK:
        for k in list(API_CACHE):
            if k == "home_feed" or (k.startswith("search|") and k.endswith("|1")):
                API_CACHE.pop(k, None)


# =============================================================================
# 3. LINK STORE  (/set_link ka data - MongoDB me, restart par bhi safe)
# =============================================================================
class LinkStore:
    """
    {"151807": {"telegram_url": "...", "episodes": {"1": "..."}, "title": "Solo Leveling"}}
    * MONGO_URI(s) ho to har badlav sab clusters me save hota hai; sabse naya `ver` jeetta hai.
    * Koi cluster down ho to skip, wapas aane par heal ho jata hai.
    * DB configured hai par ek bhi padh nahi paya -> likhna band (warna purana data overwrite ho sakta tha).
    * MONGO_URI na ho to sirf local file (Render par restart/sleep me ud jaati hai).
    """

    DOC_ID = "custom_links"

    def __init__(self, uris, db_name, coll_name, file_path):
        self.db_name = db_name
        self.coll_name = coll_name
        self.file_path = file_path
        self.links = {}
        self.ver = 0
        self.ts = 0.0
        self.loaded_from = "empty"
        self.loaded_ok = False
        self.on_change = None
        self._wlock = threading.RLock()
        self._nodes = [
            {"uri": u, "label": f"DB{i + 1}", "client": None, "coll": None, "ok": None, "ver": -1, "err": ""}
            for i, u in enumerate(uris)
        ]

    # ---- helpers -----------------------------------------------------------
    @property
    def persistent(self) -> bool:
        return bool(self._nodes)

    @staticmethod
    def _valid_links(obj) -> dict:
        out = {}
        for k, v in _d(obj).items():
            if not isinstance(v, dict) or not str(k).isdigit():
                continue
            url = v.get("telegram_url")
            eps = _d(v.get("episodes"))
            rec = {
                "telegram_url": url if isinstance(url, str) else "",
                "episodes": {str(a): b for a, b in eps.items() if isinstance(b, str) and str(a).isdigit()},
            }
            if isinstance(v.get("title"), str):
                rec["title"] = v["title"][:120]
            out[str(k)] = rec
        return out

    def _read_local(self):
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            log(f"[Links] local file padh nahi paya: {_scrub(e)}")
            return None
        if isinstance(data, dict) and isinstance(data.get("links"), dict):
            return {"ver": int(data.get("ver") or 0), "ts": float(data.get("ts") or 0), "links": self._valid_links(data["links"])}
        links = self._valid_links(data)  # purana flat format
        return {"ver": 1 if links else 0, "ts": 0.0, "links": links}

    def _write_local(self) -> None:
        try:
            tmp = self.file_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"ver": self.ver, "ts": self.ts, "links": self.links}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.file_path)
        except Exception as e:
            log(f"[Links] local file save warning: {_scrub(e)}")

    # ---- mongo -------------------------------------------------------------
    def _connect(self, node):
        if node["coll"] is not None:
            return node["coll"]
        try:
            from pymongo import MongoClient
        except Exception:
            raise RuntimeError("pymongo install nahi hai (requirements.txt me pymongo hona chahiye)") from None
        kw = dict(serverSelectionTimeoutMS=6000, connectTimeoutMS=6000, socketTimeoutMS=15000,
                  appName="flyton-miniapp", maxPoolSize=3, retryWrites=True)
        low = node["uri"].lower()
        if low.startswith("mongodb+srv://") or "tls=true" in low or "ssl=true" in low:
            try:
                import certifi

                kw["tlsCAFile"] = certifi.where()
            except Exception:
                pass
        client = MongoClient(node["uri"], **kw)
        try:
            client.admin.command("ping")
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            raise
        node["client"] = client
        node["coll"] = client[self.db_name][self.coll_name]
        return node["coll"]

    def _node_fail(self, node, err) -> None:
        node["ok"] = False
        node["err"] = _scrub(f"{type(err).__name__}: {err}")[:160]
        try:
            if node["client"] is not None:
                node["client"].close()
        except Exception:
            pass
        node["client"] = None
        node["coll"] = None
        log(f"[Links] {node['label']} problem: {node['err']}")

    def _read_node(self, node):
        try:
            coll = self._connect(node)
            doc = coll.find_one({"_id": self.DOC_ID})
        except Exception as e:
            self._node_fail(node, e)
            return None
        node["ok"] = True
        node["err"] = ""
        if not doc:
            node["ver"] = 0
            return {"ver": 0, "ts": 0.0, "links": {}}
        ver = int(doc.get("ver") or 0)
        node["ver"] = ver
        return {"ver": ver, "ts": float(doc.get("ts") or 0), "links": self._valid_links(doc.get("links"))}

    def _push(self, node) -> bool:
        try:
            coll = self._connect(node)
            coll.replace_one({"_id": self.DOC_ID},
                             {"_id": self.DOC_ID, "ver": self.ver, "ts": self.ts, "links": self.links}, upsert=True)
        except Exception as e:
            self._node_fail(node, e)
            return False
        node["ver"] = self.ver
        node["ok"] = True
        node["err"] = ""
        return True

    # ---- load / heal ---------------------------------------------------------
    def load(self) -> None:
        with self._wlock:
            best = {"ver": 0, "ts": 0.0, "links": {}, "src": "empty"}
            local = self._read_local()
            if local:
                best = dict(local, src="file")
            any_ok = False
            for node in self._nodes:
                doc = self._read_node(node)
                if doc is None:
                    continue
                any_ok = True
                key, cur = (doc["ver"], doc["ts"]), (best["ver"], best["ts"])
                # cloud, local file se barabari me jeetta hai; do clusters barabar hon to pehla cluster
                if key > cur or (key == cur and best["src"] in ("file", "empty")):
                    best = dict(doc, src=node["label"])
            self.links, self.ver, self.ts, self.loaded_from = best["links"], best["ver"], best["ts"], best["src"]
            self.loaded_ok = (not self._nodes) or any_ok
            for node in self._nodes:
                if node["ok"] and node["ver"] < self.ver:
                    self._push(node)
        log(f"[Links] loaded {len(self.links)} link(s) from {self.loaded_from} (ver {self.ver}); "
            f"MongoDB clusters: {len(self._nodes)}")

    def heal_once(self) -> None:
        if not self._nodes:
            return
        changed = False
        with self._wlock:
            # 1) jo cluster abhi tak padha nahi gaya / fail hua tha use pehle PADHO (naya data mile to apna lo)
            for node in self._nodes:
                if node["ok"] is True:
                    continue
                doc = self._read_node(node)
                if doc is None:
                    continue
                self.loaded_ok = True
                if (doc["ver"], doc["ts"]) > (self.ver, self.ts):
                    self.links, self.ver, self.ts, self.loaded_from = doc["links"], doc["ver"], doc["ts"], node["label"]
                    self._write_local()
                    changed = True
            # 2) phir jo cluster peeche reh gaye unhe naya data bhejo
            for node in self._nodes:
                if node["ok"] is True and node["ver"] < self.ver:
                    self._push(node)
        if changed and self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def heal_loop(self, interval: int = 60) -> None:
        while True:
            time.sleep(interval)
            try:
                self.heal_once()
            except Exception as e:  # pragma: no cover
                log(f"[Links] heal error: {_scrub(e)}")

    # ---- read API (lock-free) -------------------------------------------------
    def get(self, aid):
        return self.links.get(str(aid))

    def ids(self) -> list:
        return [int(k) for k in self.links]

    # ---- write API -------------------------------------------------------------
    def can_write(self) -> bool:
        return (not self._nodes) or self.loaded_ok

    def _commit(self, new_links: dict) -> dict:
        self.ver += 1
        self.ts = time.time()
        self.links = new_links
        self._write_local()
        ok = 0
        for node in self._nodes:
            if node["ok"] and self._push(node):
                ok += 1
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass
        return {"persisted": ok, "total": len(self._nodes), "ver": self.ver}

    def set_link(self, aid: int, url: str, title: str = None) -> dict:
        with self._wlock:
            if not self.can_write():
                return {"error": "db_down"}
            new = copy.deepcopy(self.links)
            rec = new.get(str(aid)) or {"telegram_url": "", "episodes": {}}
            rec["telegram_url"] = url
            if title:
                rec["title"] = title[:120]
            new[str(aid)] = rec
            return self._commit(new)

    def set_episode(self, aid: int, ep: int, url: str, title: str = None) -> dict:
        with self._wlock:
            if not self.can_write():
                return {"error": "db_down"}
            new = copy.deepcopy(self.links)
            rec = new.get(str(aid)) or {"telegram_url": "", "episodes": {}}
            rec.setdefault("episodes", {})[str(ep)] = url
            if title and not rec.get("title"):
                rec["title"] = title[:120]
            new[str(aid)] = rec
            return self._commit(new)

    def delete(self, aid: int, ep: int = None) -> dict:
        with self._wlock:
            if not self.can_write():
                return {"error": "db_down"}
            new = copy.deepcopy(self.links)
            rec = new.get(str(aid))
            if not rec:
                return {"removed": False, "persisted": 0, "total": len(self._nodes)}
            if ep is None:
                del new[str(aid)]
            elif str(ep) in rec.get("episodes", {}):
                del rec["episodes"][str(ep)]
            else:
                return {"removed": False, "persisted": 0, "total": len(self._nodes)}
            res = self._commit(new)
            res["removed"] = True
            return res

    def status(self) -> dict:
        return {
            "persistent": self.persistent,
            "loaded_from": self.loaded_from,
            "loaded_ok": self.loaded_ok,
            "ver": self.ver,
            "nodes": [{"label": n["label"], "ok": n["ok"], "ver": n["ver"], "err": n["err"]} for n in self._nodes],
            "count": len(self.links),
            "episode_links": sum(len(_d(v.get("episodes"))) for v in self.links.values()),
        }


STORE = LinkStore(Config.mongo_uris(), Config.MONGO_DB_NAME, Config.MONGO_COLLECTION,
                  os.path.join(BASE_DIR, Config.LINKS_FILE) if not os.path.isabs(Config.LINKS_FILE) else Config.LINKS_FILE)
STORE.on_change = invalidate_dub_caches
CUSTOM_LINKS = STORE.links  # (purana naam; live data ke liye STORE.links use hota hai)


# =============================================================================
# 4. ANILIST CLIENT
# =============================================================================
LIST_FRAGMENT = """
id
title { romaji english native }
coverImage { extraLarge large color }
bannerImage
format
status
episodes
season
seasonYear
averageScore
popularity
genres
isAdult
nextAiringEpisode { episode airingAt timeUntilAiring }
"""

DETAIL_FRAGMENT = LIST_FRAGMENT + """
idMal
duration
studios(isMain: true) { nodes { name } }
description(asHtml: false)
trailer { id site }
"""

MEDIA_FRAGMENT = DETAIL_FRAGMENT  # purana naam

_ANILIST_CALLS = deque()
_ANILIST_LOCK = threading.Lock()
ANILIST_STATE = {"blocked_until": 0.0, "last_error": "", "last_ok": 0.0, "calls": 0}


def _budget_ok() -> bool:
    now = time.time()
    with _ANILIST_LOCK:
        while _ANILIST_CALLS and now - _ANILIST_CALLS[0] > 60:
            _ANILIST_CALLS.popleft()
        if now < ANILIST_STATE["blocked_until"] or len(_ANILIST_CALLS) >= Config.ANILIST_MAX_PER_MIN:
            return False
        _ANILIST_CALLS.append(now)
        ANILIST_STATE["calls"] += 1
        return True


def anilist_request(query: str, variables: dict = None) -> dict:
    """AniList ko call. Hamesha dict deta hai: success me {"data": {...}}, fail me {"data": None, "errors": [...], "_status": N}."""
    if not _budget_ok():
        return {"data": None, "errors": [{"message": "AniList busy (rate limit)", "status": 429}], "_status": 429}
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    last_err = "unknown"
    for attempt in range(2):
        req = urllib.request.Request(
            Config.ANILIST_GRAPHQL_URL, data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": f"{Config.APP_NAME}/3.0"})
        try:
            with urllib.request.urlopen(req, timeout=Config.ANILIST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if isinstance(body, dict):
                body.setdefault("data", None)
                if body.get("data"):
                    ANILIST_STATE["last_ok"] = time.time()
                    ANILIST_STATE["last_error"] = ""
                else:
                    ANILIST_STATE["last_error"] = _scrub(str(body.get("errors"))[:160])
                return body
            last_err = "bad response"
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            body["data"] = None
            body["_status"] = e.code
            if e.code == 429:
                try:
                    wait = int(e.headers.get("Retry-After") or 30)
                except (TypeError, ValueError):
                    wait = 30
                ANILIST_STATE["blocked_until"] = time.time() + min(max(wait, 5), 90)
                ANILIST_STATE["last_error"] = "429 Too Many Requests (AniList rate limit)"
                log("[AniList] 429 rate limit - kuch der ruk raha hoon")
                return body
            if e.code >= 500 and attempt == 0:
                time.sleep(0.8)
                continue
            ANILIST_STATE["last_error"] = f"HTTP {e.code}: {_scrub(str(body.get('errors'))[:120])}"
            return body
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt == 0:
                time.sleep(0.8)
                continue
    ANILIST_STATE["last_error"] = _scrub(last_err)[:160]
    log(f"[AniList API Warning] {ANILIST_STATE['last_error']}")
    return {"data": None, "errors": [{"message": last_err}], "_status": 0}


def _page_media(data, alias: str) -> list:
    return [m for m in _l(_d(_d(data).get(alias)).get("media")) if isinstance(m, dict)]


def api_unavailable(msg: str = "Anime data abhi load nahi ho paya. Thodi der baad try karo."):
    wait = int(ANILIST_STATE["blocked_until"] - time.time())
    wait = wait if wait > 0 else 8
    resp = jsonify({"error": msg, "retry_after": wait})
    resp.status_code = 503
    resp.headers["Retry-After"] = str(wait)
    return resp


# =============================================================================
# 5. NORMALIZER
# =============================================================================
FORMAT_LABELS = {"TV": "TV", "TV_SHORT": "TV Short", "MOVIE": "Movie", "SPECIAL": "Special", "OVA": "OVA",
                 "ONA": "ONA", "MUSIC": "Music"}


def slugify_title(title: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9\s-]", "", title or "").strip().lower()
    return re.sub(r"[\s-]+", "-", clean) or "anime"


def clean_description(raw) -> str:
    text = re.sub(r"<br\s*/?>", "\n", str(raw or ""), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:1500] if text else "No synopsis available for this anime."


def normalize_anime(media, detail: bool = False) -> dict:
    media = _d(media)
    if not media:
        return {}
    try:
        aid = int(media.get("id") or 0)
    except (TypeError, ValueError):
        aid = 0
    if aid <= 0:
        return {}

    titles = _d(media.get("title"))
    display_title = titles.get("english") or titles.get("romaji") or titles.get("native") or "Unknown Anime"
    romaji_title = titles.get("romaji") or display_title

    cover = _d(media.get("coverImage"))
    poster = (cover.get("extraLarge") if detail else cover.get("large")) or cover.get("extraLarge") or cover.get("large") or ""
    banner = media.get("bannerImage") or cover.get("extraLarge") or poster

    nxt = media.get("nextAiringEpisode")
    nxt = nxt if isinstance(nxt, dict) and nxt.get("episode") else None
    total = media.get("episodes") if isinstance(media.get("episodes"), int) and media.get("episodes") > 0 else None
    if nxt:
        latest_ep = max(int(nxt["episode"]) - 1, 0)
    elif total:
        latest_ep = total
    else:
        latest_ep = 0

    studios = [s["name"] for s in _l(_d(media.get("studios")).get("nodes")) if isinstance(s, dict) and s.get("name")]

    link = STORE.get(aid)
    is_hindi = bool(link) or (aid in HINDI_DUB_SEEDS and seed_matches(aid, media))
    tg_link = _d(link).get("telegram_url") or Config.MAIN_CHANNEL_URL

    avg = media.get("averageScore")
    score = round(avg / 10.0, 1) if isinstance(avg, (int, float)) and avg > 0 else None
    season = " ".join(p for p in (str(media.get("season") or "").title(), str(media.get("seasonYear") or "")) if p)
    fmt = media.get("format") or "TV"

    out = {
        "id": aid,
        "mal_id": media.get("idMal"),
        "title": display_title,
        "romaji": romaji_title,
        "slug": slugify_title(romaji_title),
        "poster": poster,
        "banner": banner,
        "color": cover.get("color") or Config.THEME_PRIMARY_COLOR,
        "format": FORMAT_LABELS.get(fmt, str(fmt).replace("_", " ").title()),
        "status": media.get("status") or "RELEASING",
        "episodes": total or latest_ep or None,
        "latest_ep": latest_ep,
        "duration": media.get("duration"),
        "season": season,
        "score": score,
        "popularity": media.get("popularity") or 0,
        "genres": _l(media.get("genres"))[:4],
        "studio": studios[0] if studios else Config.APP_NAME,
        "is_hindi_dub": is_hindi,
        "has_custom_link": bool(link),
        "audio_badge": Config.DEFAULT_AUDIO_BADGE if is_hindi else "Japanese [Eng Sub]",
        "qualities": Config.DEFAULT_QUALITIES,
        "next_airing": nxt,
        "telegram_url": tg_link,
        "channel_tag": Config.MAIN_CHANNEL_USERNAME,
    }
    if detail:
        out["synopsis"] = clean_description(media.get("description"))
        out["trailer"] = media.get("trailer") if isinstance(media.get("trailer"), dict) else None
    return out


def dub_candidate_ids() -> list:
    ids = list(HINDI_DUB_SEEDS)
    for i in STORE.ids():
        if i not in HINDI_DUB_SEEDS:
            ids.append(i)
    return ids[:80]


def _norm_list(media_list, limit: int = None) -> list:
    out = []
    for m in media_list:
        if m.get("isAdult"):
            continue
        n = normalize_anime(m)
        if n:
            out.append(n)
    return out[:limit] if limit else out


# =============================================================================
# 6. WEB APP & API ROUTES
# =============================================================================
@app.after_request
def add_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = Config.CORS_ALLOWED_ORIGINS
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


def _is_api() -> bool:
    return request.path.startswith("/api/")


@app.errorhandler(404)
def _not_found(_e):
    return (jsonify({"error": "Not found"}), 404) if _is_api() else (Response("Not found", 404, mimetype="text/plain"))


@app.errorhandler(405)
def _bad_method(_e):
    return (jsonify({"error": "Method not allowed"}), 405) if _is_api() else (Response("Method not allowed", 405, mimetype="text/plain"))


@app.errorhandler(Exception)
def _server_error(e):
    if isinstance(e, HTTPException):
        return e
    log("[Error] " + _scrub(traceback.format_exc()))
    if _is_api():
        return jsonify({"error": "Server error, please try again."}), 500
    return Response("Server error", 500, mimetype="text/plain")


def public_config() -> dict:
    return {
        "app_name": Config.APP_NAME,
        "tagline": Config.APP_TAGLINE,
        "logo_url": Config.APP_LOGO_URL,
        "credit_text": Config.CREDIT_TEXT,
        "developer_handle": Config.DEVELOPER_HANDLE,
        "main_channel_username": Config.MAIN_CHANNEL_USERNAME,
        "main_channel_url": Config.MAIN_CHANNEL_URL,
        "news_channel_url": Config.NEWS_CHANNEL_URL,
        "backup_channel_url": Config.BACKUP_CHANNEL_URL,
        "blogger_url": Config.BLOGGER_WEBSITE_URL,
        "default_audio": Config.DEFAULT_AUDIO_BADGE,
        "qualities": Config.DEFAULT_QUALITIES,
        "player_enabled": Config.PLAYER_ENABLED,
        "theme": {
            "primary": Config.THEME_PRIMARY_COLOR,
            "secondary": Config.THEME_SECONDARY_COLOR,
            "bg": Config.THEME_BG_COLOR,
            "glass": Config.THEME_GLASS_CARD,
        },
    }


_INDEX = {"mtime": None, "raw": ""}


def render_index() -> str:
    mtime = os.path.getmtime(INDEX_PATH)
    if _INDEX["mtime"] != mtime:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            _INDEX["raw"] = f.read()
        _INDEX["mtime"] = mtime
    page = _INDEX["raw"]
    cfg = (json.dumps(public_config(), ensure_ascii=False)
           .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    page = page.replace('{"__inject__": true}', cfg)
    return page.replace("<title>Anime App</title>", "<title>%s</title>" % html.escape(Config.APP_NAME + " - Watch & Download HD Anime"))


@app.route("/")
def serve_index():
    """Telegram Mini App / Web Portal UI (config seedha page me daal diya jaata hai: koi flash nahi)."""
    try:
        page = render_index()
    except OSError:
        return Response("index.html nahi mila - GitHub repo me index.html upload karo.", 500, mimetype="text/plain")
    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/health")
def health_check():
    return jsonify({"status": "ok", "app": Config.APP_NAME, "credit": Config.CREDIT_TEXT,
                    "channel": Config.MAIN_CHANNEL_USERNAME})


@app.route("/api/config")
def api_config():
    return jsonify(public_config())


def _get_home_raw():
    """(data, is_stale). AniList fail ho to purana cache; kuch bhi na ho to (None, False)."""
    key = "home_feed"
    data = get_cached(key)
    if data is not None:
        return data, False
    with _stripe(key):
        data = get_cached(key)
        if data is not None:
            return data, False
        query = """
        query ($dubIds: [Int]) {
          trending: Page(page: 1, perPage: 14) { media(type: ANIME, sort: TRENDING_DESC, isAdult: false) { %(f)s } }
          hindiDubs: Page(page: 1, perPage: 40) { media(type: ANIME, id_in: $dubIds, sort: POPULARITY_DESC, isAdult: false) { %(f)s } }
          airing: Page(page: 1, perPage: 14) { media(type: ANIME, status: RELEASING, sort: POPULARITY_DESC, isAdult: false) { %(f)s } }
          popular: Page(page: 1, perPage: 12) { media(type: ANIME, sort: POPULARITY_DESC, isAdult: false) { %(f)s } }
          movies: Page(page: 1, perPage: 12) { media(type: ANIME, format: MOVIE, sort: POPULARITY_DESC, isAdult: false) { %(f)s } }
        }
        """ % {"f": LIST_FRAGMENT}
        raw = anilist_request(query, {"dubIds": dub_candidate_ids()})
        d = raw.get("data")
        if isinstance(d, dict) and _page_media(d, "trending"):
            set_cached(key, d, Config.CACHE_TTL_SECONDS)
            return d, False
        stale = get_cached(key, allow_stale=True)
        return (stale, True) if stale is not None else (None, False)


@app.route("/api/home")
def api_home():
    """Trending, Airing, Popular, Movies aur Hindi Dubbed - ek hi call me."""
    d, stale = _get_home_raw()
    if d is None:
        return api_unavailable()
    dubs = [n for n in _norm_list(_page_media(d, "hindiDubs")) if n["is_hindi_dub"]]
    dubs.sort(key=lambda n: 0 if n["has_custom_link"] else 1)  # tumhare /set_link wale pehle
    trending = _norm_list(_page_media(d, "trending"))
    result = {
        "branding": {"app_name": Config.APP_NAME, "credit": Config.CREDIT_TEXT,
                     "channel": Config.MAIN_CHANNEL_USERNAME, "channel_url": Config.MAIN_CHANNEL_URL},
        "spotlight": trending[:6],
        "trending": trending,
        "hindi_dubs": dubs[:16],
        "airing": _norm_list(_page_media(d, "airing")),
        "popular": _norm_list(_page_media(d, "popular")),
        "movies": _norm_list(_page_media(d, "movies")),
        "stale": stale,
    }
    return jsonify(result)


_GENRE_RE = re.compile(r"^[A-Za-z][A-Za-z \-]{1,30}$")


@app.route("/api/search")
def api_search():
    """Title / genre / Hindi-dub filter se search."""
    q = re.sub(r"\s+", " ", request.args.get("q") or "").strip()[:80]
    genre = (request.args.get("genre") or "").strip()[:30]
    if genre.upper() == "ALL" or not _GENRE_RE.match(genre):
        genre = ""
    dub_only = (request.args.get("dub") or "") in ("1", "true", "yes")

    key = f"search|{q.lower()}|{genre.lower()}|{int(dub_only)}"
    results = get_cached(key)
    stale = False
    if results is None:
        with _stripe(key):
            results = get_cached(key)
            if results is None:
                variables = {"page": 1, "perPage": 24}
                filters = ["type: ANIME", "isAdult: false"]
                var_defs = ["$page: Int", "$perPage: Int"]
                if q:
                    var_defs.append("$search: String")
                    filters.append("search: $search")
                    variables["search"] = q
                if genre:
                    var_defs.append("$genre: String")
                    filters.append("genre: $genre")
                    variables["genre"] = genre
                if dub_only:
                    var_defs.append("$dubIds: [Int]")
                    filters.append("id_in: $dubIds")
                    variables["dubIds"] = dub_candidate_ids()
                filters.append("sort: %s" % ("SEARCH_MATCH" if q else "POPULARITY_DESC"))
                query = "query (%s) { Page(page: $page, perPage: $perPage) { media(%s) { %s } } }" % (
                    ", ".join(var_defs), ", ".join(filters), LIST_FRAGMENT)
                raw = anilist_request(query, variables)
                d = raw.get("data")
                if isinstance(d, dict) and isinstance(d.get("Page"), dict):
                    results = _page_media({"p": d["Page"]}, "p")
                    set_cached(key, results, Config.CACHE_TTL_SECONDS)  # sirf SAFAL jawab cache hota hai
                else:
                    results = get_cached(key, allow_stale=True)
                    stale = results is not None
    if results is None:
        return api_unavailable()
    items = _norm_list(results)
    if dub_only:
        items = [n for n in items if n["is_hindi_dub"]]
    return jsonify({"query": q, "genre": genre, "count": len(items), "results": items, "stale": stale})


@app.route("/api/schedule")
def api_schedule():
    """Is hafte ke popular airing anime, unke agle episode ke time ke hisaab se."""
    key = "schedule"
    media = get_cached(key)
    stale = False
    if media is None:
        with _stripe(key):
            media = get_cached(key)
            if media is None:
                query = """
                query {
                  a: Page(page: 1, perPage: 50) { media(type: ANIME, status: RELEASING, sort: POPULARITY_DESC, isAdult: false) { %(f)s } }
                  b: Page(page: 2, perPage: 50) { media(type: ANIME, status: RELEASING, sort: POPULARITY_DESC, isAdult: false) { %(f)s } }
                }
                """ % {"f": LIST_FRAGMENT}
                raw = anilist_request(query)
                d = raw.get("data")
                found = _page_media(d, "a") + _page_media(d, "b") if isinstance(d, dict) else []
                if found:
                    media = found
                    set_cached(key, media, 1200)
                else:
                    media = get_cached(key, allow_stale=True)
                    stale = media is not None
    if media is None:
        return api_unavailable()

    now = int(time.time())
    items = []
    for m in media:
        if m.get("isAdult"):
            continue
        nxt = m.get("nextAiringEpisode")
        if not isinstance(nxt, dict) or not nxt.get("airingAt"):
            continue
        if not (now - 3600 <= int(nxt["airingAt"]) <= now + 7 * 86400):
            continue
        norm = normalize_anime(m)
        if not norm:
            continue
        norm["scheduled_episode"] = nxt.get("episode")
        norm["airing_at"] = nxt.get("airingAt")
        norm["time_until_airing"] = max(int(nxt["airingAt"]) - now, 0)
        items.append(norm)
    items.sort(key=lambda n: n["airing_at"])
    return jsonify({"total": len(items), "schedule": items, "stale": stale})


@app.route("/api/anime/<int:anime_id>")
def api_anime_detail(anime_id: int):
    """Poori detail + characters + recommendations."""
    key = f"detail|{anime_id}"
    media = get_cached(key)
    stale = False
    if media is None:
        with _stripe(key):
            media = get_cached(key)
            if media is None:
                query = """
                query ($id: Int) {
                  Media(id: $id, type: ANIME) {
                    %(d)s
                    characters(sort: ROLE, perPage: 8) { edges { role node { name { full } image { large } } } }
                    recommendations(sort: RATING_DESC, perPage: 8) { nodes { mediaRecommendation { %(l)s } } }
                  }
                }
                """ % {"d": DETAIL_FRAGMENT, "l": LIST_FRAGMENT}
                raw = anilist_request(query, {"id": anime_id})
                m = _d(raw.get("data")).get("Media") if isinstance(raw.get("data"), dict) else None
                if isinstance(m, dict) and m.get("id"):
                    media = m
                    set_cached(key, media, Config.DETAIL_TTL_SECONDS)
                elif raw.get("_status") == 404 or "not found" in str(raw.get("errors")).lower():
                    return jsonify({"error": "Anime not found"}), 404
                else:
                    media = get_cached(key, allow_stale=True)
                    stale = media is not None
    if media is None:
        return api_unavailable()
    if media.get("isAdult"):
        return jsonify({"error": "Anime not found"}), 404

    info = normalize_anime(media, detail=True)
    characters = []
    for edge in _l(_d(media.get("characters")).get("edges")):
        node = _d(_d(edge).get("node"))
        characters.append({
            "name": _d(node.get("name")).get("full") or "Character",
            "role": _d(edge).get("role") or "MAIN",
            "image": _d(node.get("image")).get("large") or "",
        })
    info["characters"] = characters
    recs = []
    for r in _l(_d(media.get("recommendations")).get("nodes")):
        rm = _d(r).get("mediaRecommendation")
        if isinstance(rm, dict) and not rm.get("isAdult") and rm.get("id") != anime_id:
            n = normalize_anime(rm)
            if n:
                recs.append(n)
    info["recommendations"] = recs

    link = _d(STORE.get(anime_id))
    info["ep_count"] = max(1, min(int(info.get("latest_ep") or info.get("episodes") or 1), MAX_EPISODES))
    info["episode_links"] = dict(_d(link.get("episodes")))
    info["buttons"] = {
        "watch_download_url": link.get("telegram_url") or Config.MAIN_CHANNEL_URL,
        "anime_news_url": Config.NEWS_CHANNEL_URL,
        "all_backup_url": Config.BACKUP_CHANNEL_URL,
    }
    info["stale"] = stale
    return jsonify(info)


@app.route("/api/stream/<int:anime_id>/<int:ep_num>")
def api_episode_stream(anime_id: int, ep_num: int):
    """Embed servers + Telegram Watch/Download links (us episode ke liye)."""
    slug = re.sub(r"[^a-z0-9-]", "", (request.args.get("slug") or "anime").lower())[:80] or "anime"
    ep_num = max(1, min(ep_num, MAX_EPISODES))
    link = _d(STORE.get(anime_id))
    custom_ep_url = _d(link.get("episodes")).get(str(ep_num))
    tg_link = custom_ep_url or link.get("telegram_url") or Config.MAIN_CHANNEL_URL

    servers = []
    if custom_ep_url and "t.me/" not in custom_ep_url and custom_ep_url.lower().startswith("https://") and Config.PLAYER_ENABLED:
        servers.append({"id": "custom_vip", "name": f"⚡ {Config.APP_NAME} VIP Server", "embed_url": custom_ep_url})
    for srv in Config.STREAM_SERVERS:
        servers.append({"id": srv["id"], "name": srv["name"],
                        "embed_url": srv["embed_template"].format(slug=slug, anilist_id=anime_id, ep=ep_num)})
    return jsonify({
        "anime_id": anime_id, "episode": ep_num, "audio": Config.DEFAULT_AUDIO_BADGE,
        "qualities": Config.DEFAULT_QUALITIES, "servers": servers,
        "telegram_watch_url": tg_link, "news_url": Config.NEWS_CHANNEL_URL,
        "backup_url": Config.BACKUP_CHANNEL_URL, "credit": Config.CREDIT_TEXT,
    })


@app.route("/api/random")
def api_random_anime():
    """Dice button: kisi bhi popular anime ki ID (sirf ID - detail UI khud kholta hai)."""
    d, _stale = _get_home_raw()
    if d is None:
        return api_unavailable()
    pool = [n for n in _norm_list(_page_media(d, "hindiDubs")) if n["is_hindi_dub"]]
    pool += _norm_list(_page_media(d, "trending")) + _norm_list(_page_media(d, "popular"))
    skip = request.args.get("not", "")
    ids = [n["id"] for n in pool if not (skip.isdigit() and n["id"] == int(skip))]
    if not ids:
        return api_unavailable()
    return jsonify({"id": random.choice(ids)})


# =============================================================================
# 7. BLOGGER WEBSITE WIDGET  (/blogger-widget.js)
# =============================================================================
@app.route("/blogger-widget.js")
def blogger_embed_script():
    """
    Blogger me 1 line:   <script src="https://YOUR-APP.onrender.com/blogger-widget.js"></script>
    (chaho to  <div id="anime-w-app"></div>  daalo - widget usi ke andar dikhega; height ke liye data-height="800px")
    """
    base_url = Config.WEB_APP_URL or request.host_url.rstrip("/")
    src = json.dumps(base_url + "/?embed=blogger")
    bg = json.dumps(Config.THEME_BG_COLOR)
    name = json.dumps(Config.APP_NAME)
    js_code = """(function () {
  try {
    var me = document.currentScript;
    var height = (me && me.getAttribute('data-height')) || '100vh';
    var box = document.getElementById('anime-w-app') || document.getElementById('flyton-anime-app') || document.body;
    var f = document.createElement('iframe');
    f.src = %s;
    f.title = %s;
    f.style.width = '100%%';
    f.style.height = height;
    f.style.minHeight = '600px';
    f.style.border = 'none';
    f.style.borderRadius = '14px';
    f.style.backgroundColor = %s;
    f.setAttribute('allow', 'autoplay; fullscreen; picture-in-picture');
    f.setAttribute('allowfullscreen', '');
    box.appendChild(f);
  } catch (e) { if (window.console) console.error('Anime widget error', e); }
})();
""" % (src, name, bg)
    resp = Response(js_code, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# =============================================================================
# 8. TELEGRAM BOT  (webhook: Render sleep me bhi bot ko message milte hi server jaag jaata hai)
# =============================================================================
BOT = {"id": None, "username": ""}
TG_STATE = {"token_ok": None, "webhook_ok": None, "menu_ok": None, "commands_ok": None, "error": "",
            "init_done": False, "updates": 0, "last_update": 0.0}
_SEEN_UPDATES = deque(maxlen=500)
_SEEN_LOCK = threading.Lock()


def tg_call(method: str, payload: dict = None, timeout: int = 15) -> dict:
    if not Config.BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN set nahi hai"}
    url = f"https://api.telegram.org/bot{Config.BOT_TOKEN}/{method}"
    req = urllib.request.Request(url, data=json.dumps(payload or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        body = {"ok": False, "description": _scrub(f"{type(e).__name__}: {e}")}
    if not isinstance(body, dict):
        body = {"ok": False, "description": "bad response"}
    if not body.get("ok"):
        log(f"[Telegram] {method} fail: {_scrub(body.get('description'))}")
    return body


def webhook_url() -> str:
    return f"{Config.WEB_APP_URL}/telegram/{Config.WEBHOOK_PATH}"


def init_telegram() -> None:
    """Start par ek baar: bot check, menu button, webhook, commands. Background thread me chalta hai."""
    st = TG_STATE
    try:
        if not Config.BOT_TOKEN:
            st["error"] = "BOT_TOKEN set nahi hai - sirf web app chalega, bot nahi"
            log("[MiniApp Bot] " + st["error"])
            return
        me = tg_call("getMe")
        for wait in (3, 6, 12):
            if me.get("ok"):
                break
            time.sleep(wait)
            me = tg_call("getMe")
        if not me.get("ok"):
            st["token_ok"] = False
            st["error"] = "BOT_TOKEN galat hai ya Telegram se connect nahi hua: " + _scrub(me.get("description"))
            log("[MiniApp Bot] " + st["error"])
            return
        st["token_ok"] = True
        BOT["id"] = _d(me.get("result")).get("id")
        BOT["username"] = _d(me.get("result")).get("username") or ""
        log(f"[MiniApp Bot] bot = @{BOT['username']}")

        if not Config.WEB_APP_URL_OK:
            st["error"] = ("WEB_APP_URL https nahi mila. Render me WEB_APP_URL=https://tumhara-app.onrender.com daalo "
                           "(normally Render khud RENDER_EXTERNAL_URL deta hai)")
            log("[MiniApp Bot] " + st["error"])
            return

        label = ("🎬 " + Config.APP_NAME)[:16]
        r = tg_call("setChatMenuButton", {"menu_button": {"type": "web_app", "text": label, "web_app": {"url": Config.WEB_APP_URL}}})
        st["menu_ok"] = bool(r.get("ok"))

        info = _d(tg_call("getWebhookInfo").get("result"))
        want = webhook_url()
        if info.get("url") != want:
            r = tg_call("setWebhook", {"url": want, "secret_token": Config.WEBHOOK_HEADER_TOKEN,
                                       "allowed_updates": ["message"], "max_connections": 8})
            st["webhook_ok"] = bool(r.get("ok"))
        else:
            st["webhook_ok"] = True

        public_cmds = [{"command": "start", "description": "Open the Mini App"},
                       {"command": "app", "description": "Open the Mini App"},
                       {"command": "id", "description": "Show my Telegram ID"}]
        tg_call("setMyCommands", {"commands": public_cmds, "scope": {"type": "default"}})
        if Config.OWNER_IDS:
            owner_cmds = public_cmds + [
                {"command": "set_link", "description": "Anime ka Telegram link set karo"},
                {"command": "set_ep", "description": "Kisi episode ka link set karo"},
                {"command": "del_link", "description": "Link hatao"},
                {"command": "links", "description": "Saare saved links"},
                {"command": "status", "description": "Bot / DB / AniList status"},
                {"command": "refresh", "description": "Cache saaf karo"},
            ]
            for oid in Config.OWNER_IDS:
                tg_call("setMyCommands", {"commands": owner_cmds, "scope": {"type": "chat", "chat_id": oid}})
        st["commands_ok"] = True
        st["error"] = ""
        log(f"[MiniApp Bot Active] menu={st['menu_ok']} webhook={st['webhook_ok']} -> {Config.WEB_APP_URL}")
    except Exception as e:  # pragma: no cover
        st["error"] = _scrub(f"{type(e).__name__}: {e}")
        log("[MiniApp Bot] init error: " + st["error"])
    finally:
        st["init_done"] = True


# ---- sending ----------------------------------------------------------------
def _strip_url_buttons(markup):
    rows = []
    for row in _l(_d(markup).get("inline_keyboard")):
        keep = [b for b in row if isinstance(b, dict) and "web_app" in b]
        if keep:
            rows.append(keep)
    return {"inline_keyboard": rows} if rows else None


def tg_send(chat_id, text: str, markup=None) -> dict:
    payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    res = tg_call("sendMessage", payload)
    if res.get("ok"):
        return res
    desc = str(res.get("description", "")).lower()
    if markup and any(w in desc for w in ("button", "url", "web_app", "reply markup")):
        safe = _strip_url_buttons(markup)  # ek galat link poora message na rok de
        retry = dict(payload)
        if safe:
            retry["reply_markup"] = safe
        else:
            retry.pop("reply_markup", None)
        res = tg_call("sendMessage", retry)
        if res.get("ok"):
            return res
        desc = str(res.get("description", "")).lower()
    if "parse" in desc or "entities" in desc:
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))[:4000]
        return tg_call("sendMessage", {"chat_id": chat_id, "text": plain, "disable_web_page_preview": True})
    return res


def welcome_text() -> str:
    return (
        f"<blockquote>⚡ <b>{esc(Config.APP_NAME.upper())} — MINI APP</b> ⚡</blockquote>\n"
        f"<blockquote>🎬 <b>{esc(Config.APP_TAGLINE)}</b>\n\n"
        f"◈ <b>Audio:</b> <code>{esc(Config.DEFAULT_AUDIO_BADGE)}</code>\n"
        f"◈ <b>Quality:</b> <code>{esc(', '.join(Config.DEFAULT_QUALITIES))}</code>\n"
        f"◈ <b>Official Channel:</b> {esc(Config.MAIN_CHANNEL_USERNAME)}</blockquote>\n"
        f"<blockquote>✨ <b>{esc(Config.CREDIT_TEXT)}</b></blockquote>"
    )


def welcome_markup(private: bool) -> dict:
    rows = []
    label = f"🚀 OPEN {Config.APP_NAME.upper()} MINI APP 🚀"[:60]
    if Config.WEB_APP_URL_OK and private:
        rows.append([{"text": label, "web_app": {"url": Config.WEB_APP_URL}}])  # web_app button sirf private chat me chalta hai
    elif BOT["username"]:
        rows.append([{"text": label, "url": f"https://t.me/{BOT['username']}?startapp"}])
    sub = []
    if Config.NEWS_CHANNEL_URL:
        sub.append({"text": "• ANIME NEWS •", "url": Config.NEWS_CHANNEL_URL})
    if Config.BACKUP_CHANNEL_URL:
        sub.append({"text": "• ALL BACKUP •", "url": Config.BACKUP_CHANNEL_URL})
    if sub:
        rows.append(sub)
    return {"inline_keyboard": rows}


# ---- command parsing --------------------------------------------------------
_CMD_RE = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]{3,64}))?(?:\s+(.*))?$", re.S)
_ANILIST_URL_RE = re.compile(r"anilist\.co/anime/(\d{1,9})", re.I)


def parse_command(text: str):
    m = _CMD_RE.match((text or "").strip())
    if not m:
        return None, ""
    cmd, target, rest = m.group(1).lower(), m.group(2), (m.group(3) or "").strip()
    if target and BOT["username"] and target.lower() != BOT["username"].lower():
        return None, ""  # kisi aur bot ke liye hai
    return cmd, rest


def parse_anime_id(token: str):
    token = (token or "").strip()
    m = _ANILIST_URL_RE.search(token)
    if m:
        return int(m.group(1))
    if token.isdigit() and 0 < int(token) < 10 ** 9:
        return int(token)
    return None


def clean_link(raw: str) -> str:
    """'@channel', 't.me/x', 'https://...' -> saaf https link; galat ho to ''."""
    return tg_url(raw, "")


def is_owner(user_id) -> bool:
    return bool(Config.OWNER_IDS) and user_id in Config.OWNER_IDS


def lookup_title(aid: int) -> str:
    """Confirmation message ke liye anime ka naam (fail ho to khali)."""
    media = get_cached(f"detail|{aid}", allow_stale=True)
    if not isinstance(media, dict):
        key = f"title|{aid}"
        t = get_cached(key)
        if t is None:
            raw = anilist_request("query ($id: Int) { Media(id: $id, type: ANIME) { id title { romaji english } } }", {"id": aid})
            m = _d(_d(raw.get("data")).get("Media"))
            titles = _d(m.get("title"))
            t = titles.get("english") or titles.get("romaji") or ""
            if t:
                set_cached(key, t, 86400)
        return t or ""
    titles = _d(media.get("title"))
    return titles.get("english") or titles.get("romaji") or ""


def _db_line(res: dict) -> str:
    if not STORE.persistent:
        return ("⚠️ <b>MongoDB set nahi hai</b> - ye link Render restart/sleep ke baad ud jayega. "
                "Render → Environment me <code>MONGO_URI</code> daalo.")
    if res.get("persisted", 0) >= 1:
        return f"💾 Database me save ✅ ({res['persisted']}/{res['total']})"
    return "⚠️ Database me save nahi ho paya (abhi sirf memory me hai). <code>/status</code> check karo."


def _db_down_reply() -> str:
    return ("⚠️ Database abhi connect nahi hai, isliye save nahi kiya (warna purana data overwrite ho sakta tha). "
            "1-2 minute baad dobara try karo ya <code>/status</code> dekho.")


USAGE_SET_LINK = ("<b>Use:</b> <code>/set_link [AniList ID ya AniList link] [Telegram link]</code>\n"
                  "Example: <code>/set_link 151807 https://t.me/FlyTon_Anime/123</code>\n"
                  "ID kaise mile: anilist.co par anime kholo → link me jo number hai (anilist.co/anime/<b>151807</b>)")


def cmd_set_link(chat_id, rest: str) -> None:
    parts = rest.split(None, 1)
    aid = parse_anime_id(parts[0]) if parts else None
    url = clean_link(parts[1]) if len(parts) > 1 else ""
    if not aid or not url:
        tg_send(chat_id, "⚠️ " + USAGE_SET_LINK)
        return
    title = lookup_title(aid)
    res = STORE.set_link(aid, url, title)
    if res.get("error"):
        tg_send(chat_id, _db_down_reply())
        return
    name = f"<b>{esc(title)}</b> " if title else ""
    tg_send(chat_id, f"✅ <b>Link save ho gaya</b>\n🎬 {name}(ID <code>{aid}</code>)\n🔗 <code>{esc(url)}</code>\n{_db_line(res)}")


def cmd_set_ep(chat_id, rest: str) -> None:
    parts = rest.split(None, 2)
    aid = parse_anime_id(parts[0]) if parts else None
    ep = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() and 0 < int(parts[1]) <= MAX_EPISODES else None
    url = clean_link(parts[2]) if len(parts) > 2 else ""
    if not aid or not ep or not url:
        tg_send(chat_id, "⚠️ <b>Use:</b> <code>/set_ep [AniList ID] [Episode no.] [Telegram link]</code>\n"
                         "Example: <code>/set_ep 151807 5 https://t.me/FlyTon_Anime/456</code>")
        return
    res = STORE.set_episode(aid, ep, url, lookup_title(aid))
    if res.get("error"):
        tg_send(chat_id, _db_down_reply())
        return
    tg_send(chat_id, f"✅ <b>Episode link save ho gaya</b>\n🎬 ID <code>{aid}</code> • Episode <b>{ep}</b>\n"
                     f"🔗 <code>{esc(url)}</code>\n{_db_line(res)}")


def cmd_del_link(chat_id, rest: str) -> None:
    parts = rest.split()
    aid = parse_anime_id(parts[0]) if parts else None
    ep = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    if not aid:
        tg_send(chat_id, "⚠️ <b>Use:</b> <code>/del_link [AniList ID]</code>  (poora link)\n"
                         "ya <code>/del_link [AniList ID] [Episode no.]</code>  (sirf us episode ka)")
        return
    res = STORE.delete(aid, ep)
    if res.get("error"):
        tg_send(chat_id, _db_down_reply())
        return
    if not res.get("removed"):
        tg_send(chat_id, f"ℹ️ ID <code>{aid}</code>{' episode ' + str(ep) if ep else ''} ka koi saved link nahi mila.")
        return
    tg_send(chat_id, f"🗑 <b>Hata diya</b> (ID <code>{aid}</code>{', episode ' + str(ep) if ep else ''})\n{_db_line(res)}")


def cmd_links(chat_id) -> None:
    links = STORE.links
    if not links:
        tg_send(chat_id, "📭 Abhi koi custom link save nahi hai.\n" + USAGE_SET_LINK)
        return
    lines = []
    for k in sorted(links, key=lambda x: int(x)):
        rec = links[k]
        eps = len(_d(rec.get("episodes")))
        title = esc(rec.get("title") or "")
        lines.append(f"• <code>{k}</code> {title}{' (+' + str(eps) + ' ep)' if eps else ''}\n   {esc(rec.get('telegram_url') or '-')}")
    chunk, size = [], 0
    header = f"📚 <b>Saved links: {len(links)}</b>\n\n"
    for line in lines:
        if size + len(line) > 3300:
            tg_send(chat_id, header + "\n".join(chunk))
            chunk, size, header = [], 0, ""
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        tg_send(chat_id, header + "\n".join(chunk))


def build_status_text() -> str:
    st, ls = TG_STATE, STORE.status()
    yes, no, warn = "✅", "❌", "⚠️"
    out = [f"📊 <b>{esc(Config.APP_NAME)} — Status</b>", ""]
    out.append(f"{yes if Config.WEB_APP_URL_OK else no} <b>Web URL:</b> <code>{esc(Config.WEB_APP_URL or 'NOT SET')}</code>")
    out.append(f"{yes if st['token_ok'] else no} <b>Bot:</b> {('@' + esc(BOT['username'])) if BOT['username'] else 'connect nahi hua'}")
    info = _d(tg_call("getWebhookInfo").get("result"))
    wh_ok = info.get("url") == webhook_url()
    out.append(f"{yes if wh_ok else no} <b>Webhook:</b> {'set' if wh_ok else 'SET NAHI'}"
               f" • pending: {info.get('pending_update_count', 0)}")
    if info.get("last_error_message"):
        out.append(f"   {warn} last error: <code>{esc(str(info['last_error_message'])[:120])}</code>")
    out.append(f"{yes if st['menu_ok'] else no} <b>Menu button:</b> {'linked' if st['menu_ok'] else 'set nahi hua'}")
    out.append(f"{yes if Config.OWNER_IDS else no} <b>Owner ID:</b> {', '.join(map(str, Config.OWNER_IDS)) or 'NOT SET'}")
    if ls["persistent"]:
        for n in ls["nodes"]:
            out.append(f"{yes if n['ok'] else no} <b>MongoDB {esc(n['label'])}:</b> {'connected' if n['ok'] else esc(n['err'] or 'connect nahi hua')}")
        out.append(f"   data: ver {ls['ver']} • loaded from {esc(ls['loaded_from'])}")
    else:
        out.append(f"{warn} <b>MongoDB:</b> set nahi - links restart par ud jayenge!  (<code>MONGO_URI</code> daalo)")
    out.append(f"🔗 <b>Custom links:</b> {ls['count']} (+{ls['episode_links']} episode links)")
    now = time.time()
    if ANILIST_STATE["blocked_until"] > now:
        out.append(f"{warn} <b>AniList:</b> rate-limited, {int(ANILIST_STATE['blocked_until'] - now)}s me wapas")
    else:
        out.append(f"{yes if not ANILIST_STATE['last_error'] else warn} <b>AniList:</b> "
                   f"{'ok' if not ANILIST_STATE['last_error'] else esc(ANILIST_STATE['last_error'][:100])} • calls: {ANILIST_STATE['calls']}")
    out.append(f"🗂 <b>Cache:</b> {len(API_CACHE)} entries • ⏱ uptime {int((now - START_TIME) // 60)} min")
    if st["error"]:
        out.append(f"\n{warn} <b>Problem:</b> <code>{esc(st['error'][:300])}</code>")
    return "\n".join(out)


OWNER_COMMANDS = {"set_link", "set_ep", "del_link", "links", "status", "refresh"}


def handle_update(upd: dict) -> None:
    uid = upd.get("update_id")
    if uid is not None:
        with _SEEN_LOCK:
            if uid in _SEEN_UPDATES:
                return  # Telegram ne wahi update dobara bheja
            _SEEN_UPDATES.append(uid)
    msg = upd.get("message")
    if not isinstance(msg, dict):
        return
    chat = _d(msg.get("chat"))
    chat_id, ctype = chat.get("id"), chat.get("type")
    user_id = _d(msg.get("from")).get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text.startswith("/"):
        return
    cmd, rest = parse_command(text)
    if not cmd:
        return
    TG_STATE["updates"] += 1
    TG_STATE["last_update"] = time.time()
    private = ctype == "private"
    log(f"[Bot] /{cmd} from {user_id} ({ctype})")

    if cmd in ("start", "app", "help", "open"):
        tg_send(chat_id, welcome_text(), welcome_markup(private))
    elif cmd == "id":
        tg_send(chat_id, f"🆔 <b>Tumhara Telegram ID:</b> <code>{user_id}</code>\n💬 <b>Chat ID:</b> <code>{chat_id}</code>\n\n"
                         f"Ye number Render me <code>OWNER_ID</code> me daalna.")
    elif cmd in OWNER_COMMANDS:
        if not is_owner(user_id):
            if private:
                if not Config.OWNER_IDS:
                    tg_send(chat_id, "⚠️ OWNER_ID abhi set nahi hai. Render → Environment me <code>OWNER_ID</code> daalo "
                                     "(apna ID <code>/id</code> se pata chalega), tab ye commands chalenge.")
                else:
                    tg_send(chat_id, "⛔ Ye command sirf owner ke liye hai.")
            return
        if cmd == "set_link":
            cmd_set_link(chat_id, rest)
        elif cmd == "set_ep":
            cmd_set_ep(chat_id, rest)
        elif cmd == "del_link":
            cmd_del_link(chat_id, rest)
        elif cmd == "links":
            cmd_links(chat_id)
        elif cmd == "status":
            tg_send(chat_id, build_status_text())
        elif cmd == "refresh":
            with _CACHE_LOCK:
                API_CACHE.clear()
            ANILIST_STATE["blocked_until"] = 0.0
            tg_send(chat_id, "🔄 Cache saaf ho gaya. Agli baar anime data fresh aayega.")


def _safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


@app.route("/telegram/<token_path>", methods=["POST"])
def telegram_webhook(token_path: str):
    if not Config.BOT_TOKEN or not _safe_eq(token_path, Config.WEBHOOK_PATH):
        return Response("not found", 404, mimetype="text/plain")
    if not _safe_eq(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), Config.WEBHOOK_HEADER_TOKEN):
        return Response("forbidden", 403, mimetype="text/plain")
    upd = request.get_json(silent=True)
    if isinstance(upd, dict):
        try:
            handle_update(upd)
        except Exception:
            log("[Bot] handler error: " + _scrub(traceback.format_exc()))
    return jsonify({"ok": True})  # hamesha 200: warna Telegram usi update ko baar-baar bhejta hai


# =============================================================================
# 9. START
# =============================================================================
_BOOT = {"done": False}
_BOOT_LOCK = threading.Lock()


def bootstrap() -> None:
    """Ek baar: links load, DB-heal thread, Telegram setup thread."""
    with _BOOT_LOCK:
        if _BOOT["done"]:
            return
        _BOOT["done"] = True
    log(f"🚀 {Config.APP_NAME} start ho raha hai | web={Config.WEB_APP_URL or 'NOT SET'} | "
        f"bot={'yes' if Config.BOT_TOKEN else 'NO TOKEN'} | owners={len(Config.OWNER_IDS)} | mongo={len(Config.MONGO_URIS)}")
    if not Config.MONGO_URIS:
        log("⚠️  MONGO_URI set nahi hai: /set_link wale links Render restart/sleep ke baad ud jayenge. Render → Environment me MONGO_URI daalo.")
    try:
        STORE.load()
    except Exception as e:  # pragma: no cover
        log(f"[Links] load error: {_scrub(e)}")
    if STORE.persistent:
        threading.Thread(target=STORE.heal_loop, name="links-heal", daemon=True).start()
    threading.Thread(target=init_telegram, name="tg-init", daemon=True).start()


if __name__ != "__main__" and not os.environ.get("ANIMEW_NO_BOOT"):
    bootstrap()  # gunicorn app:app se chalane par

if __name__ == "__main__":
    bootstrap()
    log(f"Server {Config.HOST}:{Config.PORT} par chal raha hai (Flask 'development server' warning normal hai, error nahi)")
    app.run(host=Config.HOST, port=Config.PORT, debug=False, threaded=True, use_reloader=False)
