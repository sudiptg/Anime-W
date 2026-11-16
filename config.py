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


_ALLOWED_TEMPLATE_FIELDS = {"slug", "anilist_id", "ep"}


def _load_stream_servers(default: list) -> list:
    if not _env_bool("PLAYER_ENABLED", True):
        return []
    raw = _env("STREAM_SERVERS_JSON")
    if not raw:
        return default
    try:
        data = json.loads(raw)
        out = []
        for i, item in enumerate(data if isinstance(data, list) else []):
            tpl = str(item.get("embed_template", ""))
            names = {f for _, f, _, _ in string.Formatter().parse(tpl) if f}
            if not tpl.lower().startswith("https://") or not names <= _ALLOWED_TEMPLATE_FIELDS:
                raise ValueError(f"server #{i + 1}: must start with https://, only {{slug}} {{anilist_id}} {{ep}} are allowed")
            out.append({
                "id": re.sub(r"[^a-z0-9_-]", "", str(item.get("id", f"srv{i + 1}")).lower()) or f"srv{i + 1}",
                "name": str(item.get("name", f"Server {i + 1}"))[:40],
                "embed_template": tpl,
            })
        return out
    except Exception as err:
        print(f"[Config Warning] STREAM_SERVERS_JSON is invalid ({err}) - using the default servers", flush=True)
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
    THEME_PRIMARY_COLOR = _color("THEME_PRIMARY_COLOR", "#ff2a5f")
    THEME_SECONDARY_COLOR = _color("THEME_SECONDARY_COLOR", "#7c3aed")
    THEME_BG_COLOR = _color("THEME_BG_COLOR", "#07080d")
    THEME_GLASS_CARD = _color("THEME_GLASS_CARD", "rgba(18, 22, 36, 0.78)")

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

    # Embed players (3rd-party servers: we do not control them, they can go down at any time).
    # To use your own servers, put a JSON list in the STREAM_SERVERS_JSON env variable; to turn the player off, set PLAYER_ENABLED=0.
    #
    # MegaPlay's correct format (from their official docs, https://megaplay.buzz/api):
    #     /stream/ani/{anilist_id}/{episode}/{sub|dub}
    # The old code used /stream/s-2/{anilist_id}/{ep}, which is the HiAnime episode-ID format, so
    # Server 2 never worked. It now works directly with the AniList ID (no need to guess a slug).
    _DEFAULT_STREAM_SERVERS = [
        {"id": "megaplay-sub", "name": "Server 1 (Sub)", "embed_template": "https://megaplay.buzz/stream/ani/{anilist_id}/{ep}/sub"},
        {"id": "megaplay-dub", "name": "Server 2 (Dub)", "embed_template": "https://megaplay.buzz/stream/ani/{anilist_id}/{ep}/dub"},
        {"id": "vidsrc", "name": "Server 3 (Backup)", "embed_template": "https://vidsrc.icu/embed/anime/{anilist_id}/{ep}/0"},
        {"id": "vidstreaming", "name": "Server 4 (Backup 2)", "embed_template": "https://2anime.xyz/embed/{slug}-episode-{ep}"},
    ]
    STREAM_SERVERS = _load_stream_servers(_DEFAULT_STREAM_SERVERS)
    PLAYER_ENABLED = bool(STREAM_SERVERS)

    # Cache (seconds): keeps Render fast and stays under AniList's rate limit
    CACHE_TTL_SECONDS = _env_int("CACHE_TTL_SECONDS", 900, 30, 86400)
    DETAIL_TTL_SECONDS = _env_int("DETAIL_TTL_SECONDS", 21600, 60, 172800)
    STALE_MAX_SECONDS = _env_int("STALE_MAX_SECONDS", 86400, 600, 604800)

    @staticmethod
    def mongo_uris() -> list:
        return list(Config.MONGO_URIS)
