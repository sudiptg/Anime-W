# -*- coding: utf-8 -*-
"""
Anime-W  (Telegram Mini App)  -  settings
=========================================
You can change everything from the Render "Environment" tab using environment variables. If you enter
something wrong or empty, the app will not crash - it falls back to a safe default (the old code crashed
completely on int("abc")).

Required environment variables (only 2):
    BOT_TOKEN   = token of the NEW bot for this Mini App (from BotFather)   <- do NOT use the Auto-Download-Bot token
    OWNER_ID    = your numeric Telegram ID  (send /id to the bot and it will tell you)

Recommended (otherwise the links saved with /set_link are lost on restart):
    MONGO_URI   = mongodb+srv://...   (the same URI you use in Auto-Download-Bot works)

WEB_APP_URL is no longer required - Render provides RENDER_EXTERNAL_URL and the app picks it up.
"""
import hashlib
import json
import os
import re
import string
from urllib.parse import urlparse

try:  # optional: local .env file (not needed on Render)
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


# --------------------------------------------------------------------------- helpers
def _clean(value) -> str:
    return (value or "").strip().strip('"').strip("'").strip()


def _env(name: str, default: str = "") -> str:
    v = _clean(os.environ.get(name))
    return v if v else default


def _env_int(name: str, default: int, lo: int = None, hi: int = None) -> int:
    raw = _clean(os.environ.get(name))
    if not raw:
        return default
    try:
        val = int(float(raw))
    except ValueError:
        print(f"[Config Warning] {name}={raw!r} is not a number, using default {default}", flush=True)
        return default
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


def _env_bool(name: str, default: bool) -> bool:
    raw = _clean(os.environ.get(name)).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


def _id_list(raw: str) -> list:
    """'123, 456 789' -> [123, 456, 789]  (invalid entries are skipped)."""
    out = []
    for tok in re.split(r"[,\s;]+", raw or ""):
        tok = tok.strip()
        if re.fullmatch(r"-?\d{3,}", tok):
            n = int(tok)
            if n not in out:
                out.append(n)
    return out


def tg_url(value: str, default: str = "") -> str:
    """
    Turns any form into a clean https link:
        @FlyTon_Anime -> https://t.me/FlyTon_Anime      t.me/x -> https://t.me/x      FlyTon_Anime -> https://t.me/FlyTon_Anime
    Falls back to `default` if the value is invalid. (Before, entering '@name' made Telegram reject the whole /start message.)
    """
    v = _clean(value)
    if not v:
        return default
    if v.startswith("@"):
        v = "https://t.me/" + v[1:]
    elif re.fullmatch(r"[A-Za-z0-9_]{4,32}", v):
        v = "https://t.me/" + v
    # t.me / telegram.me / www.t.me  ->  always https://t.me/...
    v = re.sub(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/", "https://t.me/", v, flags=re.I)
    if not re.match(r"^https?://", v, re.I):
        return default
    parsed = urlparse(v)
    if not parsed.netloc or re.search(r"[\s<>\"'`\\\x00-\x1f]", v):
        return default  # spaces / HTML / quotes in a link would break the button or the page
    return v


def tg_username(value: str, default: str = "") -> str:
    v = _clean(value)
    v = re.sub(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/", "", v, flags=re.I)
    v = v.lstrip("@").split("/")[0].split("?")[0]
    if re.fullmatch(r"[A-Za-z0-9_]{3,64}", v):
        return "@" + v
    return default


def _color(name: str, default: str) -> str:
    v = _env(name, default)
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", v) or re.fullmatch(r"rgba?\(\s*[\d\s.,%]+\)", v):
        return v
    print(f"[Config Warning] {name}={v!r} is not a valid colour, using the default", flush=True)
    return default


def _web_url() -> str:
    """WEB_APP_URL (or Render's RENDER_EXTERNAL_URL) -> 'https://xxx.onrender.com' (no trailing /, no path)."""
    raw = _env("WEB_APP_URL") or _env("RENDER_EXTERNAL_URL")
    if not raw and _env("RENDER_EXTERNAL_HOSTNAME"):
        raw = "https://" + _env("RENDER_EXTERNAL_HOSTNAME")
    if not raw or "your-app-name" in raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    p = urlparse(raw)
    if not p.netloc:
        return ""
    scheme = "https"
    host = p.netloc
    if re.match(r"^(localhost|127\.|0\.0\.0\.0)", host):
        scheme = p.scheme  # local testing
    return f"{scheme}://{host}"


def _mongo_uris() -> list:
    raw = []
    for name in ("MONGO_URI", "MONGODB_URI", "MONGO_URL") + tuple(f"MONGO_URI_{i}" for i in range(1, 10)):
        raw.append(os.environ.get(name, ""))
    raw.extend((os.environ.get("MONGO_URIS", "") or "").replace(",", " ").split())
    out = []
    for item in raw:
        item = _clean(item)
        if item.lower().startswith(("mongodb://", "mongodb+srv://")) and item not in out:
            out.append(item)
    return out


# --------------------------------------------------------------------------- languages
# Audio / subtitle languages the player can offer. The app never invents a language: an option is only
# switched on when a server (or an owner Telegram link) for it really exists.
LANGUAGES = [
    ("ja", "Japanese"),
    ("en", "English"),
    ("hi", "Hindi"),
    ("ta", "Tamil"),
    ("te", "Telugu"),
]
LANG_CODES = {code for code, _ in LANGUAGES}
LANG_LABELS = dict(LANGUAGES)
_LANG_ALIASES = {
    "japanese": "ja", "jp": "ja", "jpn": "ja",
    "english": "en", "eng": "en",
    "hindi": "hi", "hin": "hi",
    "tamil": "ta", "tam": "ta",
    "telugu": "te", "tel": "te",
}


def norm_lang(value, default: str = "") -> str:
    """'Hindi' / 'hin' / 'hi' -> 'hi'.  Unknown values return `default`."""
    v = _clean(str(value if value is not None else "")).lower()
    if v in LANG_CODES:
        return v
    return _LANG_ALIASES.get(v, default)


_ALLOWED_TEMPLATE_FIELDS = {"slug", "anilist_id", "ep"}
_SERVER_KINDS = ("embed", "hls", "mp4")


def _check_template(tpl: str, label: str) -> str:
    names = {f for _, f, _, _ in string.Formatter().parse(tpl) if f}
    if not tpl.lower().startswith("https://") or not names <= _ALLOWED_TEMPLATE_FIELDS or re.search(r"[\s<>\"'`\\\x00-\x1f]", tpl):
        raise ValueError(f"{label}: must start with https://, only {{slug}} {{anilist_id}} {{ep}} are allowed")
    return tpl


def _parse_server(item, i: int) -> dict:
    if not isinstance(item, dict):
        raise ValueError(f"server #{i + 1}: must be an object")
    label = f"server #{i + 1}"
    kind = str(item.get("kind") or ("embed" if item.get("embed_template") else "")).strip().lower()
    if kind not in _SERVER_KINDS:
        raise ValueError(f"{label}: kind must be one of {', '.join(_SERVER_KINDS)}")
    if kind == "embed":
        tpl = _check_template(str(item.get("embed_template") or ""), label)
    else:
        tpl = _check_template(str(item.get("src_template") or ""), label)
    sid = re.sub(r"[^a-z0-9_-]", "", str(item.get("id", f"srv{i + 1}")).lower()) or f"srv{i + 1}"
    name = str(item.get("name", f"Server {i + 1}"))[:40]

    # Language of the AUDIO. Older settings had no "lang": a name/id with "dub" means English, otherwise Japanese.
    raw_lang = str(item.get("lang") or "").strip().lower()
    lang = "multi" if raw_lang == "multi" else norm_lang(raw_lang, "")
    typ = str(item.get("type") or "").strip().lower()
    if typ not in ("sub", "dub"):
        typ = "dub" if "dub" in (sid + " " + name).lower() else "sub"
    if not lang:
        lang = "en" if typ == "dub" else "ja"

    out = {
        "id": sid,
        "name": name,
        "kind": kind,
        "lang": lang,                                     # audio language ('multi' = other / unknown)
        "type": typ,                                      # 'sub' or 'dub'
        "sub_lang": norm_lang(item.get("sub_lang"), ""),   # subtitle language that is built into the video (if known)
        "sandbox": _bool_value(item.get("sandbox"), True),  # embed only: block pop-ups / new tabs / redirects
        "subs": [],
    }
    out["embed_template" if kind == "embed" else "src_template"] = tpl
    if kind != "embed":
        for j, sub in enumerate(item.get("subs") or []):
            if not isinstance(sub, dict):
                raise ValueError(f"{label}: subs #{j + 1} must be an object")
            slang = norm_lang(sub.get("lang"), "")
            if not slang:
                raise ValueError(f"{label}: subs #{j + 1} needs lang = ja / en / hi / ta / te")
            out["subs"].append({
                "lang": slang,
                "label": str(sub.get("label") or LANG_LABELS[slang])[:30],
                "src_template": _check_template(str(sub.get("src_template") or ""), f"{label} subs #{j + 1}"),
            })
    return out


def _bool_value(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _load_stream_servers(default: list) -> list:
    if not _env_bool("PLAYER_ENABLED", True):
        return []
    raw = _env("STREAM_SERVERS_JSON")
    if not raw:
        return [_parse_server(item, i) for i, item in enumerate(default)]
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("it must be a JSON list")
        return [_parse_server(item, i) for i, item in enumerate(data)]
    except Exception as err:
        print(f"[Config Warning] STREAM_SERVERS_JSON is invalid ({err}) - using the default servers", flush=True)
        return [_parse_server(item, i) for i, item in enumerate(default)]


def _https_url(name: str, default: str) -> str:
    v = _env(name, default)
    if v.lower().startswith("https://") and not re.search(r"[\s<>\"'`\\\x00-\x1f]", v):
        return v
    print(f"[Config Warning] {name}={v!r} must be an https:// link, using the default", flush=True)
    return default


class Config:
    # =========================================================================
    # 1. BRANDING & CREDIT
    # =========================================================================
    APP_NAME = _env("APP_NAME", "FlyTon Anime")
    APP_TAGLINE = _env("APP_TAGLINE", "Watch & Download HD Anime in Hindi Dub & Sub")
    APP_LOGO_URL = _env("APP_LOGO_URL", "https://cdn-icons-png.flaticon.com/512/2503/2503508.png")

    MAIN_CHANNEL_USERNAME = tg_username(os.environ.get("MAIN_CHANNEL_USERNAME", ""), "@FlyTon_Anime")
    MAIN_CHANNEL_URL = tg_url(os.environ.get("MAIN_CHANNEL_URL", ""), "https://t.me/" + MAIN_CHANNEL_USERNAME.lstrip("@"))
    NEWS_CHANNEL_URL = tg_url(os.environ.get("NEWS_CHANNEL_URL", ""), MAIN_CHANNEL_URL)
    BACKUP_CHANNEL_URL = tg_url(os.environ.get("BACKUP_CHANNEL_URL", ""), MAIN_CHANNEL_URL)

    CREDIT_TEXT = _env("CREDIT_TEXT", "Powered by FlyTon Anime Official")
    DEVELOPER_HANDLE = _env("DEVELOPER_HANDLE", MAIN_CHANNEL_USERNAME)

    # =========================================================================
    # 2. TELEGRAM BOT & MINI APP
    # =========================================================================
    BOT_TOKEN = _env("BOT_TOKEN")
    OWNER_IDS = _id_list(os.environ.get("OWNER_ID", "") + " " + os.environ.get("OWNER_IDS", ""))
    OWNER_ID = OWNER_IDS[0] if OWNER_IDS else 0  # kept for the old code

    # Your live Render URL. Leave it empty - RENDER_EXTERNAL_URL is used automatically.
    WEB_APP_URL = _web_url()
    WEB_APP_URL_OK = WEB_APP_URL.startswith("https://") and "." in WEB_APP_URL

    # Webhook secrets (derived from BOT_TOKEN, nothing to store anywhere)
    _seed = ("animew|" + BOT_TOKEN).encode("utf-8")
    WEBHOOK_PATH = hashlib.sha256(_seed).hexdigest()[:32]
    WEBHOOK_HEADER_TOKEN = hashlib.sha256(b"hdr|" + _seed).hexdigest()[:48]

    # =========================================================================
    # 3. WEB SERVER & BLOGGER
    # =========================================================================
    HOST = _env("HOST", "0.0.0.0")
    PORT = _env_int("PORT", 8080, 1, 65535)
    BLOGGER_WEBSITE_URL = _env("BLOGGER_WEBSITE_URL")
    CORS_ALLOWED_ORIGINS = _env("CORS_ALLOWED_ORIGINS", "*")

    # =========================================================================
    # 4. DATABASE (so links survive a restart)
    # =========================================================================
    MONGO_URIS = _mongo_uris()
    MONGO_DB_NAME = _env("MONGO_DB_NAME", "flyton_miniapp")
    MONGO_COLLECTION = _env("MONGO_COLLECTION", "miniapp_links")
    LINKS_FILE = _env("LINKS_FILE", "custom_anime_links.json")

    # =========================================================================
    # 5. THEME
    # =========================================================================
    # Calm violet instead of the old loud red. Any of these can still be changed from Render -> Environment.
    THEME_PRIMARY_COLOR = _color("THEME_PRIMARY_COLOR", "#8b7bff")
    THEME_SECONDARY_COLOR = _color("THEME_SECONDARY_COLOR", "#5b4bdb")
    THEME_BG_COLOR = _color("THEME_BG_COLOR", "#08090f")
    THEME_GLASS_CARD = _color("THEME_GLASS_CARD", "#12141e")

    # Small Telegram icon at the top of an anime page (it opens that anime's Telegram links). It only appears
    # for anime that have a link saved with /set_link or /set_ep. SHOW_TG_LINKS=0 hides it completely.
    SHOW_TG_LINKS = _env_bool("SHOW_TG_LINKS", True)

    # =========================================================================
    # 6. ANIME DATA (AniList) & PLAYER
    # =========================================================================
    ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
    JIKAN_API_URL = "https://api.jikan.moe/v4"
    ANISKIP_API_URL = "https://api.aniskip.com/v2"
    ANILIST_TIMEOUT = _env_int("ANILIST_TIMEOUT", 12, 4, 40)
    # AniList's limit is 90/min (sometimes 30/min). We never make more than 25 calls/min ourselves.
    ANILIST_MAX_PER_MIN = _env_int("ANILIST_MAX_PER_MIN", 25, 5, 85)

    DEFAULT_AUDIO_BADGE = _env("DEFAULT_AUDIO_BADGE", "Hindi [Dual Audio]")
    DEFAULT_QUALITIES = ["480p", "720p", "1080p"]

    # --- Video servers -------------------------------------------------------------------------------------------
    # Two kinds of server:
    #   "embed" = a third-party player page shown in an iframe. We do not control it (ads, speed, languages and
    #             subtitles are decided by that player). The app blocks its pop-ups / new tabs with an iframe sandbox.
    #   "hls" / "mp4" = a direct video file played by the app's OWN player: no ads at all, real audio / subtitle /
    #             quality menus. Use it only for video you have the right to stream.
    #
    # Put your own list in the STREAM_SERVERS_JSON environment variable (Render -> Environment). Example:
    #   [
    #     {"id": "hi-main", "name": "Hindi HD", "kind": "hls", "lang": "hi", "type": "dub",
    #      "src_template": "https://cdn.example.com/{anilist_id}/hi/{ep}/index.m3u8",
    #      "subs": [{"lang": "en", "label": "English", "src_template": "https://cdn.example.com/{anilist_id}/en/{ep}.vtt"}]},
    #     {"id": "ja-embed", "name": "Server 1 (Sub)", "kind": "embed", "lang": "ja", "type": "sub",
    #      "embed_template": "https://player.example.com/{anilist_id}/{ep}"}
    #   ]
    # Fields: lang = ja | en | hi | ta | te | multi     type = sub | dub     sandbox = true | false (embed only)
    # Allowed placeholders: {anilist_id} {ep} {slug}.   To turn the player off completely set PLAYER_ENABLED=0.
    #
    # MegaPlay's format (from their docs, https://megaplay.buzz/api):  /stream/ani/{anilist_id}/{episode}/{sub|dub}
    _DEFAULT_STREAM_SERVERS = [
        {"id": "megaplay-sub", "name": "Server 1 (Sub)", "kind": "embed", "lang": "ja", "type": "sub",
         "embed_template": "https://megaplay.buzz/stream/ani/{anilist_id}/{ep}/sub"},
        {"id": "megaplay-dub", "name": "Server 2 (Dub)", "kind": "embed", "lang": "en", "type": "dub",
         "embed_template": "https://megaplay.buzz/stream/ani/{anilist_id}/{ep}/dub"},
        {"id": "vidsrc", "name": "Server 3 (Backup)", "kind": "embed", "lang": "multi",
         "embed_template": "https://vidsrc.icu/embed/anime/{anilist_id}/{ep}/0"},
        {"id": "vidstreaming", "name": "Server 4 (Backup 2)", "kind": "embed", "lang": "multi",
         "embed_template": "https://2anime.xyz/embed/{slug}-episode-{ep}"},
    ]
    STREAM_SERVERS = _load_stream_servers(_DEFAULT_STREAM_SERVERS)
    PLAYER_ENABLED = bool(STREAM_SERVERS)

    # Which audio language is selected first on an anime page: auto | ja | en | hi | ta | te
    #   auto = the first language that has a playable server, in this order: hi, ta, te, en, ja
    _dl = _env("DEFAULT_LANG", "auto").lower()
    DEFAULT_LANG = _dl if _dl == "auto" or _dl in LANG_CODES else (norm_lang(_dl, "") or "auto")

    # hls.js is only downloaded when a direct HLS (.m3u8) server is played.
    HLS_JS_URL = _https_url("HLS_JS_URL", "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js")

    # Cache (seconds): keeps Render fast and stays under AniList's rate limit
    CACHE_TTL_SECONDS = _env_int("CACHE_TTL_SECONDS", 900, 30, 86400)
    DETAIL_TTL_SECONDS = _env_int("DETAIL_TTL_SECONDS", 21600, 60, 172800)
    STALE_MAX_SECONDS = _env_int("STALE_MAX_SECONDS", 86400, 600, 604800)

    @staticmethod
    def mongo_uris() -> list:
        return list(Config.MONGO_URIS)
