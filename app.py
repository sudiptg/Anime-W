import os
import re
import json
import time
import random
import threading
import urllib.request
import urllib.parse
from flask import Flask, request, jsonify, send_from_directory, Response
from config import Config

app = Flask(__name__, static_folder=".")

# =============================================================================
# 1. IN-MEMORY CACHE & CUSTOM LINKS STORAGE
# =============================================================================
API_CACHE = {}
CUSTOM_LINKS_FILE = "custom_anime_links.json"

# Built-in Hindi Dub / Dual Audio Popular Anime IDs (AniList IDs)
HINDI_DUB_ANILIST_IDS = {
    151807,  # Solo Leveling
    101922,  # Demon Slayer: Kimetsu no Yaiba
    142329,  # Demon Slayer: Swordsmith Village Arc
    166240,  # Demon Slayer: Hashira Training Arc
    113415,  # Jujutsu Kaisen
    145064,  # Jujutsu Kaisen Season 2
    20,      # Naruto
    1735,    # Naruto Shippuden
    21,      # One Piece
    102976,  # JoJo's Bizarre Adventure: Golden Wind
    21459,   # My Hero Academia
    97940,   # Black Clover
    1535,    # Death Note
    16498,   # Attack on Titan
    127230,  # Chainsaw Man
    140960,  # Spy x Family
    131586,  # Blue Lock
    154587,  # Frieren: Beyond Journey's End
    163134,  # Re:Zero Season 3
    171018,  # Dan Da Dan
    162804,  # Kaiju No. 8
    153288,  # Mashle: Magic and Muscles
    108465,  # Mushoku Tensei: Jobless Reincarnation
    161645,  # The Apothecary Diaries
    156822,  # Blue Box
    139518,  # Classroom of the Elite Season 2
    146065,  # Classroom of the Elite Season 3
    125367,  # Tokyo Revengers
    20605,   # Tokyo Ghoul
    11061,   # Hunter x Hunter (2011)
    269,     # Bleach
    116674,  # Bleach: Thousand-Year Blood War
    20958,   # Haikyu!!
    104578,  # Vinland Saga
    98659    # Classroom of the Elite
}


def load_custom_links() -> dict:
    if os.path.exists(CUSTOM_LINKS_FILE):
        try:
            with open(CUSTOM_LINKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_custom_links(data: dict):
    try:
        with open(CUSTOM_LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Custom Links Save Warning] {e}")


CUSTOM_LINKS = load_custom_links()


def get_cached(key: str):
    entry = API_CACHE.get(key)
    if not entry:
        return None
    if (time.time() - entry["ts"]) > Config.CACHE_TTL_SECONDS:
        API_CACHE.pop(key, None)
        return None
    return entry["data"]


def set_cached(key: str, data):
    if len(API_CACHE) > 300:
        API_CACHE.clear()
    API_CACHE[key] = {"ts": time.time(), "data": data}


def slugify_title(title: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9\s-]", "", title or "").strip().lower()
    return re.sub(r"[\s-]+", "-", clean) or "anime"


# =============================================================================
# 2. CORS MIDDLEWARE (FOR BLOGGER WEBSITE & TELEGRAM WEBVIEW)
# =============================================================================
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = Config.CORS_ALLOWED_ORIGINS
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    return response


# =============================================================================
# 3. ANILIST GRAPHQL CLIENT & NORMALIZER
# =============================================================================
MEDIA_FRAGMENT = """
id
idMal
title {
  romaji
  english
  native
}
coverImage {
  extraLarge
  large
  color
}
bannerImage
format
status
episodes
duration
season
seasonYear
averageScore
popularity
genres
studios(isMain: true) {
  nodes {
    name
  }
}
nextAiringEpisode {
  episode
  airingAt
  timeUntilAiring
}
description(asHtml: false)
trailer {
  id
  site
}
"""


def anilist_request(query: str, variables: dict = None) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        Config.ANILIST_GRAPHQL_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{Config.APP_NAME}/2.0"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[AniList API Warning] {e}")
        return {}


def normalize_anime(media: dict) -> dict:
    if not media:
        return {}
    aid = int(media.get("id") or 0)
    title_obj = media.get("title") or {}
    display_title = title_obj.get("english") or title_obj.get("romaji") or title_obj.get("native") or "Unknown Anime"
    romaji_title = title_obj.get("romaji") or display_title

    cover_obj = media.get("coverImage") or {}
    poster = cover_obj.get("extraLarge") or cover_obj.get("large") or ""
    banner = media.get("bannerImage") or poster

    next_ep_obj = media.get("nextAiringEpisode")
    total_eps = media.get("episodes")
    latest_ep = (next_ep_obj["episode"] - 1) if (next_ep_obj and next_ep_obj.get("episode")) else (total_eps or 12)
    latest_ep = max(1, int(latest_ep))

    studios = [
        s["name"] for s in (media.get("studios", {}).get("nodes") or []) if s and s.get("name")
    ]

    is_hindi = (aid in HINDI_DUB_ANILIST_IDS) or (str(aid) in CUSTOM_LINKS)
    audio_badge = Config.DEFAULT_AUDIO_BADGE if is_hindi else "Japanese [Eng Sub]"

    raw_desc = media.get("description") or "No synopsis available for this anime."
    clean_desc = re.sub(r"<[^>]+>", "", raw_desc).strip()

    custom_tg_link = CUSTOM_LINKS.get(str(aid), {}).get("telegram_url") or Config.MAIN_CHANNEL_URL

    return {
        "id": aid,
        "mal_id": media.get("idMal"),
        "title": display_title,
        "romaji": romaji_title,
        "slug": slugify_title(romaji_title),
        "poster": poster,
        "banner": banner,
        "color": cover_obj.get("color") or Config.THEME_PRIMARY_COLOR,
        "format": media.get("format") or "TV",
        "status": media.get("status") or "RELEASING",
        "episodes": total_eps or latest_ep,
        "latest_ep": latest_ep,
        "duration": media.get("duration") or 24,
        "season": f"{media.get('season') or ''} {media.get('seasonYear') or ''}".strip() or "TV Series",
        "score": round((media.get("averageScore") or 80) / 10.0, 1),
        "popularity": media.get("popularity") or 0,
        "genres": (media.get("genres") or [])[:4],
        "studio": studios[0] if studios else Config.APP_NAME,
        "is_hindi_dub": is_hindi,
        "audio_badge": audio_badge,
        "qualities": Config.DEFAULT_QUALITIES,
        "synopsis": clean_desc,
        "next_airing": next_ep_obj,
        "trailer": media.get("trailer"),
        "telegram_url": custom_tg_link,
        "channel_tag": Config.MAIN_CHANNEL_USERNAME
    }


# =============================================================================
# 4. WEB APP & API ROUTES (FOR TELEGRAM MINI APP & BLOGGER)
# =============================================================================
@app.route("/")
def serve_index():
    """Serves the Ultra Pro Telegram Mini App & Web Portal UI."""
    return send_from_directory(".", "index.html")


@app.route("/health")
def health_check():
    return jsonify({
        "status": "ok",
        "app": Config.APP_NAME,
        "credit": Config.CREDIT_TEXT,
        "channel": Config.MAIN_CHANNEL_USERNAME
    })


@app.route("/api/config")
def api_config():
    """Returns 100% of your custom branding, colors, and Telegram channel links."""
    return jsonify({
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
        "theme": {
            "primary": Config.THEME_PRIMARY_COLOR,
            "secondary": Config.THEME_SECONDARY_COLOR,
            "bg": Config.THEME_BG_COLOR,
            "glass": Config.THEME_GLASS_CARD
        }
    })


@app.route("/api/home")
def api_home():
    """Fetches Trending, Airing, Popular, Movies, and Hindi Dubbed Anime in 1 fast call."""
    cached = get_cached("home_feed")
    if cached:
        return jsonify(cached)

    query = f"""
    query ($dubIds: [Int]) {{
      trending: Page(page: 1, perPage: 12) {{
        media(type: ANIME, sort: TRENDING_DESC, isAdult: false) {{
          {MEDIA_FRAGMENT}
        }}
      }}
      hindiDubs: Page(page: 1, perPage: 16) {{
        media(type: ANIME, id_in: $dubIds, sort: POPULARITY_DESC, isAdult: false) {{
          {MEDIA_FRAGMENT}
        }}
      }}
      airing: Page(page: 1, perPage: 12) {{
        media(type: ANIME, status: RELEASING, sort: POPULARITY_DESC, isAdult: false) {{
          {MEDIA_FRAGMENT}
        }}
      }}
      popular: Page(page: 1, perPage: 12) {{
        media(type: ANIME, sort: POPULARITY_DESC, isAdult: false) {{
          {MEDIA_FRAGMENT}
        }}
      }}
      movies: Page(page: 1, perPage: 10) {{
        media(type: ANIME, format: MOVIE, sort: POPULARITY_DESC, isAdult: false) {{
          {MEDIA_FRAGMENT}
        }}
      }}
    }}
    """
    raw = anilist_request(query, {"dubIds": list(HINDI_DUB_ANILIST_IDS)})
    data = raw.get("data") or {}

    result = {
        "branding": {
            "app_name": Config.APP_NAME,
            "credit": Config.CREDIT_TEXT,
            "channel": Config.MAIN_CHANNEL_USERNAME,
            "channel_url": Config.MAIN_CHANNEL_URL
        },
        "spotlight": [normalize_anime(m) for m in (data.get("trending", {}).get("media") or [])[:6]],
        "trending": [normalize_anime(m) for m in (data.get("trending", {}).get("media") or [])],
        "hindi_dubs": [normalize_anime(m) for m in (data.get("hindiDubs", {}).get("media") or [])],
        "airing": [normalize_anime(m) for m in (data.get("airing", {}).get("media") or [])],
        "popular": [normalize_anime(m) for m in (data.get("popular", {}).get("media") or [])],
        "movies": [normalize_anime(m) for m in (data.get("movies", {}).get("media") or [])]
    }

    if result["trending"]:
        set_cached("home_feed", result)
    return jsonify(result)


@app.route("/api/search")
def api_search():
    """Supports instant search by title, genre, format, or Hindi Dub filter."""
    q = (request.args.get("q") or "").strip()
    genre = (request.args.get("genre") or "").strip()
    dub_only = request.args.get("dub") == "1"

    cache_key = f"search_{q.lower()}_{genre.lower()}_{dub_only}"
    cached = get_cached(cache_key)
    if cached:
        return jsonify(cached)

    variables = {"page": 1, "perPage": 24}
    filters = ["type: ANIME", "isAdult: false"]
    var_defs = ["$page: Int", "$perPage: Int"]

    if q:
        var_defs.append("$search: String")
        filters.append("search: $search")
        variables["search"] = q
    if genre and genre.upper() != "ALL":
        var_defs.append("$genre: String")
        filters.append("genre: $genre")
        variables["genre"] = genre
    if dub_only:
        var_defs.append("$dubIds: [Int]")
        filters.append("id_in: $dubIds")
        variables["dubIds"] = list(HINDI_DUB_ANILIST_IDS)

    sort_order = "SEARCH_MATCH" if q else "POPULARITY_DESC"
    filters.append(f"sort: {sort_order}")

    query = f"""
    query ({', '.join(var_defs)}) {{
      Page(page: $page, perPage: $perPage) {{
        media({', '.join(filters)}) {{
          {MEDIA_FRAGMENT}
        }}
      }}
    }}
    """
    raw = anilist_request(query, variables)
    media_list = raw.get("data", {}).get("Page", {}).get("media") or []
    results = [normalize_anime(m) for m in media_list]

    payload = {"query": q, "genre": genre, "count": len(results), "results": results}
    set_cached(cache_key, payload)
    return jsonify(payload)


@app.route("/api/schedule")
def api_schedule():
    """Returns upcoming airing episodes grouped by day of the week."""
    cached = get_cached("airing_schedule")
    if cached:
        return jsonify(cached)

    now = int(time.time())
    end_week = now + (7 * 86400)

    query = f"""
    query ($start: Int, $end: Int) {{
      Page(page: 1, perPage: 45) {{
        airingSchedules(airingAt_greater: $start, airingAt_lesser: $end, sort: TIME) {{
          id
          episode
          airingAt
          timeUntilAiring
          media {{
            {MEDIA_FRAGMENT}
          }}
        }}
      }}
    }}
    """
    raw = anilist_request(query, {"start": now - 3600, "end": end_week})
    schedules = raw.get("data", {}).get("Page", {}).get("airingSchedules") or []

    items = []
    for s in schedules:
        media = s.get("media")
        if not media or media.get("isAdult"):
            continue
        norm = normalize_anime(media)
        norm["scheduled_episode"] = s.get("episode")
        norm["airing_at"] = s.get("airingAt")
        norm["time_until_airing"] = s.get("timeUntilAiring")
        items.append(norm)

    payload = {"total": len(items), "schedule": items}
    if items:
        set_cached("airing_schedule", payload)
    return jsonify(payload)


@app.route("/api/anime/<int:anime_id>")
def api_anime_detail(anime_id: int):
    """Fetches complete details, characters, recommendations & episode list for an anime."""
    cache_key = f"anime_detail_{anime_id}"
    cached = get_cached(cache_key)
    if cached:
        return jsonify(cached)

    query = f"""
    query ($id: Int) {{
      Media(id: $id, type: ANIME) {{
        {MEDIA_FRAGMENT}
        characters(sort: ROLE, perPage: 8) {{
          edges {{
            role
            node {{
              name {{
                full
              }}
              image {{
                large
              }}
            }}
          }}
        }}
        recommendations(sort: RATING_DESC, perPage: 8) {{
          nodes {{
            mediaRecommendation {{
              {MEDIA_FRAGMENT}
            }}
          }}
        }}
      }}
    }}
    """
    raw = anilist_request(query, {"id": anime_id})
    media = raw.get("data", {}).get("Media")
    if not media:
        return jsonify({"error": "Anime not found"}), 404

    info = normalize_anime(media)

    characters = []
    for edge in (media.get("characters", {}).get("edges") or []):
        node = edge.get("node") or {}
        characters.append({
            "name": (node.get("name") or {}).get("full") or "Character",
            "role": edge.get("role") or "MAIN",
            "image": (node.get("image") or {}).get("large") or ""
        })

    recommendations = []
    for rec in (media.get("recommendations", {}).get("nodes") or []):
        rec_media = rec.get("mediaRecommendation")
        if rec_media:
            recommendations.append(normalize_anime(rec_media))

    ep_count = int(info.get("latest_ep") or info.get("episodes") or 12)
    ep_count = max(1, min(ep_count, 200))

    custom_entry = CUSTOM_LINKS.get(str(anime_id), {})
    custom_eps = custom_entry.get("episodes", {})

    episodes_list = []
    for ep_num in range(1, ep_count + 1):
        ep_str = f"{ep_num:02d}"
        episodes_list.append({
            "number": ep_num,
            "label": f"Episode {ep_str}",
            "audio": info["audio_badge"],
            "qualities": Config.DEFAULT_QUALITIES,
            "custom_url": custom_eps.get(str(ep_num)) or custom_entry.get("telegram_url") or Config.MAIN_CHANNEL_URL
        })

    info["characters"] = characters
    info["recommendations"] = recommendations
    info["episodes_list"] = episodes_list
    info["buttons"] = {
        "watch_download_url": custom_entry.get("telegram_url") or Config.MAIN_CHANNEL_URL,
        "anime_news_url": Config.NEWS_CHANNEL_URL,
        "all_backup_url": Config.BACKUP_CHANNEL_URL
    }

    set_cached(cache_key, info)
    return jsonify(info)


@app.route("/api/stream/<int:anime_id>/<int:ep_num>")
def api_episode_stream(anime_id: int, ep_num: int):
    """Builds multi-server streaming embed links + direct Telegram Watch/Download links."""
    slug = (request.args.get("slug") or "anime").strip()
    custom_entry = CUSTOM_LINKS.get(str(anime_id), {})
    custom_ep_url = custom_entry.get("episodes", {}).get(str(ep_num))
    tg_channel_link = custom_ep_url or custom_entry.get("telegram_url") or Config.MAIN_CHANNEL_URL

    servers = []
    if custom_ep_url and custom_ep_url.startswith("http") and "t.me/" not in custom_ep_url:
        servers.append({
            "id": "custom_vip",
            "name": f"⚡ {Config.APP_NAME} VIP Server (Hindi/Multi)",
            "embed_url": custom_ep_url
        })

    for srv in Config.STREAM_SERVERS:
        embed_url = srv["embed_template"].format(
            slug=slug,
            anilist_id=anime_id,
            ep=ep_num
        )
        servers.append({
            "id": srv["id"],
            "name": srv["name"],
            "embed_url": embed_url
        })

    return jsonify({
        "anime_id": anime_id,
        "episode": ep_num,
        "audio": Config.DEFAULT_AUDIO_BADGE,
        "qualities": Config.DEFAULT_QUALITIES,
        "servers": servers,
        "telegram_watch_url": tg_channel_link,
        "news_url": Config.NEWS_CHANNEL_URL,
        "backup_url": Config.BACKUP_CHANNEL_URL,
        "credit": Config.CREDIT_TEXT
    })


@app.route("/api/random")
def api_random_anime():
    """Picks a random high-rated anime for the Dice / Random feature."""
    random_id = random.choice(list(HINDI_DUB_ANILIST_IDS))
    return api_anime_detail(random_id)


# =============================================================================
# 5. BLOGGER WEBSITE AUTO-CONNECT WIDGET (/blogger-widget.js)
# =============================================================================
@app.route("/blogger-widget.js")
def blogger_embed_script():
    """
    Allows you to connect this Render app to your Blogger website with 1 line of code:
    <script src="https://your-render-url.onrender.com/blogger-widget.js"></script>
    """
    base_url = Config.WEB_APP_URL if "your-app-name" not in Config.WEB_APP_URL else request.host_url.rstrip("/")
    js_code = f"""
    (function() {{
        var container = document.getElementById('flyton-anime-app') || document.body;
        var iframe = document.createElement('iframe');
        iframe.src = '{base_url}/?embed=blogger';
        iframe.style.width = '100%';
        iframe.style.height = '100vh';
        iframe.style.border = 'none';
        iframe.style.borderRadius = '14px';
        iframe.style.backgroundColor = '{Config.THEME_BG_COLOR}';
        iframe.allow = 'autoplay; fullscreen; picture-in-picture';
        container.appendChild(iframe);
    }})();
    """
    return Response(js_code, mimetype="application/javascript")


# =============================================================================
# 6. BUILT-IN TELEGRAM MINI APP BOT LAUNCHER (BACKGROUND THREAD)
# =============================================================================
def telegram_api_call(method: str, payload: dict) -> dict:
    if not Config.BOT_TOKEN:
        return {}
    url = f"https://api.telegram.org/bot{Config.BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[Telegram API Info] {method}: {e}")
        return {}


def start_telegram_miniapp_bot():
    """
    Configures the Telegram Bot Menu Button to launch your Mini App
    and responds to /start, /app, and owner /set_link commands.
    """
    if not Config.BOT_TOKEN or "your-app-name" in Config.WEB_APP_URL:
        print("[MiniApp Bot Info] Set BOT_TOKEN and WEB_APP_URL in Config/Render to activate Telegram Mini App button.")
        return

    # Set the official Telegram Mini App Menu Button next to the chat input bar
    telegram_api_call("setChatMenuButton", {
        "menu_button": {
            "type": "web_app",
            "text": f"🎬 {Config.APP_NAME}",
            "web_app": {"url": Config.WEB_APP_URL}
        }
    })
    print(f"[MiniApp Bot Active] Telegram WebApp Menu Button linked to: {Config.WEB_APP_URL}")

    offset = 0
    while True:
        try:
            res = telegram_api_call("getUpdates", {"offset": offset, "timeout": 10})
            if not res.get("ok"):
                # If another bot worker is already polling this token, exit polling quietly
                time.sleep(15)
                continue

            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                user_id = (msg.get("from") or {}).get("id")
                text = (msg.get("text") or "").strip()

                if not chat_id or not text:
                    continue

                if text.startswith("/start") or text.startswith("/app"):
                    caption = (
                        f"<blockquote>⚡ <b>{Config.APP_NAME.upper()} — MINI APP</b> ⚡</blockquote>\n"
                        f"<blockquote>🎬 <b>{Config.APP_TAGLINE}</b>\n\n"
                        f"◈ <b>Audio:</b> <code>{Config.DEFAULT_AUDIO_BADGE}</code>\n"
                        f"◈ <b>Quality:</b> <code>480p, 720p, 1080p</code>\n"
                        f"◈ <b>Official Channel:</b> {Config.MAIN_CHANNEL_USERNAME}</blockquote>\n"
                        f"<blockquote>✨ <b>{Config.CREDIT_TEXT}</b></blockquote>"
                    )
                    reply_markup = {
                        "inline_keyboard": [
                            [
                                {
                                    "text": f"🚀 OPEN {Config.APP_NAME.upper()} MINI APP 🚀",
                                    "web_app": {"url": Config.WEB_APP_URL}
                                }
                            ],
                            [
                                {"text": "• ANIME NEWS •", "url": Config.NEWS_CHANNEL_URL},
                                {"text": "• ALL BACKUP •", "url": Config.BACKUP_CHANNEL_URL}
                            ]
                        ]
                    }
                    telegram_api_call("sendMessage", {
                        "chat_id": chat_id,
                        "text": caption,
                        "parse_mode": "HTML",
                        "reply_markup": reply_markup
                    })

                elif text.startswith("/set_link") and (not Config.OWNER_ID or user_id == Config.OWNER_ID):
                    parts = text.split(maxsplit=2)
                    if len(parts) < 3:
                        telegram_api_call("sendMessage", {
                            "chat_id": chat_id,
                            "text": "<blockquote>⚠️ <b>Usage:</b> <code>/set_link [AniList_ID] [Telegram_or_Stream_URL]</code></blockquote>",
                            "parse_mode": "HTML"
                        })
                    else:
                        aid, link_url = parts[1].strip(), parts[2].strip()
                        CUSTOM_LINKS[aid] = {"telegram_url": link_url, "episodes": {}}
                        save_custom_links(CUSTOM_LINKS)
                        API_CACHE.clear()
                        telegram_api_call("sendMessage", {
                            "chat_id": chat_id,
                            "text": f"<blockquote>✅ <b>Saved Custom Link for Anime ID <code>{aid}</code>:</b>\n<code>{link_url}</code></blockquote>",
                            "parse_mode": "HTML"
                        })

        except Exception as loop_err:
            print(f"[MiniApp Polling Info] {loop_err}")
            time.sleep(10)


if __name__ == "__main__":
    # Start Telegram Mini App Bot helper in a daemon thread
    bot_thread = threading.Thread(target=start_telegram_miniapp_bot, daemon=True)
    bot_thread.start()

    print(f"🚀 Starting {Config.APP_NAME} Server on {Config.HOST}:{Config.PORT}...")
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)
