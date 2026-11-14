import os


class Config:
    # =========================================================================
    # 1. BRANDING & CUSTOM CREDIT SETTINGS (100% YOUR OWN CREDIT)
    # =========================================================================
    APP_NAME = os.environ.get("APP_NAME", "FlyTon Anime")
    APP_TAGLINE = os.environ.get("APP_TAGLINE", "Watch & Download HD Anime in Hindi Dub & Sub")
    APP_LOGO_URL = os.environ.get(
        "APP_LOGO_URL",
        "https://cdn-icons-png.flaticon.com/512/2503/2503508.png"
    )

    # Your Custom Credit & Official Channel Links
    CREDIT_TEXT = os.environ.get("CREDIT_TEXT", "Powered by FlyTon Anime Official")
    DEVELOPER_HANDLE = os.environ.get("DEVELOPER_HANDLE", "@FlyTon_Anime")
    MAIN_CHANNEL_USERNAME = os.environ.get("MAIN_CHANNEL_USERNAME", "@FlyTon_Anime")
    MAIN_CHANNEL_URL = os.environ.get("MAIN_CHANNEL_URL", "https://t.me/FlyTon_Anime")
    NEWS_CHANNEL_URL = os.environ.get("NEWS_CHANNEL_URL", "https://t.me/FlyTon_Anime")
    BACKUP_CHANNEL_URL = os.environ.get("BACKUP_CHANNEL_URL", "https://t.me/FlyTon_Anime")

    # =========================================================================
    # 2. TELEGRAM BOT & MINI APP (WEBAPP) CONFIGURATION
    # =========================================================================
    API_ID = int(os.environ.get("API_ID", "0"))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

    # Your Render Live URL (e.g., https://flyton-anime-app.onrender.com)
    # Telegram Mini App button will open this URL inside Telegram!
    WEB_APP_URL = os.environ.get("WEB_APP_URL", "https://your-app-name.onrender.com").rstrip("/")

    # =========================================================================
    # 3. RENDER WEB SERVER & BLOGGER INTEGRATION SETTINGS
    # =========================================================================
    HOST = os.environ.get("HOST", "0.0.0.0")
    PORT = int(os.environ.get("PORT", "8080"))
    DEBUG = os.environ.get("DEBUG", "False").lower() == "true"

    # Connect your Blogger Website (Allows Blogger to embed or fetch API data via CORS)
    BLOGGER_WEBSITE_URL = os.environ.get("BLOGGER_WEBSITE_URL", "")
    CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*")

    # =========================================================================
    # 4. PRO UI THEME SETTINGS (MIDNIGHT GLASS & NEON ACCENTS)
    # =========================================================================
    THEME_PRIMARY_COLOR = os.environ.get("THEME_PRIMARY_COLOR", "#ff2a5f")
    THEME_SECONDARY_COLOR = os.environ.get("THEME_SECONDARY_COLOR", "#7c3aed")
    THEME_BG_COLOR = os.environ.get("THEME_BG_COLOR", "#090a10")
    THEME_GLASS_CARD = os.environ.get("THEME_GLASS_CARD", "rgba(18, 21, 33, 0.78)")

    # =========================================================================
    # 5. ANIME METADATA, SCHEDULE & STREAMING API ENDPOINTS
    # =========================================================================
    ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
    JIKAN_API_URL = "https://api.jikan.moe/v4"
    ANISKIP_API_URL = "https://api.aniskip.com/v2"

    # Default Audio & Quality Badges shown on the Mini App UI
    DEFAULT_AUDIO_BADGE = os.environ.get("DEFAULT_AUDIO_BADGE", "Hindi [Dual Audio]")
    DEFAULT_QUALITIES = ["480p", "720p", "1080p"]

    # Built-in Multi-Server Embed Providers for Instant Episode Playback
    STREAM_SERVERS = [
        {
            "id": "vidstreaming",
            "name": "Server 1 (Fast HD)",
            "embed_template": "https://2anime.xyz/embed/{slug}-episode-{ep}"
        },
        {
            "id": "megaplay",
            "name": "Server 2 (Multi-Quality)",
            "embed_template": "https://megaplay.buzz/stream/s-2/{anilist_id}/{ep}"
        },
        {
            "id": "vidsrc",
            "name": "Server 3 (Backup HD)",
            "embed_template": "https://vidsrc.icu/embed/anime/{anilist_id}/{ep}/0"
        }
    ]

    # Cache TTL in seconds ( keeps Render fast and prevents rate limits )
    CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "900"))
