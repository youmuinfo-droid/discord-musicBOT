# ============================================================
# NEXORA MUSIC BOT
# 完全版 / 17 SLASH COMMANDS
# Python 3.12+
# discord.py 2.6+
# yt-dlp
# FFmpeg
#
# 修正版
#
# - SoundCloud固定フォーマット指定なし
# - Spotify → SoundCloud → YouTube の公開音源フォールバック
# - 再生直前にストリームURLを再取得
# - yt-dlp HTTPヘッダーをFFmpegへ引き継ぐ
# - YouTube 通常取得 → web_safari fallback
# - JS runtime自動検出
# - Spotify oEmbed 504 retry
# - 詳細エラーをDiscord Embedへ表示
# - ANSIエスケープコード除去
# - URLクエリ/認証情報の表示を可能な範囲で抑制
# - 再生中の音量変更対応
# - queue loop修正
# - 既存17コマンド維持
# ============================================================

import asyncio
import html
import json
import os
import platform
import random
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
import tempfile
import wave
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

try:
    import edge_tts
except Exception:
    edge_tts = None


# ============================================================
# .env 簡易ローダー
# python-dotenv不要
# ============================================================

def load_simple_env():
    env_path = ".env"

    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()

                if not line:
                    continue

                if line.startswith("#"):
                    continue

                if "=" not in line:
                    continue

                key, value = line.split("=", 1)

                key = key.strip()
                value = value.strip()

                if len(value) >= 2:
                    if value[0] == '"' and value[-1] == '"':
                        value = value[1:-1]

                    elif value[0] == "'" and value[-1] == "'":
                        value = value[1:-1]

                if key:
                    os.environ.setdefault(key, value)

    except Exception:
        pass


load_simple_env()


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv(
    "DISCORD_TOKEN",
    "herediscordBOTtoken"
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "hereapi")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
PREFIX = "n!"

MAX_QUEUE_SIZE = 50

AUTO_DISCONNECT_SECONDS = 120
DEFAULT_VOLUME = 0.50

MAX_EXTRACT_TASKS = 2

# ============================================================
# VOICE / TTS SETTINGS
# ============================================================
# TTSは軽量なedge-ttsを使用します。
TTS_VOICE = os.getenv("NEXORA_TTS_VOICE", "ja-JP-NanamiNeural")
TTS_RATE = os.getenv("NEXORA_TTS_RATE", "+0%")
TTS_VOLUME = os.getenv("NEXORA_TTS_VOLUME", "+0%")
TTS_MASTER_ENABLED = True
TTS_USER_JOIN_DEFAULT = False
TTS_DJ_DEFAULT = False
TTS_RADIO_DEFAULT = False

# サーバーごとのTTS設定
tts_master_by_guild: dict[int, bool] = {}
tts_user_join_by_guild: dict[int, bool] = {}
tts_dj_by_guild: dict[int, bool] = {}
tts_radio_by_guild: dict[int, bool] = {}
radio_query_by_guild: dict[int, str] = {}
tts_tasks_by_guild: dict[int, asyncio.Task] = {}

# Music Panel custom emojis
SOUNDCLOUD_EMOJI = "<:soundcloud:1551524685307453543>"
LOADING_EMOJI = "<a:kurukuruloading:1551526021612699718>"
LINEAR_LOADING_EMOJI = "<a:senhgataloding:1551526359648444457>"
BOT_EMOJI = "<:avater:1551526567186931713>"

EXTRACT_TIMEOUT = 35

FFMPEG_EXECUTABLE = os.getenv(
    "FFMPEG_EXECUTABLE",
    "ffmpeg"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)


# ============================================================
# INTENTS
# ============================================================

intents = discord.Intents.default()

intents.guilds = True

intents.voice_states = True
intents.message_content = True


# ============================================================
# BOT
# ============================================================

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    case_insensitive=True
)


# ============================================================
# GLOBALS
# ============================================================

extract_semaphore = asyncio.Semaphore(
    MAX_EXTRACT_TASKS
)

music_players: dict[int, "MusicPlayer"] = {}


presence_task: Optional[asyncio.Task] = None

last_error_by_guild: dict[
    int,
    list["Diagnostic"]
] = {}


# ============================================================
# UTILS
# ============================================================

def truncate(
    text: Any,
    length: int = 1000
) -> str:

    if text is None:
        return ""

    text = str(text)

    if len(text) <= length:
        return text

    return text[:max(0, length - 3)] + "..."


def strip_ansi(
    text: Any
) -> str:

    if text is None:
        return ""

    text = str(text)

    return re.sub(
        r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])",
        "",
        text
    )


def safe_error_text(
    text: Any
) -> str:
    """
    Discordに表示するエラーから
    Cookie / token / authorization / query等を
    可能な範囲で除去。
    """

    if text is None:
        return ""

    text = strip_ansi(text)

    patterns = [
        (
            r"(?i)(authorization\s*[:=]\s*)[^\s\r\n]+",
            r"\1[REDACTED]"
        ),
        (
            r"(?i)(cookie\s*[:=]\s*)[^\r\n]+",
            r"\1[REDACTED]"
        ),
        (
            r"(?i)(set-cookie\s*[:=]\s*)[^\r\n]+",
            r"\1[REDACTED]"
        ),
        (
            r"(?i)(token\s*[=:]\s*)[^\s&]+",
            r"\1[REDACTED]"
        ),
        (
            r"(?i)(po_token\s*[=:]\s*)[^\s&]+",
            r"\1[REDACTED]"
        ),
        (
            r"(?i)(visitor_data\s*[=:]\s*)[^\s&]+",
            r"\1[REDACTED]"
        ),
        (
            r"(?i)(visitor_id\s*[=:]\s*)[^\s&]+",
            r"\1[REDACTED]"
        ),
    ]

    for pattern, replacement in patterns:
        text = re.sub(
            pattern,
            replacement,
            text
        )

    # 長いURLのqueryを隠す
    text = re.sub(
        r"(https?://[^\s?]+)\?[^\s]+",
        r"\1?[REDACTED]",
        text
    )

    return truncate(
        text,
        3500
    )


def safe_target(
    target: str
) -> str:

    if not target:
        return "-"

    target = str(target)

    if target.startswith(
        ("http://", "https://")
    ):
        try:
            parsed = urlparse(target)

            result = (
                f"{parsed.scheme}://"
                f"{parsed.netloc}"
                f"{parsed.path}"
            )

            return truncate(
                result,
                500
            )

        except Exception:
            return "[URL]"

    return truncate(
        target,
        500
    )


def is_url(
    text: str
) -> bool:

    return bool(
        re.match(
            r"^https?://",
            text.strip(),
            re.IGNORECASE
        )
    )


def detect_service(
    url: str
) -> str:

    lower = url.lower()

    if "soundcloud.com" in lower:
        return "SoundCloud"

    if (
        "youtube.com" in lower
        or "youtu.be" in lower
        or "music.youtube.com" in lower
    ):
        return "YouTube"

    if "tiktok.com" in lower:
        return "TikTok"

    if "spotify.com" in lower:
        return "Spotify"

    if "bandcamp.com" in lower:
        return "Bandcamp"

    if "mixcloud.com" in lower:
        return "Mixcloud"

    if "vimeo.com" in lower:
        return "Vimeo"

    if "dailymotion.com" in lower:
        return "Dailymotion"

    return "Direct / Generic"


def get_yt_dlp_version() -> str:

    try:
        return str(
            getattr(
                yt_dlp.version,
                "__version__",
                "unknown"
            )
        )

    except Exception:
        return "unknown"


def find_js_runtime() -> Optional[str]:
    bundled_deno = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "deno"
    )
    if os.path.isfile(bundled_deno) and os.access(bundled_deno, os.X_OK):
        return "deno (bundled)"
    bundled_qjs = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "qjs"
    )
    if os.path.isfile(bundled_qjs) and os.access(bundled_qjs, os.X_OK):
        return "quickjs (bundled)"

    candidates = [
        ("deno", "deno"),
        ("node", "node"),
        ("bun", "bun"),
        ("qjs", "quickjs"),
    ]

    for executable, name in candidates:

        if shutil.which(executable):
            return name

    return None


def get_ffmpeg_path() -> Optional[str]:

    return shutil.which(
        FFMPEG_EXECUTABLE
    )


def runtime_info() -> str:

    ffmpeg = get_ffmpeg_path()

    if ffmpeg:
        ffmpeg_text = ffmpeg
    else:
        ffmpeg_text = "見つかりません"

    js_runtime = find_js_runtime()

    if js_runtime:
        js_text = js_runtime
    else:
        js_text = "見つかりません"

    return (
        f"Python: {platform.python_version()}\n"
        f"discord.py: {discord.__version__}\n"
        f"yt-dlp: {get_yt_dlp_version()}\n"
        f"FFmpeg: {ffmpeg_text}\n"
        f"JS runtime: {js_text}"
    )


# ============================================================
# DIAGNOSTICS
# ============================================================

@dataclass
class Diagnostic:

    service: str

    stage: str

    target: str

    error: str = ""

    warnings: list[str] = field(
        default_factory=list
    )

    success: bool = False


def remember_error(
    guild_id: Optional[int],
    diagnostic: Diagnostic
):

    if guild_id is None:
        return

    last_error_by_guild.setdefault(
        guild_id,
        []
    )

    last_error_by_guild[guild_id].append(
        diagnostic
    )

    last_error_by_guild[guild_id] = (
        last_error_by_guild[guild_id][-10:]
    )


def error_hint(
    error_text: str
) -> str:

    lower = (
        safe_error_text(
            error_text
        ).lower()
    )

    if (
        "sign in to confirm" in lower
        or "not a bot" in lower
        or "confirm you're not a bot" in lower
    ):
        return (
            "YouTube側からBot判定・アクセス制限が返されています。"
            "このサーバーから対象動画を現在取得できない可能性があります。"
            "NEXORAはこの制限を回避する処理は行わず、"
            "Spotifyの場合はSoundCloudなど別の公開音源を先に試します。"
        )

    if "no video formats found" in lower:
        return (
            "再生可能な音声形式が取得できませんでした。"
            "SoundCloud等では公開状態や配信形式の変更によって、"
            "一部の音源だけ取得できない場合があります。"
        )

    if (
        "http 504" in lower
        or "504" in lower
        or "gateway timeout" in lower
    ):
        return (
            "外部サービスからの応答がタイムアウトしました。"
            "一時的な通信失敗の可能性があります。"
            "もう一度試すか、曲名検索を使用してください。"
        )

    if "no supported javascript runtime" in lower:
        return (
            "yt-dlpが利用できるJavaScript runtimeを"
            "サーバー上で確認できませんでした。"
            "YouTubeの一部取得ではruntimeが必要になる場合があります。"
        )

    if "429" in lower or "too many requests" in lower:
        return (
            "外部サービスからアクセス頻度制限が返されました。"
            "少し時間を置いてから再試行してください。"
        )

    if "403" in lower:
        return (
            "外部サービスからアクセス拒否が返されました。"
            "対象音源側の公開条件や一時的なアクセス制限が"
            "原因の可能性があります。"
        )

    if "timeout" in lower:
        return (
            "取得処理が時間内に完了しませんでした。"
            "外部サービスの応答遅延またはネットワーク状況が"
            "原因の可能性があります。"
        )

    return (
        "取得処理中に外部サービスまたはyt-dlpから"
        "エラーが返されました。下の詳細を確認してください。"
    )


def make_error_embed(
    title: str,
    message: str,
    diagnostics: Optional[list[Diagnostic]] = None
) -> discord.Embed:

    embed = discord.Embed(
        title=f"❌ {title}",
        description=truncate(
            message,
            3900
        ),
        color=discord.Color.red()
    )

    if diagnostics:

        for index, diagnostic in enumerate(
            diagnostics[:6],
            start=1
        ):

            value_parts = [
                f"**サービス:** {diagnostic.service}",
                f"**段階:** {diagnostic.stage}",
                f"**対象:** {safe_target(diagnostic.target)}",
            ]

            if diagnostic.error:

                value_parts.append(
                    "**yt-dlp / エラー:**\n"
                    + safe_error_text(
                        diagnostic.error
                    )[:700]
                )

            if diagnostic.warnings:

                warning_text = "\n".join(
                    diagnostic.warnings[:3]
                )

                value_parts.append(
                    "**警告:**\n"
                    + safe_error_text(
                        warning_text
                    )[:500]
                )

            embed.add_field(
                name=f"詳細 {index}",
                value=truncate(
                    "\n".join(value_parts),
                    1024
                ),
                inline=False
            )

    embed.add_field(
        name="環境",
        value=truncate(
            runtime_info(),
            1000
        ),
        inline=False
    )

    combined_error = ""

    if diagnostics:

        combined_error = " ".join(
            d.error
            for d in diagnostics
            if d.error
        )

    if combined_error:

        embed.add_field(
            name="考えられる原因",
            value=truncate(
                error_hint(
                    combined_error
                ),
                1000
            ),
            inline=False
        )

    embed.set_footer(
        text=(
            "NEXORA Music • "
            f"yt-dlp {get_yt_dlp_version()}"
        )
    )

    return embed


def make_success_embed(
    title: str,
    description: str = ""
) -> discord.Embed:

    return discord.Embed(
        title=f"✅ {title}",
        description=description,
        color=discord.Color.green()
    )


# ============================================================
# YT-DLP LOGGER
# ============================================================

class DetailedYTDLPLogger:

    def __init__(self):

        self.warnings: list[str] = []

        self.errors: list[str] = []

        self.debug_messages: list[str] = []

    def debug(
        self,
        msg
    ):

        if not msg:
            return

        text = strip_ansi(
            str(msg)
        )

        if not text.strip():
            return

        # 大量のdebugログを保存しない
        if len(self.debug_messages) < 20:

            self.debug_messages.append(
                truncate(
                    text,
                    1000
                )
            )

    def warning(
        self,
        msg
    ):

        if msg:

            self.warnings.append(
                truncate(
                    strip_ansi(str(msg)),
                    1500
                )
            )

    def error(
        self,
        msg
    ):

        if msg:

            self.errors.append(
                truncate(
                    strip_ansi(str(msg)),
                    1500
                )
            )


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def base_ytdlp_options(
    *,
    youtube_client: Optional[str] = None,
    flat: bool = False
) -> dict:

    options: dict[str, Any] = {

        "quiet": True,

        "no_warnings": False,

        "skip_download": True,

        "noplaylist": True,

        "nocheckcertificate": False,

        "source_address": "0.0.0.0",

        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": (
                "ja-JP,ja;q=0.9,"
                "en-US;q=0.8,en;q=0.7"
            ),
        },

        # 固定SoundCloudフォーマットは指定しない。
        # 現在利用可能な音声をyt-dlp側に選択させる。
        "format": (
            "bestaudio[acodec!=none][abr>=160]"
            "/bestaudio"
            "/bestvideo*+bestaudio"
            "/best"
        ),

        "retries": 2,

        "fragment_retries": 2,

        "socket_timeout": 15,

        "extract_flat": flat,

        "ignoreerrors": False,
    }

    bundled_deno = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "deno"
    )
    if os.path.isfile(bundled_deno) and os.access(bundled_deno, os.X_OK):
        options["js_runtimes"] = {
            "deno": {"path": bundled_deno}
        }
    else:
        bundled_qjs = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "qjs"
        )
        js_runtime = None
        if os.path.isfile(bundled_qjs) and os.access(bundled_qjs, os.X_OK):
            options["js_runtimes"] = {
                "quickjs": {"path": bundled_qjs}
            }
        else:
            js_runtime = find_js_runtime()
        if js_runtime and "(" not in js_runtime:
            options["js_runtimes"] = {
                js_runtime: {}
            }

    if youtube_client:

        options["extractor_args"] = {
            "youtube": {
                "player_client": [
                    youtube_client
                ]
            }
        }

    return options


# ============================================================
# YT-DLP SYNC
# ============================================================

def ytdlp_extract_sync(
    target: str,
    options: dict
):

    logger = DetailedYTDLPLogger()

    local_options = dict(
        options
    )

    local_options["logger"] = logger

    try:

        with yt_dlp.YoutubeDL(
            local_options
        ) as ydl:

            info = ydl.extract_info(
                target,
                download=False
            )

        return {
            "info": info,
            "warnings": logger.warnings,
            "errors": logger.errors,
            "exception": "",
        }

    except Exception as exc:

        return {
            "info": None,
            "warnings": logger.warnings,
            "errors": logger.errors,
            "exception": (
                f"{type(exc).__name__}: "
                f"{exc}"
            ),
        }


async def ytdlp_extract(
    target: str,
    options: dict
):

    async with extract_semaphore:

        loop = asyncio.get_running_loop()

        try:

            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    ytdlp_extract_sync,
                    target,
                    options
                ),
                timeout=EXTRACT_TIMEOUT
            )

            return result

        except asyncio.TimeoutError:

            return {
                "info": None,
                "warnings": [],
                "errors": [],
                "exception": (
                    "yt-dlp extraction timeout "
                    f"({EXTRACT_TIMEOUT}s)"
                )
            }

        except Exception as exc:

            return {
                "info": None,
                "warnings": [],
                "errors": [],
                "exception": (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )
            }


# ============================================================
# INFO HELPERS
# ============================================================

def get_first_entry(
    info: Optional[dict]
) -> Optional[dict]:

    if not info:
        return None

    if "entries" in info:

        entries = info.get(
            "entries"
        )

        if entries:

            for entry in entries:

                if entry:
                    return entry

        return None

    return info


def choose_audio_format(
    info: Optional[dict]
) -> Optional[dict]:

    if not info:
        return None

    formats = info.get(
        "formats"
    ) or []

    candidates = []

    for fmt in formats:

        if not isinstance(
            fmt,
            dict
        ):
            continue

        url = fmt.get(
            "url"
        )

        if not url:
            continue

        acodec = str(
            fmt.get("acodec")
            or ""
        ).lower()

        vcodec = str(
            fmt.get("vcodec")
            or ""
        ).lower()

        if acodec in (
            "",
            "none"
        ):
            continue

        if vcodec not in (
            "",
            "none"
        ):
            continue

        protocol = str(
            fmt.get("protocol")
            or ""
        ).lower()

        abr = fmt.get(
            "abr"
        ) or 0

        try:
            abr = float(
                abr
            )
        except Exception:
            abr = 0

        score = 0

        if "opus" in acodec:
            score += 50

        if "mp4a" in acodec:
            score += 40

        if "aac" in acodec:
            score += 35

        if protocol in (
            "http",
            "https"
        ):
            score += 30

        if protocol in (
            "m3u8",
            "m3u8_native"
        ):
            score += 25

        score += min(
            int(abr),
            320
        ) / 10

        candidates.append(
            (
                score,
                fmt
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    return candidates[0][1]


def get_webpage_url(
    info: dict
) -> str:

    return (
        info.get("webpage_url")
        or info.get("original_url")
        or ""
    )


def get_http_headers(
    fmt: Optional[dict],
    info: Optional[dict]
) -> dict:

    headers = {}

    if isinstance(
        info,
        dict
    ):

        info_headers = info.get(
            "http_headers"
        )

        if isinstance(
            info_headers,
            dict
        ):
            headers.update(
                info_headers
            )

    if isinstance(
        fmt,
        dict
    ):

        fmt_headers = fmt.get(
            "http_headers"
        )

        if isinstance(
            fmt_headers,
            dict
        ):
            headers.update(
                fmt_headers
            )

    if "User-Agent" not in headers:

        headers["User-Agent"] = USER_AGENT

    return headers


# ============================================================
# TRACK
# ============================================================

@dataclass
class Track:

    title: str

    webpage_url: str

    requester_id: int

    requester_name: str

    duration: Optional[float] = None

    thumbnail: Optional[str] = None

    uploader: Optional[str] = None

    service: str = "Unknown"

    stream_url: str = ""

    stream_headers: dict = field(
        default_factory=dict
    )

    original_query: str = ""


# ============================================================
# EXTRACT TRACK
# ============================================================

def info_to_track(
    info: dict,
    requester: discord.Member,
    original_query: str,
    service: str
) -> Optional[Track]:

    info = get_first_entry(
        info
    )

    if not info:
        return None

    title = (
        info.get("title")
        or info.get("track")
        or "Unknown title"
    )

    webpage_url = (
        info.get("webpage_url")
        or info.get("original_url")
        or ""
    )

    stream_url = ""

    headers = {}

    direct_fmt = choose_audio_format(
        info
    )

    if direct_fmt:

        stream_url = (
            direct_fmt.get("url")
            or ""
        )

        headers = get_http_headers(
            direct_fmt,
            info
        )

    elif info.get("url") and is_url(
        str(info.get("url"))
    ):

        stream_url = str(
            info.get("url")
        )

        headers = get_http_headers(
            None,
            info
        )

    thumbnail = info.get(
        "thumbnail"
    )

    if not thumbnail:

        thumbnails = info.get(
            "thumbnails"
        ) or []

        if thumbnails:

            try:

                thumbnail = (
                    thumbnails[-1]
                    .get("url")
                )

            except Exception:

                thumbnail = None

    duration = info.get(
        "duration"
    )

    try:

        if duration is not None:
            duration = float(
                duration
            )

    except Exception:

        duration = None

    return Track(
        title=html.unescape(
            str(title)
        ),

        webpage_url=webpage_url,

        requester_id=requester.id,

        requester_name=requester.display_name,

        duration=duration,

        thumbnail=thumbnail,

        uploader=(
            str(info.get("uploader"))
            if info.get("uploader")
            else None
        ),

        service=service,

        stream_url=stream_url,

        stream_headers=headers,

        original_query=original_query
    )


# ============================================================
# YOUTUBE EXTRACTION
# ============================================================

async def extract_youtube(
    target: str
):

    attempts: list[Diagnostic] = []

    strategies = [
        (
            "通常",
            None
        ),
        (
            "web_safari",
            "web_safari"
        ),
        (
            "android_no_cookie",
            "android"
        ),
        (
            "ios_no_cookie",
            "ios"
        ),
    ]

    for stage_name, client in strategies:

        result = await ytdlp_extract(
            target,
            base_ytdlp_options(
                youtube_client=client
            )
        )

        info = result.get(
            "info"
        )

        errors = result.get(
            "errors"
        ) or []

        warnings = result.get(
            "warnings"
        ) or []

        exception = result.get(
            "exception"
        )

        if info:

            entry = get_first_entry(
                info
            )

            if entry:

                fmt = choose_audio_format(
                    entry
                )

                if fmt:

                    attempts.append(
                        Diagnostic(
                            service="YouTube",
                            stage=stage_name,
                            target=target,
                            warnings=warnings,
                            success=True
                        )
                    )

                    return (
                        info,
                        attempts
                    )

                # web_safariでinfoは取れたが
                # 音声形式がない場合は成功扱いにしない。
                attempts.append(
                    Diagnostic(
                        service="YouTube",
                        stage=stage_name,
                        target=target,
                        error=(
                            "動画情報は取得できましたが、"
                            "再生可能な音声形式がありません。"
                        ),
                        warnings=warnings,
                        success=False
                    )
                )

        error = (
            exception
            or (
                errors[-1]
                if errors
                else (
                    "再生可能な情報を取得できませんでした。"
                )
            )
        )

        attempts.append(
            Diagnostic(
                service="YouTube",
                stage=stage_name,
                target=target,
                error=error,
                warnings=warnings,
                success=False
            )
        )

    return (
        None,
        attempts
    )


# ============================================================
# GENERIC / SOUNDCLOUD EXTRACTION
# ============================================================

async def extract_generic(
    target: str,
    service: str
):

    attempts: list[Diagnostic] = []

    result = await ytdlp_extract(
        target,
        base_ytdlp_options()
    )

    info = result.get(
        "info"
    )

    errors = result.get(
        "errors"
    ) or []

    warnings = result.get(
        "warnings"
    ) or []

    exception = result.get(
        "exception"
    )

    if info:

        entry = get_first_entry(
            info
        )

        if entry:

            fmt = choose_audio_format(
                entry
            )

            if fmt:

                attempts.append(
                    Diagnostic(
                        service=service,
                        stage="yt-dlp",
                        target=target,
                        warnings=warnings,
                        success=True
                    )
                )

                return (
                    info,
                    attempts
                )

            # SoundCloudなどで情報だけ取れて
            # 音声形式がない場合
            error = (
                "yt-dlpの情報は取得できましたが、"
                "再生可能な音声形式がありません。"
            )

            attempts.append(
                Diagnostic(
                    service=service,
                    stage="音声形式確認",
                    target=target,
                    error=error,
                    warnings=warnings,
                    success=False
                )
            )

            return (
                None,
                attempts
            )

    error = (
        exception
        or (
            errors[-1]
            if errors
            else (
                "yt-dlpから情報を取得できませんでした。"
            )
        )
    )

    attempts.append(
        Diagnostic(
            service=service,
            stage="yt-dlp",
            target=target,
            error=error,
            warnings=warnings,
            success=False
        )
    )

    return (
        None,
        attempts
    )


# ============================================================
# KT3 / KTC LOCAL PLAYBACK
# ============================================================
KT3_HEADER_SIZE = 48
KT3_MAX_BYTES = 500 * 1024 * 1024
KTC_DECODER = os.getenv("KTC_DECODER", "ktc_decoder/target/release/ktc-decoder")
KTC_WASM = os.getenv(
    "KTC_WASM",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ktc.wasm")
)

def _write_float_wav(path: str, channels: list[list[float]], sample_rate: int):
    count = len(channels[0])
    channel_count = len(channels)
    data_len = count * channel_count * 4
    block_align = channel_count * 4
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 3, channel_count,
                                       sample_rate, sample_rate * block_align,
                                       block_align, 32))
        f.write(b"data" + struct.pack("<I", data_len))
        for i in range(count):
            for channel in channels:
                f.write(struct.pack("<f", channel[i]))

def _read_wav_sample_info(path: str) -> tuple[int, int]:
    """PCM(format 1)とIEEE float(format 3)の両方に対応するWAV情報取得。"""
    with open(path, "rb") as f:
        if f.read(4) != b"RIFF":
            raise ValueError("RIFF WAVではありません")
        f.read(4)
        if f.read(4) != b"WAVE":
            raise ValueError("WAVEヘッダーがありません")
        sample_rate = 0
        block_align = 0
        data_size = 0
        while True:
            chunk = f.read(8)
            if len(chunk) < 8:
                break
            chunk_id = chunk[:4]
            chunk_size = struct.unpack("<I", chunk[4:])[0]
            payload = f.read(chunk_size)
            if chunk_size & 1:
                f.read(1)
            if chunk_id == b"fmt " and len(payload) >= 16:
                _, _, sample_rate, _, block_align, _ = struct.unpack_from("<HHIIHH", payload, 0)
            elif chunk_id == b"data":
                data_size = chunk_size
        if not sample_rate or not block_align or not data_size:
            raise ValueError("WAVのfmt/dataチャンクが不正です")
        return sample_rate, data_size // block_align

async def decode_ktc_with_wasm(data: bytes, wav_path: str):
    try:
        import wasmtime
    except ImportError as exc:
        raise RuntimeError("WASM再生にはrequirements.txtへ wasmtime>=20.0.0 が必要です。") from exc
    if not os.path.isfile(KTC_WASM):
        raise RuntimeError("ktc.wasmがありません。main.pyと同じフォルダへアップロードしてください。")

    def decode_sync():
        engine = wasmtime.Engine()
        module = wasmtime.Module.from_file(engine, KTC_WASM)
        store = wasmtime.Store(engine)
        instance = wasmtime.Instance(store, module, [])
        exports = instance.exports(store)
        memory = exports["memory"]
        alloc = exports["ktc_alloc"]
        decode = exports["ktc_decode"]
        free = exports["ktc_free"]
        free_result = exports["ktc_free_result"]
        ptr = alloc(store, len(data))
        memory.write(store, data, ptr)
        result_ptr = decode(store, ptr, len(data))
        try:
            raw_count = memory.read(store, result_ptr, result_ptr + 4)
            result_count = struct.unpack("<I", bytes(raw_count))[0]
            raw = bytes(memory.read(store, result_ptr, result_ptr + result_count * 4))
            status = struct.unpack_from("<I", raw, 4)[0]
            if status:
                message_len = struct.unpack_from("<I", raw, 8)[0]
                message = raw[12:12 + message_len].decode("utf-8", "replace")
                raise RuntimeError(f"KTCデコード失敗: {message}")
            channel_count, sample_rate, sample_count = struct.unpack_from("<III", raw, 8)
            values = memoryview(raw)[20:]
            data_len = sample_count * channel_count * 4
            block_align = channel_count * 4
            with open(wav_path, "wb") as wav:
                wav.write(b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE")
                wav.write(b"fmt " + struct.pack("<IHHIIHH", 16, 3, channel_count,
                                                   sample_rate, sample_rate * block_align,
                                                   block_align, 32))
                wav.write(b"data" + struct.pack("<I", data_len))
                chunk_frames = 8192
                for first in range(0, sample_count, chunk_frames):
                    count = min(chunk_frames, sample_count - first)
                    interleaved = bytearray(count * block_align)
                    out_pos = 0
                    for i in range(first, first + count):
                        for channel in range(channel_count):
                            at = 20 + (channel * sample_count + i) * 4
                            interleaved[out_pos:out_pos + 4] = values[at - 20:at - 16]
                            out_pos += 4
                    wav.write(interleaved)
        finally:
            free_result(store, result_ptr)
            free(store, ptr, len(data))

    await asyncio.to_thread(decode_sync)

def _read_kt3_container(path: str) -> tuple[bytes, dict]:
    file_size = os.path.getsize(path)
    if file_size > KT3_MAX_BYTES: raise ValueError("KT3ファイルは500MiB以下にしてください。")
    with open(path, "rb") as f:
        header = f.read(KT3_HEADER_SIZE)
        if len(header) != KT3_HEADER_SIZE or header[:3] != b"KT3": raise ValueError("KT3ヘッダーが不正です。")
        version, media_format = header[4], header[5]
        meta_len = int.from_bytes(header[24:28], "little"); lyrics_len = int.from_bytes(header[28:32], "little"); media_len = int.from_bytes(header[32:36], "little")
        spark_len = int.from_bytes(header[38:42], "little")
        frame_cache_len = int.from_bytes(header[42:46], "little")
        if version != 1 or any(n > file_size for n in (meta_len, lyrics_len, media_len)): raise ValueError(f"未対応または不正なKT3です（version={version}）。")
        f.seek(KT3_HEADER_SIZE); raw_meta = f.read(meta_len); f.seek(lyrics_len, os.SEEK_CUR); media = f.read(media_len)
    if len(media) != media_len: raise ValueError("KT3のメディアデータが途中で終わっています。")
    try: meta = json.loads(raw_meta.decode("utf-8")) if raw_meta else {}
    except Exception as exc: raise ValueError(f"KT3メタデータ(JSON)を読めません: {exc}") from exc
    stems = {}
    stem_meta = meta.get("stems", {}) if isinstance(meta, dict) else {}
    if isinstance(stem_meta, dict) and (stem_meta.get("vocalLen") or stem_meta.get("instLen")):
        stem_pos = KT3_HEADER_SIZE + meta_len + lyrics_len + media_len + spark_len + frame_cache_len
        with open(path, "rb") as f:
            f.seek(stem_pos)
            vocal_len = int(stem_meta.get("vocalLen", 0) or 0)
            inst_len = int(stem_meta.get("instLen", 0) or 0)
            if vocal_len:
                stems["vocal"] = f.read(vocal_len)
            if inst_len:
                stems["inst"] = f.read(inst_len)
    return media, {
        "meta": meta if isinstance(meta, dict) else {},
        "media_format": media_format,
        "loop_start": struct.unpack_from("<d", header, 8)[0],
        "loop_end": struct.unpack_from("<d", header, 16)[0],
        "stems": stems,
    }

def _legacy_suffix(media: bytes, media_format: int) -> str:
    for sig, ext in ((b"OggS", ".ogg"), (b"RIFF", ".wav"), (b"ID3", ".mp3"), (b"fLaC", ".flac"), (b"ftyp", ".m4a"), (b"OpusHead", ".opus")):
        if media.startswith(sig): return ext
    return {0: ".mp3", 1: ".ogg", 2: ".wav", 3: ".m4a", 4: ".opus"}.get(media_format, ".bin")

async def prepare_kt3_source(track: "Track") -> str:
    media, info = _read_kt3_container(track.stream_url); meta = info["meta"]
    setattr(track, "kt3_loop_start", max(0.0, float(info.get("loop_start", 0.0))))
    setattr(track, "kt3_loop_end", max(0.0, float(info.get("loop_end", 0.0))))
    cleanup_paths = getattr(track, "cleanup_paths", None)
    if cleanup_paths is None:
        cleanup_paths = []
        setattr(track, "cleanup_paths", cleanup_paths)
    if meta.get("title"): track.title = f"{meta.get('artist')} - {meta['title']}" if meta.get("artist") else str(meta["title"])
    work_dir = os.path.abspath("nexora_media_downloads"); os.makedirs(work_dir, exist_ok=True)
    stems = info.get("stems", {})
    if not media and stems:
        stem_paths = {}
        for stem_name in ("vocal", "inst"):
            stem_data = stems.get(stem_name)
            if not stem_data:
                continue
            if not stem_data.startswith(b"KTC\x00"):
                raise RuntimeError(f"KT3の{stem_name} stemがKTC形式ではありません。")
            fd, stem_path = tempfile.mkstemp(
                prefix=f"kt3_{stem_name}_", suffix=".wav", dir=work_dir
            )
            os.close(fd)
            try:
                await decode_ktc_with_wasm(stem_data, stem_path)
            except Exception:
                try: os.remove(stem_path)
                except OSError: pass
                raise
            cleanup_paths.append(stem_path)
            stem_paths[stem_name] = stem_path
        primary = stem_paths.get("vocal") or stem_paths.get("inst")
        secondary = stem_paths.get("inst") if stem_paths.get("vocal") else None
        if primary:
            if secondary:
                setattr(track, "kt3_extra_stem", secondary)
            if getattr(track, "kt3_loop_end", 0.0) <= 0.0:
                sample_rate, frame_count = _read_wav_sample_info(primary)
                setattr(track, "kt3_loop_end", frame_count / sample_rate)
            return primary
    if media.startswith(b"KTC\x00"):
        fd, wav_path = tempfile.mkstemp(prefix="kt3_decoded_", suffix=".wav", dir=work_dir); os.close(fd)
        if os.path.isfile(KTC_WASM):
            try:
                await decode_ktc_with_wasm(media, wav_path)
            except Exception:
                try: os.remove(wav_path)
                except OSError: pass
                raise
            cleanup_paths.append(wav_path)
            if getattr(track, "kt3_loop_end", 0.0) <= 0.0:
                sample_rate, frame_count = _read_wav_sample_info(wav_path)
                setattr(track, "kt3_loop_end", frame_count / sample_rate)
            return wav_path

        fd, ktc_path = tempfile.mkstemp(prefix="kt3_audio_", suffix=".ktc", dir=work_dir); os.close(fd)
        with open(ktc_path, "wb") as f: f.write(media)
        decoder_candidates = [
            KTC_DECODER,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "ktc-decoder"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "ktc_decoder"),
        ]
        decoder = next(
            (candidate for candidate in decoder_candidates if os.path.isfile(candidate)),
            shutil.which(KTC_DECODER) or KTC_DECODER,
        )
        if not os.path.isfile(decoder):
            for q in (ktc_path, wav_path):
                try: os.remove(q)
                except OSError: pass
            raise RuntimeError("ktc.wasmまたはKTCデコーダーがありません。ktc.wasmをmain.pyと同じフォルダへアップロードしてください。")
        try: result = await asyncio.to_thread(subprocess.run, [decoder, ktc_path, wav_path], capture_output=True, text=True, timeout=180, check=False)
        finally:
            try: os.remove(ktc_path)
            except OSError: pass
        if result.returncode != 0 or not os.path.isfile(wav_path) or os.path.getsize(wav_path) < 44:
            try: os.remove(wav_path)
            except OSError: pass
            raise RuntimeError(f"KTC音声のデコードに失敗しました: {(result.stderr or result.stdout or '')[-500:]}")
        cleanup_paths.append(wav_path)
        if getattr(track, "kt3_loop_end", 0.0) <= 0.0:
            sample_rate, frame_count = _read_wav_sample_info(wav_path)
            setattr(track, "kt3_loop_end", frame_count / sample_rate)
        return wav_path
    fd, media_path = tempfile.mkstemp(prefix="kt3_media_", suffix=_legacy_suffix(media, info["media_format"]), dir=work_dir); os.close(fd)
    with open(media_path, "wb") as f: f.write(media)
    cleanup_paths.append(media_path); return media_path

# ============================================================
# PLAYBACK STREAM RESOLUTION
# ============================================================

async def resolve_stream(
    track: Track
):

    # ========================================================
    # 重要:
    # 保存済みstream_urlをそのまま使わない。
    #
    # YouTube / SoundCloud等のURLは期限切れになる可能性が
    # あるため、webpage_urlがある場合は再生直前に再取得する。
    # ========================================================

    if not track.webpage_url:

        if track.stream_url:
            if track.service in {"DownloadedFile", "LocalFile"} and os.path.splitext(track.stream_url)[1].lower() == ".kt3":
                track.stream_url = await prepare_kt3_source(track)
            return (
                track.stream_url,
                track.stream_headers,
                []
            )

        diagnostic = Diagnostic(
            service=track.service,
            stage="再生URL取得",
            target=track.original_query,
            error=(
                "webpage_urlが存在しないため、"
                "再生用ストリームURLを取得できませんでした。"
            )
        )

        return (
            None,
            {},
            [diagnostic]
        )

    service = track.service

    if service == "YouTube":

        info, diagnostics = await extract_youtube(
            track.webpage_url
        )

    else:

        info, diagnostics = await extract_generic(
            track.webpage_url,
            service
        )

    if not info:

        return (
            None,
            {},
            diagnostics
        )

    entry = get_first_entry(
        info
    )

    if not entry:

        diagnostics.append(
            Diagnostic(
                service=service,
                stage="再生URL選択",
                target=track.webpage_url,
                error=(
                    "yt-dlpの結果にentryがありません。"
                )
            )
        )

        return (
            None,
            {},
            diagnostics
        )

    fmt = choose_audio_format(
        entry
    )

    if not fmt:

        diagnostics.append(
            Diagnostic(
                service=service,
                stage="音声形式選択",
                target=track.webpage_url,
                error=(
                    "音声形式が1件も取得できませんでした。"
                )
            )
        )

        return (
            None,
            {},
            diagnostics
        )

    stream_url = fmt.get(
        "url"
    )

    if not stream_url:

        diagnostics.append(
            Diagnostic(
                service=service,
                stage="ストリームURL",
                target=track.webpage_url,
                error=(
                    "選択された音声形式にURLがありません。"
                )
            )
        )

        return (
            None,
            {},
            diagnostics
        )

    headers = get_http_headers(
        fmt,
        entry
    )

    # 最新の情報に更新
    track.stream_url = stream_url

    track.stream_headers = headers

    # タイトル等も更新
    new_title = (
        entry.get("title")
        or entry.get("track")
    )

    if new_title:

        track.title = html.unescape(
            str(new_title)
        )

    if entry.get("thumbnail"):

        track.thumbnail = (
            entry.get("thumbnail")
        )

    if entry.get("uploader"):

        track.uploader = str(
            entry.get("uploader")
        )

    if entry.get("duration") is not None:

        try:

            track.duration = float(
                entry.get("duration")
            )

        except Exception:
            pass

    return (
        stream_url,
        headers,
        diagnostics
    )


# ============================================================
# FFmpeg HEADER
# ============================================================

def build_ffmpeg_header_blob(
    headers: dict
) -> str:

    lines = []

    # Cookieや認証情報はFFmpegへ渡さない
    # 通常の公開音源再生に必要なヘッダーだけ使用する。
    blocked_headers = {
        "cookie",
        "authorization",
        "proxy-authorization",
        "x-goog-visitor-id",
        "x-goog-visitor-data",
    }

    for key, value in headers.items():

        if value is None:
            continue

        key = (
            str(key)
            .replace("\r", "")
            .replace("\n", "")
        )

        value = (
            str(value)
            .replace("\r", "")
            .replace("\n", "")
        )

        if not key:
            continue

        if key.lower() in blocked_headers:
            continue

        lines.append(
            f"{key}: {value}"
        )

    if not any(
        line.lower().startswith(
            "user-agent:"
        )
        for line in lines
    ):

        lines.append(
            f"User-Agent: {USER_AGENT}"
        )

    # FFmpegの -headers 用
    return "\r\n".join(lines) + "\r\n"


# ============================================================
# FFMPEG
# ============================================================

def _loudnorm_filter() -> str:
    return "loudnorm=I=-16:TP=-1.5:LRA=11"

def _atempo_filter(speed: float) -> str:
    speed = max(0.5, min(float(speed), 2.0))
    return f"atempo={speed:g}"

def create_audio_source(
    stream_url: str,
    headers: dict,
    volume: float,
    loop_start: float = 0.0,
    loop_end: float = 0.0,
    extra_input: Optional[str] = None,
    speed: float = 1.0,
):

    if not is_url(stream_url):
        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            raise RuntimeError('FFmpegが見つかりません。')
        local_options = "-vn -sn -dn -loglevel warning"
        if extra_input and os.path.isfile(extra_input):
            local_options = f"-i {shlex.quote(extra_input)} " + local_options
            if loop_end > loop_start >= 0.0:
                try:
                    sample_rate, _ = _read_wav_sample_info(stream_url)
                    start_sample = max(0, round(loop_start * sample_rate))
                    end_sample = max(start_sample + 1, round(loop_end * sample_rate))
                    loop_size = end_sample - start_sample
                    local_options += (
                        f' -filter_complex "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[m];'
                        f'[m]aloop=loop=-1:size={loop_size}:start={start_sample},'
                        f'{_loudnorm_filter()},{_atempo_filter(speed)},volume={max(0.0, min(float(volume), 6.0))}[out]" -map "[out]"'
                    )
                except (OSError, ValueError):
                    local_options += (
                        f' -filter_complex "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0,'
                        f'{_loudnorm_filter()},{_atempo_filter(speed)},volume={max(0.0, min(float(volume), 6.0))}[out]" -map "[out]"'
                    )
            else:
                local_options += (
                    f' -filter_complex "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0,'
                    f'{_loudnorm_filter()},{_atempo_filter(speed)},volume={max(0.0, min(float(volume), 6.0))}[out]" -map "[out]"'
                )
            return discord.FFmpegPCMAudio(
                stream_url, executable=ffmpeg_path, before_options='-nostdin',
                options=local_options)
        if loop_end > loop_start >= 0.0:
            try:
                sample_rate, _ = _read_wav_sample_info(stream_url)
                start_sample = max(0, round(loop_start * sample_rate))
                end_sample = max(start_sample + 1, round(loop_end * sample_rate))
                loop_size = end_sample - start_sample
                local_options += (
                    f" -af aloop=loop=-1:size={loop_size}:start={start_sample},"
                    f"{_loudnorm_filter()},{_atempo_filter(speed)},volume={max(0.0, min(float(volume), 6.0))}"
                )
            except (OSError, ValueError, wave.Error):
                local_options += f" -af {_loudnorm_filter()},{_atempo_filter(speed)},volume={max(0.0, min(float(volume), 6.0))}"
        else:
            local_options += f" -af {_loudnorm_filter()},{_atempo_filter(speed)},volume={max(0.0, min(float(volume), 6.0))}"
        return discord.FFmpegPCMAudio(
            stream_url, executable=ffmpeg_path, before_options='-nostdin',
            options=local_options)
    ffmpeg_path = get_ffmpeg_path()

    if not ffmpeg_path:

        raise RuntimeError(
            "FFmpegが見つかりません。"
        )

    parsed = urlparse(
        stream_url
    )

    scheme = (
        parsed.scheme.lower()
    )

    before_options = (
        "-nostdin "
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5 "
        "-reconnect_on_network_error 1 "
        "-reconnect_on_http_error "
        "403,404,429,500,502,503,504 "
        "-rw_timeout 15000000 "
    )

    header_blob = build_ffmpeg_header_blob(
        headers
    )

    escaped_header = (
        header_blob
        .replace(
            "\\",
            "\\\\"
        )
        .replace(
            '"',
            '\\"'
        )
    )

    before_options += (
        f'-headers "{escaped_header}" '
    )

    if scheme in (
        "http",
        "https"
    ):

        lower_url = stream_url.lower()

        if (
            ".m3u8" in lower_url
            or "m3u8" in lower_url
        ):

            before_options += (
                "-protocol_whitelist "
                "\"file,http,https,tcp,tls,crypto\" "
            )

    safe_volume = max(
        0.0,
        min(
            float(volume),
            2.0
        )
    )

    options = (
        "-vn "
        "-sn "
        "-dn "
        "-loglevel warning "
        "-af "
        f"{_loudnorm_filter()},{_atempo_filter(speed)},volume={safe_volume}"
    )

    return discord.FFmpegPCMAudio(
        stream_url,
        executable=ffmpeg_path,
        before_options=before_options,
        options=options
    )


# ============================================================
# EMBEDS
# ============================================================

def track_embed(
    track: Track
) -> discord.Embed:

    embed = discord.Embed(
        title="🎵 再生中",
        description=(
            f"**{truncate(track.title, 250)}**"
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="サービス",
        value=track.service,
        inline=True
    )

    embed.add_field(
        name="リクエスト",
        value=f"<@{track.requester_id}>",
        inline=True
    )

    if track.webpage_url:

        embed.add_field(
            name="配信元",
            value=(
                f"[音源を開く]"
                f"({track.webpage_url})"
            ),
            inline=True
        )

    if track.uploader:

        embed.add_field(
            name="投稿者",
            value=truncate(
                track.uploader,
                100
            ),
            inline=True
        )

    if track.thumbnail:

        try:

            embed.set_thumbnail(
                url=track.thumbnail
            )

        except Exception:
            pass

    return embed


# ============================================================
# MUSIC PLAYER
# ============================================================

class MusicPlayer:

    def __init__(
        self,
        guild_id: int
    ):

        self.guild_id = guild_id

        self.queue: list[Track] = []

        self.current: Optional[Track] = None

        self.voice: Optional[
            discord.VoiceClient
        ] = None

        self.volume = DEFAULT_VOLUME

        self.speed = 1.0

        self.loop_mode = "off"

        self.playing = False

        self.text_channel: Optional[
            discord.TextChannel
        ] = None

        self.last_activity = time.monotonic()

        self.disconnect_task: Optional[
            asyncio.Task
        ] = None

        self.audio_source: Optional[
            discord.PCMVolumeTransformer
        ] = None

        self.lock = asyncio.Lock()

        self.play_generation = 0

        # Public Music Panel
        self.panel_message: Optional[discord.Message] = None
        self.panel_view: Optional["MusicPanelView"] = None
        self.panel_task: Optional[asyncio.Task] = None

        # Progress tracking
        self.started_at: Optional[float] = None
        self.paused_at: Optional[float] = None
        self.paused_total: float = 0.0

    def touch(self):

        self.last_activity = (
            time.monotonic()
        )

    def enqueue(
        self,
        track: Track
    ) -> bool:

        if len(self.queue) >= MAX_QUEUE_SIZE:
            return False

        self.queue.append(
            track
        )

        return True

    def remove(
        self,
        index: int
    ) -> Optional[Track]:

        if index < 1:
            return None

        index -= 1

        if index >= len(self.queue):
            return None

        return self.queue.pop(
            index
        )

    def clear_queue(self):

        self.queue.clear()

    def shuffle_queue(self):

        random.shuffle(
            self.queue
        )


# ============================================================
# TTS / VOICE ANNOUNCEMENTS
# ============================================================

def guild_tts_enabled(guild_id: int) -> bool:
    return tts_master_by_guild.get(
        guild_id,
        TTS_MASTER_ENABLED
    )


def guild_user_join_enabled(guild_id: int) -> bool:
    return tts_user_join_by_guild.get(
        guild_id,
        TTS_USER_JOIN_DEFAULT
    )


def guild_dj_enabled(guild_id: int) -> bool:
    return tts_dj_by_guild.get(
        guild_id,
        TTS_DJ_DEFAULT
    )


def guild_radio_enabled(guild_id: int) -> bool:
    return tts_radio_by_guild.get(
        guild_id,
        TTS_RADIO_DEFAULT
    )


def tts_temp_path(guild_id: int) -> str:
    fd, path = tempfile.mkstemp(
        prefix=f"nexora_tts_{guild_id}_",
        suffix=".mp3"
    )
    os.close(fd)
    return path


async def generate_tts_file(
    text: str,
    path: str
):
    if edge_tts is None:
        raise RuntimeError(
            "edge-tts がインストールされていません。"
            "requirements.txt に edge-tts を追加してください。"
        )

    communicate = edge_tts.Communicate(
        text,
        TTS_VOICE,
        rate=TTS_RATE,
        volume=TTS_VOLUME
    )

    await communicate.save(path)


async def wait_for_voice_source(
    voice: discord.VoiceClient,
    source: discord.AudioSource
):
    done = asyncio.Event()
    callback_error: list[Optional[Exception]] = [None]

    def after(error):
        callback_error[0] = error
        try:
            bot.loop.call_soon_threadsafe(done.set)
        except Exception:
            pass

    voice.play(
        source,
        after=after
    )

    await done.wait()

    if callback_error[0]:
        raise callback_error[0]


async def speak_in_voice(
    player: "MusicPlayer",
    text: str,
    *,
    force: bool = False
) -> bool:
    """VCでTTSを1回読み上げます。再生中なら一時停止して終了後に再開します。"""

    if not player.voice:
        return False

    if not player.voice.is_connected():
        return False

    if not force and not guild_tts_enabled(player.guild_id):
        return False

    text = re.sub(r"\s+", " ", str(text)).strip()
    text = truncate(text, 300)
    if not text:
        return False

    # 同じサーバーでTTSを同時実行しない
    old_task = tts_tasks_by_guild.get(player.guild_id)
    if old_task and old_task is not asyncio.current_task() and not old_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(old_task), timeout=20)
        except Exception:
            pass

    tts_tasks_by_guild[player.guild_id] = asyncio.current_task()

    path = None
    was_playing = False
    was_paused = False

    try:
        voice = player.voice
        was_playing = voice.is_playing()
        was_paused = voice.is_paused()

        if was_playing:
            voice.pause()
            player.paused_at = time.monotonic()

        elif was_paused:
            # 一時停止中は音楽を壊さないため、TTSを重ねず終了
            return False

        path = tts_temp_path(player.guild_id)
        await asyncio.wait_for(
            generate_tts_file(text, path),
            timeout=20
        )

        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            raise RuntimeError("FFmpegが見つかりません。")

        source = discord.FFmpegPCMAudio(
            path,
            executable=ffmpeg_path,
            before_options="-nostdin",
            options="-vn -loglevel warning"
        )

        await wait_for_voice_source(
            voice,
            source
        )

        # 元の音楽へ戻す
        if was_playing and voice.is_connected():
            if player.paused_at is not None:
                player.paused_total += (
                    time.monotonic() - player.paused_at
                )
                player.paused_at = None
            voice.resume()

        return True

    except Exception as exc:
        remember_error(
            player.guild_id,
            Diagnostic(
                service="TTS",
                stage="音声読み上げ",
                target=text,
                error=f"{type(exc).__name__}: {exc}"
            )
        )
        return False

    finally:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

        current_task = tts_tasks_by_guild.get(player.guild_id)
        if current_task is asyncio.current_task():
            tts_tasks_by_guild.pop(player.guild_id, None)


async def delayed_voice_announce(
    player: "MusicPlayer",
    text: str,
    delay: float = 3.0
):
    try:
        await asyncio.sleep(delay)
        if player.voice and player.voice.is_connected():
            await speak_in_voice(player, text)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


def schedule_voice_announce(
    player: "MusicPlayer",
    text: str,
    delay: float = 3.0
):
    old = tts_tasks_by_guild.get(player.guild_id)
    if old and not old.done():
        old.cancel()

    task = asyncio.create_task(
        delayed_voice_announce(
            player,
            text,
            delay
        )
    )
    tts_tasks_by_guild[player.guild_id] = task
    return task


async def announce_disconnect(
    player: "MusicPlayer"
):
    if player.voice and player.voice.is_connected():
        await speak_in_voice(
            player,
            "NEXORA-musicBOTがVCから切断されます。"
        )


async def radio_add_next_track(
    player: "MusicPlayer"
):
    """ラジオモード時にSoundCloudから次の公開音源を1曲だけ探す。"""
    if not guild_radio_enabled(player.guild_id):
        return False

    query = radio_query_by_guild.get(player.guild_id)
    if not query and player.current:
        query = player.current.title

    if not query:
        return False

    info, diagnostics = await search_soundcloud(query)
    if not info:
        for diagnostic in diagnostics:
            remember_error(player.guild_id, diagnostic)
        return False

    entries = info.get("entries") if isinstance(info, dict) else None
    if not entries:
        entries = [info]

    candidates = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = entry.get("webpage_url") or entry.get("original_url")
        title = entry.get("title")
        if url and title:
            candidates.append(entry)

    if not candidates:
        return False

    current_url = player.current.webpage_url if player.current else ""
    filtered = [
        item for item in candidates
        if item.get("webpage_url") != current_url
    ] or candidates

    entry = random.choice(filtered)

    title = str(entry.get("title") or entry.get("track") or "NEXORA Radio")
    webpage_url = str(
        entry.get("webpage_url")
        or entry.get("original_url")
        or ""
    )
    if not webpage_url:
        return False

    duration = entry.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except Exception:
        duration = None

    track = Track(
        title=html.unescape(title),
        webpage_url=webpage_url,
        requester_id=0,
        requester_name="NEXORA Radio",
        duration=duration,
        thumbnail=entry.get("thumbnail"),
        uploader=str(entry.get("uploader")) if entry.get("uploader") else None,
        service="SoundCloud",
        original_query=query
    )

    return player.enqueue(track)


# ============================================================
# PLAYER GETTER
# ============================================================

def get_player(
    guild_id: int
) -> MusicPlayer:

    player = music_players.get(
        guild_id
    )

    if player is None:

        player = MusicPlayer(
            guild_id
        )

        music_players[guild_id] = player

    return player


# ============================================================
# VOICE
# ============================================================

async def ensure_voice(
    interaction: discord.Interaction,
    player: MusicPlayer
):

    if not interaction.guild:

        return (
            None,
            "サーバー内でのみ使用できます。"
        )

    member = interaction.user

    if not isinstance(
        member,
        discord.Member
    ):

        return (
            None,
            "Discord Member情報を取得できません。"
        )

    if (
        not member.voice
        or not member.voice.channel
    ):

        return (
            None,
            "先にボイスチャンネルへ参加してください。"
        )

    channel = member.voice.channel

    try:

        if player.voice:

            if player.voice.is_connected():

                if (
                    player.voice.channel.id
                    != channel.id
                ):

                    await player.voice.move_to(
                        channel
                    )

                player.touch()

                return (
                    player.voice,
                    None
                )

        player.voice = await channel.connect(
            reconnect=True
        )

        player.touch()

        # VC接続から3秒後に接続アナウンス
        schedule_voice_announce(
            player,
            voice_join_message(player.guild_id),
            3.0
        )

        return (
            player.voice,
            None
        )

    except Exception as exc:

        return (
            None,
            (
                "ボイス接続に失敗しました: "
                f"{type(exc).__name__}: {exc}"
            )
        )



# ============================================================
# PUBLIC MUSIC PANEL
# ============================================================

def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def get_elapsed_seconds(player: MusicPlayer) -> float:
    if player.started_at is None:
        return 0.0

    now = time.monotonic()
    paused_total = player.paused_total

    if player.paused_at is not None:
        paused_total += max(
            0.0,
            now - player.paused_at
        )

    return max(
        0.0,
        now - player.started_at - paused_total
    )


def make_progress_bar(
    elapsed: float,
    duration: Optional[float],
    length: int = 22
) -> str:
    if not duration or duration <= 0:
        return "🔘" + "─" * (length - 1)

    ratio = max(
        0.0,
        min(elapsed / duration, 1.0)
    )

    position = min(
        length - 1,
        int(ratio * (length - 1))
    )

    chars = ["─"] * length
    chars[position] = "🔘"

    return "".join(chars)


def music_panel_embed(player: MusicPlayer) -> discord.Embed:
    if not player.current:
        embed = discord.Embed(
            title=f"{BOT_EMOJI} NEXORA MUSIC PANEL",
            description=f"{LOADING_EMOJI} 現在再生中の曲はありません。",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="操作",
            value="▶️ Resume / ⏭️ Skip / ⏹️ Stop / 📜 List",
            inline=False
        )
        return embed

    track = player.current
    elapsed = get_elapsed_seconds(player)

    if player.voice and player.voice.is_paused():
        status = "⏸️ 一時停止中"
    elif player.voice and player.voice.is_playing():
        status = "▶️ 再生中"
    else:
        status = "⏹️ 停止"

    service_emoji = (
        SOUNDCLOUD_EMOJI
        if track.service == "SoundCloud"
        else "🎵"
    )

    embed = discord.Embed(
        title=f"{BOT_EMOJI} NEXORA MUSIC PANEL",
        description=(
            f"**{truncate(track.title, 250)}**\n"
            f"{service_emoji} `{track.service}` • {status}"
        ),
        color=discord.Color.blurple()
    )

    if track.duration:
        progress = make_progress_bar(
            elapsed,
            track.duration
        )
        time_text = (
            f"`{format_time(elapsed)}` "
            f"{progress} "
            f"`{format_time(track.duration)}`"
        )
    else:
        time_text = (
            f"`{format_time(elapsed)}` "
            f"{LINEAR_LOADING_EMOJI}"
        )

    embed.add_field(
        name="進行状況",
        value=time_text,
        inline=False
    )
    embed.add_field(
        name="音量",
        value=f"`{int(player.volume * 100)}%`",
        inline=True
    )
    embed.add_field(
        name="速度",
        value=f"`{player.speed:g}x`",
        inline=True
    )
    embed.add_field(
        name="ループ",
        value=f"`{player.loop_mode}`",
        inline=True
    )
    embed.add_field(
        name="キュー",
        value=f"`{len(player.queue)}曲`",
        inline=True
    )

    if track.uploader:
        embed.add_field(
            name="投稿者",
            value=truncate(track.uploader, 100),
            inline=True
        )

    if track.thumbnail:
        try:
            embed.set_thumbnail(url=track.thumbnail)
        except Exception:
            pass

    embed.set_footer(
        text="NEXORA Music • 公開Music Panel"
    )

    return embed


class MusicPanelView(discord.ui.View):

    def __init__(self, player: MusicPlayer):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(
        label="Resume",
        style=discord.ButtonStyle.primary,
        emoji="▶️"
    )
    async def resume_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        player = self.player

        if not player.voice:
            await interaction.response.send_message(
                embed=make_error_embed(
                    "再生できません",
                    "現在ボイスチャンネルに接続していません。"
                ),
                ephemeral=True
            )
            return

        if player.voice.is_paused():
            if player.paused_at is not None:
                player.paused_total += max(
                    0.0,
                    time.monotonic() - player.paused_at
                )
                player.paused_at = None

            player.voice.resume()
            player.touch()

            await interaction.response.send_message(
                embed=make_success_embed(
                    "再開",
                    "再生を再開しました。"
                ),
                ephemeral=True
            )

        elif player.voice.is_playing():
            player.voice.pause()
            player.paused_at = time.monotonic()
            player.touch()

            await interaction.response.send_message(
                embed=make_success_embed(
                    "一時停止",
                    "再生を一時停止しました。"
                ),
                ephemeral=True
            )

        else:
            await interaction.response.send_message(
                embed=make_error_embed(
                    "再生していません",
                    "現在再生中の音源がありません。"
                ),
                ephemeral=True
            )
            return

        await update_music_panel(player)

    @discord.ui.button(
        label="Skip",
        style=discord.ButtonStyle.secondary,
        emoji="⏭️"
    )
    async def skip_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        player = self.player

        if (
            not player.voice
            or not player.voice.is_playing()
        ):
            await interaction.response.send_message(
                embed=make_error_embed(
                    "スキップできません",
                    "現在再生中の音源がありません。"
                ),
                ephemeral=True
            )
            return

        title = (
            player.current.title
            if player.current
            else "現在の曲"
        )

        if player.loop_mode == "track":
            player.loop_mode = "off"

        player.touch()
        player.voice.stop()

        await interaction.response.send_message(
            embed=make_success_embed(
                "スキップ",
                f"**{truncate(title, 250)}** をスキップしました。"
            ),
            ephemeral=True
        )

    @discord.ui.button(
        label="Stop",
        style=discord.ButtonStyle.danger,
        emoji="⏹️"
    )
    async def stop_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        player = self.player

        player.play_generation += 1
        player.loop_mode = "off"
        player.clear_queue()
        player.current = None
        player.audio_source = None
        player.playing = False
        player.started_at = None
        player.paused_at = None
        player.paused_total = 0.0

        if player.voice:
            try:
                if (
                    player.voice.is_playing()
                    or player.voice.is_paused()
                ):
                    player.voice.stop()
            except Exception:
                pass

        player.touch()

        await interaction.response.send_message(
            embed=make_success_embed(
                "停止",
                "再生を停止し、キューを削除しました。"
            ),
            ephemeral=True
        )

        await update_music_panel(player)

    @discord.ui.button(
        label="List",
        style=discord.ButtonStyle.secondary,
        emoji="📜"
    )
    async def list_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        player = self.player

        embed = discord.Embed(
            title="📜 再生キュー",
            color=discord.Color.blurple()
        )

        if player.current:
            embed.add_field(
                name="🎵 現在再生中",
                value=truncate(
                    player.current.title,
                    1000
                ),
                inline=False
            )

        if not player.queue:
            embed.add_field(
                name="キュー",
                value="キューは空です。",
                inline=False
            )
        else:
            lines = []

            for index, track in enumerate(
                player.queue[:25],
                start=1
            ):
                lines.append(
                    f"`{index}.` "
                    f"{truncate(track.title, 80)} "
                    f"• `{track.service}`"
                )

            embed.add_field(
                name=f"次の曲 ({len(player.queue)}曲)",
                value=truncate(
                    "\n".join(lines),
                    1000
                ),
                inline=False
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


async def update_music_panel(player: MusicPlayer):
    if not player.panel_message:
        return

    try:
        if player.panel_view is None:
            player.panel_view = MusicPanelView(player)

        await player.panel_message.edit(
            embed=music_panel_embed(player),
            view=player.panel_view
        )
    except Exception:
        pass


async def music_panel_loop(player: MusicPlayer):
    try:
        while player.panel_message:
            await asyncio.sleep(2)
            await update_music_panel(player)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def send_music_panel(player: MusicPlayer):
    if not player.text_channel:
        return

    try:
        if player.panel_view is None:
            player.panel_view = MusicPanelView(player)

        if player.panel_message:
            await update_music_panel(player)
            return

        player.panel_message = await player.text_channel.send(
            embed=music_panel_embed(player),
            view=player.panel_view
        )

        if (
            not player.panel_task
            or player.panel_task.done()
        ):
            player.panel_task = asyncio.create_task(
                music_panel_loop(player)
            )

    except Exception:
        player.panel_message = None


# ============================================================
# PLAY NEXT
# ============================================================

async def play_next(
    player: MusicPlayer
):

    async with player.lock:

        if player.playing:
            return

        while True:

            player.touch()

            track: Optional[Track] = None

            # ================================================
            # 現在曲ループ
            # ================================================

            if (
                player.loop_mode == "track"
                and player.current
            ):

                track = player.current

            # ================================================
            # 通常キュー
            # ================================================

            elif player.queue:

                track = player.queue.pop(
                    0
                )

            else:

                player.current = None
                player.playing = False
                player.audio_source = None
                player.started_at = None
                player.paused_at = None
                player.paused_total = 0.0
                await update_music_panel(player)
                if player.voice and player.voice.channel:
                    try:
                        await player.voice.channel.edit(status="")
                    except (discord.Forbidden, discord.HTTPException, AttributeError):
                        pass

                return

            if not player.voice:

                player.current = None

                return

            if not player.voice.is_connected():

                player.current = None

                return

            try:

                # ============================================
                # 再生直前にURLを再取得
                # ============================================

                (
                    stream_url,
                    headers,
                    diagnostics
                ) = await resolve_stream(
                    track
                )

                if not stream_url:

                    if player.loop_mode == "track":

                        player.loop_mode = "off"

                        player.current = None

                    for diagnostic in diagnostics:

                        remember_error(
                            player.guild_id,
                            diagnostic
                        )

                    if player.text_channel:

                        embed = make_error_embed(
                            "再生できませんでした",
                            (
                                f"**{truncate(track.title, 200)}**\n\n"
                                "再生用ストリームの取得に失敗しました。"
                            ),
                            diagnostics
                        )

                        try:

                            await player.text_channel.send(
                                embed=embed
                            )

                        except Exception:
                            pass

                    continue

                # ============================================
                # FFmpeg source
                # ============================================

                ffmpeg_source = create_audio_source(
                    stream_url,
                    headers,
                    player.volume,
                    getattr(track, "kt3_loop_start", 0.0),
                    getattr(track, "kt3_loop_end", 0.0),
                    getattr(track, "kt3_extra_stem", None),
                    player.speed,
                )

                # VolumeTransformerで現在の音量を保持
                source = discord.PCMVolumeTransformer(
                    ffmpeg_source,
                    volume=player.volume
                )

                player.audio_source = source

                player.current = track
                player.started_at = time.monotonic()
                player.paused_at = None
                player.paused_total = 0.0

                player.playing = True

                # DJモード: 曲開始アナウンス
                if guild_dj_enabled(player.guild_id):
                    await speak_in_voice(
                        player,
                        f"次の曲、{track.title}を再生します。"
                    )

                player.play_generation += 1

                generation = (
                    player.play_generation
                )

                def after_play(
                    error
                ):

                    try:

                        asyncio.run_coroutine_threadsafe(
                            playback_finished(
                                player,
                                error,
                                generation
                            ),
                            bot.loop
                        )

                    except Exception:
                        pass

                player.voice.play(
                    source,
                    after=after_play
                )

                if player.text_channel:

                    try:

                        await player.text_channel.send(
                            embed=track_embed(
                                track
                            )
                        )

                    except Exception:
                        pass

                await send_music_panel(
                    player
                )

                return

            except Exception as exc:

                player.playing = False

                player.audio_source = None

                diagnostic = Diagnostic(
                    service=track.service,
                    stage="FFmpeg / Discord Voice",
                    target=(
                        track.webpage_url
                        or track.original_query
                    ),
                    error=(
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )
                )

                remember_error(
                    player.guild_id,
                    diagnostic
                )

                if player.text_channel:

                    embed = make_error_embed(
                        "音声再生エラー",
                        (
                            f"**{truncate(track.title, 200)}**\n\n"
                            "FFmpegまたはDiscord Voice側で"
                            "音声再生に失敗しました。"
                        ),
                        [diagnostic]
                    )

                    try:

                        await player.text_channel.send(
                            embed=embed
                        )

                    except Exception:
                        pass

                continue


# ============================================================
# PLAYBACK FINISHED
# ============================================================

async def playback_finished(
    player: MusicPlayer,
    error: Optional[Exception],
    generation: int
):

    # 古い再生イベントを無視
    if generation != player.play_generation:
        return

    player.playing = False

    player.audio_source = None

    player.touch()

    current_track = player.current
    if current_track and player.loop_mode == "off":
        for cleanup_path in getattr(current_track, "cleanup_paths", []):
            try: os.remove(cleanup_path)
            except OSError: pass
        getattr(current_track, "cleanup_paths", []).clear()
    if error:

        diagnostic = Diagnostic(
            service=(
                current_track.service
                if current_track
                else "Unknown"
            ),
            stage="Discord Voice callback",
            target=(
                current_track.webpage_url
                if current_track
                else ""
            ),
            error=(
                f"{type(error).__name__}: "
                f"{error}"
            )
        )

        remember_error(
            player.guild_id,
            diagnostic
        )

        if player.text_channel:

            try:

                await player.text_channel.send(
                    embed=make_error_embed(
                        "再生終了時エラー",
                        (
                            "Discord Voiceの再生コールバックから"
                            "エラーが返されました。"
                        ),
                        [diagnostic]
                    )
                )

            except Exception:
                pass

    # queue loopの場合、
    # 終了した曲をキューの最後へ戻す。
    if (
        player.loop_mode == "queue"
        and current_track
    ):

        player.queue.append(
            current_track
        )

    # DJモード: 曲終了アナウンス
    if current_track and guild_dj_enabled(player.guild_id) and not error:
        await speak_in_voice(
            player,
            f"{current_track.title}の再生が終了しました。"
        )

    # ラジオモード: キューが空なら次のSoundCloud公開音源を自動追加
    if not player.queue and guild_radio_enabled(player.guild_id):
        try:
            await radio_add_next_track(player)
        except Exception as exc:
            remember_error(
                player.guild_id,
                Diagnostic(
                    service="SoundCloud Radio",
                    stage="ラジオ次曲検索",
                    target=radio_query_by_guild.get(player.guild_id, ""),
                    error=f"{type(exc).__name__}: {exc}"
                )
            )

    await asyncio.sleep(
        0.5
    )

    await play_next(
        player
    )


# ============================================================
# AUTO DISCONNECT
# ============================================================

async def auto_disconnect_loop(
    player: MusicPlayer
):

    while True:

        await asyncio.sleep(
            30
        )

        try:

            if not player.voice:
                return

            if not player.voice.is_connected():
                return

            if player.playing:
                continue

            if player.queue:
                continue

            if auto_tts_channels_by_guild.get(player.guild_id, set()):
                continue

            elapsed = (
                time.monotonic()
                - player.last_activity
            )

            if elapsed >= AUTO_DISCONNECT_SECONDS:

                await announce_disconnect(player)

                try:

                    await player.voice.disconnect(
                        force=True
                    )

                except Exception:
                    pass

                player.voice = None

                player.current = None

                player.audio_source = None

                return

        except Exception:
            return


def ensure_disconnect_watcher(
    player: MusicPlayer
):

    task = player.disconnect_task

    if task and not task.done():
        return

    player.disconnect_task = (
        asyncio.create_task(
            auto_disconnect_loop(
                player
            )
        )
    )


# ============================================================
# SPOTIFY OEMBED
# ============================================================

async def spotify_oembed(
    url: str
):

    endpoint = (
        "https://open.spotify.com/oembed"
    )

    timeout = aiohttp.ClientTimeout(
        total=10
    )

    last_error = ""

    for attempt in range(2):

        try:

            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": USER_AGENT
                }
            ) as session:

                async with session.get(
                    endpoint,
                    params={
                        "url": url
                    }
                ) as response:

                    if response.status != 200:

                        last_error = (
                            f"Spotify oEmbed HTTP "
                            f"{response.status}"
                        )

                    else:

                        data = await response.json()

                        title = data.get(
                            "title"
                        )

                        if title:

                            return (
                                str(title),
                                []
                            )

                        last_error = (
                            "Spotify oEmbed responseに"
                            "titleがありません。"
                        )

        except Exception as exc:

            last_error = (
                f"{type(exc).__name__}: "
                f"{exc}"
            )

        if attempt == 0:

            await asyncio.sleep(
                1.5
            )

    diagnostic = Diagnostic(
        service="Spotify",
        stage="oEmbed",
        target=url,
        error=last_error
    )

    return (
        None,
        [diagnostic]
    )


# ============================================================
# SEARCH SOUNDCLOUD
# ============================================================

async def search_soundcloud(
    query: str
):

    query = query.strip()

    target = (
        query
        if is_url(query)
        else f"scsearch1:{query}"
    )

    return await extract_generic(
        target,
        "SoundCloud"
    )


# ============================================================
# SEARCH YOUTUBE
# ============================================================

async def search_youtube(
    query: str
):

    query = query.strip()

    target = (
        query
        if is_url(query)
        else f"ytsearch1:{query}"
    )

    return await extract_youtube(
        target
    )


# ============================================================
# PUBLIC SOURCE FALLBACK
#
# Spotifyの音声そのものを抽出するのではなく、
# Spotifyから取得した曲名を公開音源サービスで検索する。
# ============================================================

async def search_public_sources(
    title: str
):

    all_diagnostics: list[
        Diagnostic
    ] = []

    # ========================================================
    # 1. SoundCloud
    # ========================================================

    sc_info, sc_diagnostics = (
        await search_soundcloud(
            title
        )
    )

    all_diagnostics.extend(
        sc_diagnostics
    )

    if sc_info:

        return (
            sc_info,
            "SoundCloud",
            all_diagnostics
        )

    # ========================================================
    # 2. YouTube
    # ========================================================

    yt_info, yt_diagnostics = (
        await search_youtube(
            title
        )
    )

    all_diagnostics.extend(
        yt_diagnostics
    )

    if yt_info:

        return (
            yt_info,
            "YouTube",
            all_diagnostics
        )

    return (
        None,
        None,
        all_diagnostics
    )


async def search_spotify_public_source(title: str):
    """Spotifyの曲名はYouTubeを優先し、失敗時だけSoundCloudへ戻す。"""
    yt_info, diagnostics = await search_youtube(title)
    if yt_info:
        return yt_info, "YouTube", diagnostics
    sc_info, sc_diagnostics = await search_soundcloud(title)
    diagnostics.extend(sc_diagnostics)
    if sc_info:
        return sc_info, "SoundCloud", diagnostics
    return None, None, diagnostics


# ============================================================
# SEARCH SERVICE
# ============================================================

async def search_service(
    query: str,
    service: str
):

    query = query.strip()

    if service == "SoundCloud":

        info, diagnostics = (
            await search_soundcloud(
                query
            )
        )

        return (
            info,
            diagnostics
        )

    if service == "YouTube":

        info, diagnostics = (
            await search_youtube(
                query
            )
        )

        return (
            info,
            diagnostics
        )

    if service == "Spotify":

        # Spotify URLの場合
        if is_url(query):

            title, diagnostics = (
                await spotify_oembed(
                    query
                )
            )

            if not title:

                return (
                    None,
                    diagnostics
                )

            (
                info,
                actual_service,
                public_diagnostics
            ) = await search_spotify_public_source(
                title
            )

            diagnostics.extend(
                public_diagnostics
            )

            return (
                info,
                diagnostics
            )

        # Spotifyボタンに曲名が入った場合も
        # Spotify APIを使わず公開音源を検索する。
        (
            info,
            actual_service,
            diagnostics
        ) = await search_spotify_public_source(
            query
        )

        return (
            info,
            diagnostics
        )

    info, diagnostics = (
        await extract_generic(
            query,
            service
        )
    )

    return (
        info,
        diagnostics
    )


# ============================================================
# SOURCE BUTTON VIEW
# ============================================================

class SourceSelectView(
    discord.ui.View
):

    def __init__(
        self,
        query: str,
        requester: discord.Member,
        player: MusicPlayer,
        timeout: float = 60
    ):

        super().__init__(
            timeout=timeout
        )

        self.query = query

        self.requester = requester

        self.player = player

    async def handle_source(
        self,
        interaction: discord.Interaction,
        service: str
    ):

        if (
            interaction.user.id
            != self.requester.id
        ):

            await interaction.response.send_message(
                embed=make_error_embed(
                    "操作できません",
                    "この検索を開始したユーザーだけ操作できます。"
                ),
                ephemeral=True
            )

            return

        await interaction.response.defer()

        info, diagnostics = (
            await search_service(
                self.query,
                service
            )
        )

        if not info:

            for diagnostic in diagnostics:

                remember_error(
                    interaction.guild_id,
                    diagnostic
                )

            await interaction.edit_original_response(
                embed=make_error_embed(
                    f"{service} 検索失敗",
                    (
                        f"検索語: **{truncate(self.query, 250)}**"
                    ),
                    diagnostics
                ),
                view=None
            )

            return

        actual_service = service

        # Spotifyボタンなどで公開音源に
        # フォールバックした場合はサービスを推定。
        if service == "Spotify":

            if diagnostics:

                for diagnostic in reversed(
                    diagnostics
                ):

                    if diagnostic.success:

                        actual_service = (
                            diagnostic.service
                        )

                        break

            if actual_service == "Spotify":

                actual_service = "SoundCloud"

        track = info_to_track(
            info,
            self.requester,
            self.query,
            actual_service
        )

        if not track:

            diagnostic = Diagnostic(
                service=actual_service,
                stage="Track生成",
                target=self.query,
                error=(
                    "yt-dlpの結果からTrackを生成できませんでした。"
                )
            )

            await interaction.edit_original_response(
                embed=make_error_embed(
                    f"{actual_service} 取得失敗",
                    "曲情報をTrackへ変換できませんでした。",
                    [diagnostic]
                ),
                view=None
            )

            return

        if not self.player.enqueue(
            track
        ):

            await interaction.edit_original_response(
                embed=make_error_embed(
                    "キューがいっぱいです",
                    f"最大 {MAX_QUEUE_SIZE} 曲までです。"
                ),
                view=None
            )

            return

        self.player.text_channel = (
            interaction.channel
            if isinstance(
                interaction.channel,
                discord.TextChannel
            )
            else self.player.text_channel
        )

        ensure_disconnect_watcher(
            self.player
        )

        await interaction.edit_original_response(
            embed=make_success_embed(
                "キューに追加しました",
                (
                    f"**{truncate(track.title, 250)}**\n"
                    f"サービス: `{actual_service}`\n"
                    f"キュー: `{len(self.player.queue)}` 曲"
                )
            ),
            view=None
        )

        if not self.player.playing:

            await play_next(
                self.player
            )

    @discord.ui.button(
        label="SoundCloud",
        style=discord.ButtonStyle.primary,
        emoji="☁️"
    )
    async def soundcloud_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await self.handle_source(
            interaction,
            "SoundCloud"
        )

    @discord.ui.button(
        label="YouTube",
        style=discord.ButtonStyle.danger,
        emoji="▶️"
    )
    async def youtube_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await self.handle_source(
            interaction,
            "YouTube"
        )

    @discord.ui.button(
        label="Spotify",
        style=discord.ButtonStyle.success,
        emoji="🟢"
    )
    async def spotify_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await self.handle_source(
            interaction,
            "Spotify"
        )


# ============================================================
# PREFIX MUSIC COMMANDS / n!
# SoundCloudを基本音源として使用
# ============================================================
async def search_candidates(query: str, service: str):
    """YouTube/SoundCloudの検索結果上位5件を返す。"""
    prefix = "ytsearch5:" if service == "YouTube" else "scsearch5:"
    result = await ytdlp_extract(
        query if is_url(query) else prefix + query,
        base_ytdlp_options(flat=True)
    )
    info = result.get("info") or {}
    entries = [entry for entry in (info.get("entries") or []) if entry]
    return entries[:5], result


class SearchResultView(discord.ui.View):
    def __init__(self, ctx, query: str, service: str, candidates: list[dict]):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.query = query
        self.service = service
        self.candidates = candidates
        for index, candidate in enumerate(candidates):
            button = discord.ui.Button(
                label=f"{index + 1}. {truncate(candidate.get('title', 'Unknown'), 70)}",
                style=discord.ButtonStyle.primary,
                custom_id=f"search_result_{index}"
            )
            button.callback = self._make_callback(index)
            self.add_item(button)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message("検索を開始したユーザーだけ選択できます。", ephemeral=True)
                return
            await interaction.response.defer()
            candidate = self.candidates[index]
            url = candidate.get("webpage_url") or candidate.get("url") or ""
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
            if url:
                await prefix_play_service(self.ctx, url, self.service, allow_selection=False)
            else:
                await self.ctx.send("選択した検索結果のURLを取得できませんでした。")
        return callback


async def prefix_send_error(
    ctx: commands.Context,
    title: str,
    description: str,
    diagnostics: Optional[list[Diagnostic]] = None
):
    try:
        await ctx.send(
            embed=make_error_embed(
                title,
                description,
                diagnostics or []
            )
        )
    except Exception:
        pass


async def prefix_send_success(
    ctx: commands.Context,
    title: str,
    description: str
):
    try:
        await ctx.send(
            embed=make_success_embed(
                title,
                description
            )
        )
    except Exception:
        pass


async def prefix_ensure_voice(
    ctx: commands.Context,
    player: MusicPlayer
):
    if not ctx.guild:
        return None, "サーバー内でのみ使用できます。"

    member = ctx.author

    if not isinstance(member, discord.Member):
        return None, "Discord Member情報を取得できません。"

    if not member.voice or not member.voice.channel:
        return None, "先にボイスチャンネルへ参加してください。"

    channel = member.voice.channel

    try:
        if player.voice and player.voice.is_connected():
            if player.voice.channel and player.voice.channel.id == channel.id:
                return player.voice, None

            if player.voice.is_playing() or player.voice.is_paused():
                player.voice.stop()

            await player.voice.move_to(channel)
            return player.voice, None

        player.voice = await channel.connect()
        return player.voice, None

    except Exception as exc:
        return None, f"ボイスチャンネルへの接続に失敗しました: {type(exc).__name__}: {exc}"



# Attachment playback fix: media-v5
MEDIA_EXTENSIONS = {'.wav', '.wave', '.mp3', '.mp4', '.m4a', '.aac', '.flac',
                    '.ogg', '.oga', '.opus', '.wma', '.webm', '.mkv', '.mov', '.m4v', '.kt3'}
MEDIA_MAX_BYTES = 100 * 1024 * 1024

def media_extension(value):
    from urllib.parse import unquote
    return os.path.splitext(unquote(urlparse(value).path))[1].lower()

async def try_prefix_media(ctx, query, insert_next=False):
    """Return True when a media request was handled, including errors."""
    if not ctx.guild:
        return False
    query = query.strip().strip('<>')
    attachment = next((a for a in ctx.message.attachments
                       if os.path.splitext(a.filename)[1].lower() in MEDIA_EXTENSIONS), None)
    remote = is_url(query) and media_extension(query) in MEDIA_EXTENSIONS
    local = False
    if query and not is_url(query) and '://' not in query:
        local = os.path.splitext(query)[1].lower() in MEDIA_EXTENSIONS
    if not (attachment or remote or local):
        return False
    player = get_player(ctx.guild.id)
    player.text_channel = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else player.text_channel
    voice, error = await prefix_ensure_voice(ctx, player)
    if error:
        await prefix_send_error(ctx, 'ボイス接続エラー', error)
        return True
    downloaded = False
    path = None
    try:
        if len(player.queue) >= MAX_QUEUE_SIZE:
            raise ValueError('キューがいっぱいです。')
        if attachment or remote:
            if attachment and attachment.size > MEDIA_MAX_BYTES:
                raise ValueError('添付ファイルは100MiB以下にしてください。')
            name = attachment.filename if attachment else os.path.basename(urlparse(query).path)
            directory = os.path.abspath('nexora_media_downloads')
            os.makedirs(directory, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix=f'{ctx.guild.id}_', suffix=media_extension(name), dir=directory)
            os.close(fd)
            downloaded = True
            # Download the current attachment URL; do not convert URLs to filesystem paths.
            url = attachment.url if attachment else query
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise RuntimeError(f'HTTP {response.status}: ファイルを取得できません。添付し直してください。')
                    total = 0
                    with open(path, 'wb') as out:
                        async for chunk in response.content.iter_chunked(65536):
                            total += len(chunk)
                            if total > MEDIA_MAX_BYTES:
                                raise ValueError('ファイルは100MiB以下にしてください。')
                            out.write(chunk)
            if not os.path.getsize(path):
                raise ValueError('ファイルが空です。')
        else:
            # Only administrators may request server-local paths.
            if not ctx.author.guild_permissions.manage_guild:
                raise ValueError('サーバー内ファイルの指定にはサーバー管理権限が必要です。添付再生は利用できます。')
            path = os.path.abspath(os.path.expanduser(query))
            if not os.path.isfile(path):
                raise ValueError('指定したローカルファイルが見つかりません。')
            name = os.path.basename(path)
        track = Track(title=name, webpage_url='', requester_id=ctx.author.id,
                      requester_name=ctx.author.display_name, service='DownloadedFile' if downloaded else 'LocalFile',
                      stream_url=path, stream_headers={}, original_query=name)
        if downloaded:
            setattr(track, 'cleanup_paths', [path])
        if insert_next:
            player.queue.insert(0, track)
        elif not player.enqueue(track):
            raise ValueError('キューがいっぱいです。')
        ensure_disconnect_watcher(player)
        await prefix_send_success(ctx, 'ファイルを追加しました', truncate(name, 250))
        if not player.playing:
            await play_next(player)
    except Exception as exc:
        if downloaded and path and os.path.exists(path):
            os.remove(path)
        await prefix_send_error(ctx, 'ファイル再生エラー', safe_error_text(str(exc)))
    return True


async def prefix_play_service(
    ctx: commands.Context,
    query: str,
    service: str = "SoundCloud",
    allow_selection: bool = True
):
    if await try_prefix_media(ctx, query):
        return
    if not ctx.guild:
        await prefix_send_error(ctx, "使用できません", "サーバー内でのみ使用できます。")
        return

    query = query.strip()
    if not query:
        await prefix_send_error(ctx, "曲名がありません", "例: `n!p nightcore`\nURLも使用できます。")
        return

    player = get_player(ctx.guild.id)
    player.text_channel = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else player.text_channel

    voice, error = await prefix_ensure_voice(ctx, player)
    if error:
        await prefix_send_error(ctx, "ボイス接続エラー", error)
        return

    ensure_disconnect_watcher(player)

    if allow_selection and service in {"YouTube", "SoundCloud"} and not is_url(query):
        candidates, result = await search_candidates(query, service)
        if len(candidates) > 1:
            embed = discord.Embed(
                title=f"🔎 {service}検索結果",
                description=f"**{truncate(query, 250)}**\n再生する項目を選択してください。",
                color=discord.Color.blurple()
            )
            for index, candidate in enumerate(candidates, start=1):
                duration = candidate.get("duration")
                duration_text = format_time(float(duration)) if duration else "--:--"
                embed.add_field(
                    name=f"{index}. {truncate(candidate.get('title', 'Unknown'), 200)}",
                    value=f"`{duration_text}` • {truncate(candidate.get('uploader', '') or '', 80)}",
                    inline=False
                )
            await ctx.send(embed=embed, view=SearchResultView(ctx, query, service, candidates))
            return

    info, diagnostics = await search_service(query, service)

    if not info:
        await prefix_send_error(
            ctx,
            f"{service}で見つかりませんでした",
            f"検索: **{truncate(query, 250)}**\n\n{service}上で公開されている音源を指定してください。",
            diagnostics
        )
        return

    track = info_to_track(
        info,
        ctx.author,
        query,
        "SoundCloud"
    )

    if not track:
        await prefix_send_error(
            ctx,
            "曲情報取得失敗",
            f"{service}の曲情報からTrackを生成できませんでした。",
            diagnostics
        )
        return

    if not player.enqueue(track):
        await prefix_send_error(
            ctx,
            "キューがいっぱいです",
            f"最大 {MAX_QUEUE_SIZE} 曲までです。"
        )
        return

    position = len(player.queue)

    if player.playing:
        await prefix_send_success(
            ctx,
            "キューに追加しました",
            f"{'☁️' if service == 'SoundCloud' else '▶️'} **{truncate(track.title, 250)}**\n順番: `{position}`\n{service}"
        )
    else:
        await prefix_send_success(
            ctx,
            "再生を開始します",
            f"{'☁️' if service == 'SoundCloud' else '▶️'} **{truncate(track.title, 250)}**\n{service}から再生します。"
        )
        await play_next(player)


@bot.command(name="p", aliases=["play"])
async def prefix_play(ctx: commands.Context, *, query: str = ""):
    """n!p [soundcloud|youtube] 曲名/URL または n!p panel"""
    query = query.strip()

    if query.lower() == "panel":
        if not ctx.guild:
            await prefix_send_error(ctx, "使用できません", "サーバー内でのみ使用できます。")
            return

        player = get_player(ctx.guild.id)
        player.text_channel = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else player.text_channel
        await send_music_panel(player)
        return

    service = "SoundCloud"
    parts = query.split(maxsplit=1)
    if parts and parts[0].lower() in {"youtube", "yt", "ユーチューブ"}:
        service = "YouTube"
        query = parts[1] if len(parts) > 1 else ""
    elif parts and parts[0].lower() in {"tiktok", "tik", "ティックトック"}:
        service = "TikTok"
        query = parts[1] if len(parts) > 1 else ""
    elif parts and parts[0].lower() in {"spotify", "sp", "スポティファイ"}:
        service = "Spotify"
        query = parts[1] if len(parts) > 1 else ""
    elif parts and parts[0].lower() in {"soundcloud", "sc", "サウンドクラウド"}:
        service = "SoundCloud"
        query = parts[1] if len(parts) > 1 else ""
    elif is_url(query) and ("youtube.com" in query.lower() or "youtu.be" in query.lower()):
        service = "YouTube"
    elif is_url(query) and "tiktok.com" in query.lower():
        service = "TikTok"
    elif is_url(query) and "spotify.com" in query.lower():
        service = "Spotify"

    await prefix_play_service(ctx, query, service)


@bot.command(name="s", aliases=["skip"])
async def prefix_skip(ctx: commands.Context):
    if not ctx.guild:
        await prefix_send_error(ctx, "使用できません", "サーバー内でのみ使用できます。")
        return

    player = get_player(ctx.guild.id)

    if not player.voice or not player.voice.is_playing():
        await prefix_send_error(ctx, "再生していません", "スキップできる曲がありません。")
        return

    title = player.current.title if player.current else "現在の曲"

    if player.loop_mode == "track":
        player.loop_mode = "off"

    player.touch()
    player.voice.stop()

    await prefix_send_success(ctx, "スキップ", f"**{truncate(title, 250)}** をスキップしました。")


@bot.command(name="j", aliases=["join"])
async def prefix_join(ctx: commands.Context):
    if not ctx.guild:
        await prefix_send_error(ctx, "使用できません", "サーバー内でのみ使用できます。")
        return

    player = get_player(ctx.guild.id)
    player.text_channel = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else player.text_channel

    voice, error = await prefix_ensure_voice(ctx, player)
    if error:
        await prefix_send_error(ctx, "参加失敗", error)
        return

    ensure_disconnect_watcher(player)
    await prefix_send_success(ctx, "参加しました", f"ボイスチャンネル **{voice.channel.name}** に参加しました。")


@bot.command(name="b", aliases=["bye", "leave"])
async def prefix_bye(ctx: commands.Context):
    if not ctx.guild:
        await prefix_send_error(ctx, "使用できません", "サーバー内でのみ使用できます。")
        return

    player = get_player(ctx.guild.id)
    player.play_generation += 1
    player.loop_mode = "off"
    player.clear_queue()
    player.current = None
    player.playing = False
    player.audio_source = None
    player.started_at = None
    player.paused_at = None
    player.paused_total = 0.0

    if player.voice:
        try:
            await player.voice.disconnect(force=True)
        except Exception:
            pass
        player.voice = None

    player.touch()
    await prefix_send_success(ctx, "退出しました", "ボイスチャンネルから退出し、キューも削除しました。")
    await update_music_panel(player)


@bot.command(name="q", aliases=["queue"])
async def prefix_queue(ctx: commands.Context):
    if not ctx.guild:
        await prefix_send_error(ctx, "使用できません", "サーバー内でのみ使用できます。")
        return

    player = get_player(ctx.guild.id)
    embed = discord.Embed(
        title=f"{BOT_EMOJI} NEXORA MUSIC QUEUE",
        description="☁️ SoundCloud キュー",
        color=discord.Color.blurple()
    )

    if player.current:
        embed.add_field(
            name="▶️ NOW",
            value=f"**{truncate(player.current.title, 180)}**",
            inline=False
        )

    if player.queue:
        lines = []
        for index, track in enumerate(player.queue[:20], start=1):
            lines.append(f"`{index:02d}` ☁️ {truncate(track.title, 120)}")
        if len(player.queue) > 20:
            lines.append(f"…ほか {len(player.queue) - 20} 曲")
        embed.add_field(name="📜 QUEUE", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="📜 QUEUE", value="キューは空です。", inline=False)

    await ctx.send(embed=embed)


@bot.command(name="np", aliases=["nowplaying"])
async def prefix_nowplaying(ctx: commands.Context):
    if not ctx.guild:
        await prefix_send_error(ctx, "使用できません", "サーバー内でのみ使用できます。")
        return

    player = get_player(ctx.guild.id)
    player.text_channel = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else player.text_channel
    await send_music_panel(player)


@bot.command(name="pause")
async def prefix_pause(ctx: commands.Context):
    if not ctx.guild:
        return
    player = get_player(ctx.guild.id)
    if not player.voice or not player.voice.is_playing():
        await prefix_send_error(ctx, "一時停止できません", "現在再生中の曲がありません。")
        return
    player.voice.pause()
    player.paused_at = time.monotonic()
    await update_music_panel(player)
    await prefix_send_success(ctx, "一時停止", "再生を一時停止しました。")


@bot.command(name="resume", aliases=["r"])
async def prefix_resume(ctx: commands.Context):
    if not ctx.guild:
        return
    player = get_player(ctx.guild.id)
    if not player.voice or not player.voice.is_paused():
        await prefix_send_error(ctx, "再開できません", "一時停止中の曲がありません。")
        return
    if player.paused_at:
        player.paused_total += time.monotonic() - player.paused_at
        player.paused_at = None
    player.voice.resume()
    await update_music_panel(player)
    await prefix_send_success(ctx, "再開", "再生を再開しました。")


@bot.command(name="v", aliases=["volume"])
async def prefix_volume(ctx: commands.Context, value: str = ""):
    if not ctx.guild:
        return
    try:
        volume = float(value)
    except Exception:
        await prefix_send_error(ctx, "音量指定エラー", "例: `n!v 50`")
        return
    if volume > 100:
        volume = volume / 100.0
    if not 0 <= volume <= 6:
        await prefix_send_error(ctx, "音量範囲エラー", "0～600 の範囲で指定してください。")
        return
    player = get_player(ctx.guild.id)
    player.volume = volume
    if player.audio_source:
        try:
            player.audio_source.volume = volume
        except Exception:
            pass
    await update_music_panel(player)
    await prefix_send_success(ctx, "音量変更", f"音量を **{round(volume * 100)}%** にしました。")


@bot.command(name="speed", aliases=["spd"])
async def prefix_speed(ctx: commands.Context, value: str = ""):
    if not ctx.guild:
        return
    try:
        speed = float(value)
    except Exception:
        await prefix_send_error(ctx, "速度指定エラー", "例: `n!speed 1.25`（0.5～2.0）")
        return
    if not 0.5 <= speed <= 2.0:
        await prefix_send_error(ctx, "速度範囲エラー", "0.5～2.0 の範囲で指定してください。")
        return
    player = get_player(ctx.guild.id)
    player.speed = speed
    if player.voice and (player.voice.is_playing() or player.voice.is_paused()):
        player.voice.stop()
        await prefix_send_success(ctx, "再生速度変更", f"速度を **{speed:g}x** にしました。現在の曲を速度変更して再生します。")
    else:
        await prefix_send_success(ctx, "再生速度変更", f"速度を **{speed:g}x** にしました。次の曲から適用します。")
    await update_music_panel(player)


@bot.command(name="loop")
async def prefix_loop(ctx: commands.Context, mode: str = ""):
    if not ctx.guild:
        return
    mode = mode.lower().strip()
    aliases = {"off": "off", "none": "off", "track": "track", "one": "track", "queue": "queue", "all": "queue"}
    if mode not in aliases:
        await prefix_send_error(ctx, "ループ指定エラー", "`n!loop off` / `n!loop track` / `n!loop queue`")
        return
    player = get_player(ctx.guild.id)
    player.loop_mode = aliases[mode]
    await update_music_panel(player)
    await prefix_send_success(ctx, "ループ変更", f"ループ: **{player.loop_mode}**")


@bot.command(name="clear", aliases=["cl"])
async def prefix_clear(ctx: commands.Context):
    if not ctx.guild:
        return
    player = get_player(ctx.guild.id)
    count = len(player.queue)
    player.clear_queue()
    await update_music_panel(player)
    await prefix_send_success(ctx, "キュー削除", f"キューから **{count}曲** 削除しました。現在再生中の曲は維持します。")


@bot.command(name="rm", aliases=["remove"])
async def prefix_remove(ctx: commands.Context, index: str = ""):
    if not ctx.guild:
        return
    try:
        number = int(index)
    except Exception:
        await prefix_send_error(ctx, "削除番号エラー", "例: `n!rm 2`")
        return
    player = get_player(ctx.guild.id)
    if number < 1 or number > len(player.queue):
        await prefix_send_error(ctx, "削除できません", f"1～{len(player.queue)} の番号を指定してください。")
        return
    track = player.queue.pop(number - 1)
    await update_music_panel(player)
    await prefix_send_success(ctx, "キューから削除", f"**{truncate(track.title, 200)}** を削除しました。")


@bot.command(name="sh", aliases=["shuffle"])
async def prefix_shuffle(ctx: commands.Context):
    if not ctx.guild:
        return
    player = get_player(ctx.guild.id)
    if len(player.queue) < 2:
        await prefix_send_error(ctx, "シャッフルできません", "キューに2曲以上必要です。")
        return
    player.shuffle()
    await update_music_panel(player)
    await prefix_send_success(ctx, "シャッフル", "キューをシャッフルしました。")


@bot.command(name="musichelp")
async def prefix_music_help(ctx: commands.Context):
    embed = discord.Embed(
        title=f"{BOT_EMOJI} NEXORA MUSIC HELP",
        description="SoundCloud中心の軽量音楽コマンドです。",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="🎵 基本",
        value=(
            "`n!p 曲名 / URL` 再生\n"
            "`n!p panel` 公開パネル\n"
            "`n!s` スキップ\n"
            "`n!j` VC参加\n"
            "`n!b` VC退出・キュー削除"
        ),
        inline=False
    )
    embed.add_field(
        name="🛠️ 操作",
        value=(
            "`n!q` キュー\n"
            "`n!np` 現在曲/パネル\n"
            "`n!pause` / `n!r` 一時停止・再開\n"
            "`n!v 50` 音量\n"
            "`n!loop off|track|queue`\n"
            "`n!rm 2` / `n!clear` / `n!sh`\n"
            "`n!tts on|off` / `n!vcnotify on|off`\n"
            "`n!dj on|off` / `n!say 文章` / `n!radio on|off [テーマ]`"
        ),
        inline=False
    )
    embed.add_field(
        name="☁️ 音源",
        value="Prefix再生はSoundCloudを使用します。YouTubeのbot判定による失敗を避ける設計です。",
        inline=False
    )
    await ctx.send(embed=embed)


# ============================================================
# VOICE / TTS COMMANDS
# ============================================================

@bot.tree.command(
    name="tts",
    description="VC読み上げ機能をON/OFFします"
)
@app_commands.describe(mode="on または off")
async def tts_command(
    interaction: discord.Interaction,
    mode: str
):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=make_error_embed("使用できません", "サーバー内で使用してください。"),
            ephemeral=True
        )
        return

    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await interaction.response.send_message(
            embed=make_error_embed("指定エラー", "`on` または `off` を指定してください。"),
            ephemeral=True
        )
        return

    enabled = mode == "on"
    tts_master_by_guild[interaction.guild.id] = enabled
    await interaction.response.send_message(
        embed=make_success_embed(
            "TTS設定変更",
            f"VC読み上げを **{'ON' if enabled else 'OFF'}** にしました。"
        )
    )


@bot.tree.command(
    name="vcnotify",
    description="ユーザーのVC参加読み上げをON/OFFします"
)
@app_commands.describe(mode="on または off")
async def vcnotify_command(
    interaction: discord.Interaction,
    mode: str
):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=make_error_embed("使用できません", "サーバー内で使用してください。"),
            ephemeral=True
        )
        return

    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await interaction.response.send_message(
            embed=make_error_embed("指定エラー", "`on` または `off` を指定してください。"),
            ephemeral=True
        )
        return

    enabled = mode == "on"
    tts_user_join_by_guild[interaction.guild.id] = enabled
    await interaction.response.send_message(
        embed=make_success_embed(
            "VC参加通知設定",
            f"ユーザーのVC参加読み上げを **{'ON' if enabled else 'OFF'}** にしました。"
        )
    )


@bot.tree.command(
    name="dj",
    description="曲開始・終了のDJ読み上げをON/OFFします"
)
@app_commands.describe(mode="on または off")
async def dj_command(
    interaction: discord.Interaction,
    mode: str
):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=make_error_embed("使用できません", "サーバー内で使用してください。"),
            ephemeral=True
        )
        return

    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await interaction.response.send_message(
            embed=make_error_embed("指定エラー", "`on` または `off` を指定してください。"),
            ephemeral=True
        )
        return

    enabled = mode == "on"
    tts_dj_by_guild[interaction.guild.id] = enabled
    await interaction.response.send_message(
        embed=make_success_embed(
            "DJモード",
            f"曲の開始・終了アナウンスを **{'ON' if enabled else 'OFF'}** にしました。"
        )
    )


@bot.tree.command(
    name="say",
    description="VCで指定した文章を読み上げます"
)
@app_commands.describe(text="読み上げる文章")
async def say_command(
    interaction: discord.Interaction,
    text: str
):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=make_error_embed("使用できません", "サーバー内で使用してください。"),
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    player = get_player(interaction.guild.id)
    voice, error = await ensure_voice(interaction, player)
    if error:
        await interaction.edit_original_response(
            embed=make_error_embed("読み上げ失敗", error)
        )
        return

    ok = await speak_in_voice(player, text, force=True)
    if not ok:
        await interaction.edit_original_response(
            embed=make_error_embed(
                "読み上げ失敗",
                "TTS音声を生成またはVCで再生できませんでした。requirements.txtのedge-ttsも確認してください。"
            )
        )
        return

    await interaction.edit_original_response(
        embed=make_success_embed(
            "読み上げ完了",
            f"**{truncate(text, 250)}** を読み上げました。"
        )
    )


@bot.tree.command(
    name="radio",
    description="SoundCloudラジオモードをON/OFFします"
)
@app_commands.describe(
    mode="on または off",
    query="ラジオの検索テーマ。例: J-POP"
)
async def radio_command(
    interaction: discord.Interaction,
    mode: str,
    query: str = ""
):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=make_error_embed("使用できません", "サーバー内で使用してください。"),
            ephemeral=True
        )
        return

    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await interaction.response.send_message(
            embed=make_error_embed("指定エラー", "`on` または `off` を指定してください。"),
            ephemeral=True
        )
        return

    guild_id = interaction.guild.id
    enabled = mode == "on"
    tts_radio_by_guild[guild_id] = enabled

    if query.strip():
        radio_query_by_guild[guild_id] = truncate(query.strip(), 200)

    description = f"SoundCloudラジオを **{'ON' if enabled else 'OFF'}** にしました。"
    if enabled:
        seed = radio_query_by_guild.get(guild_id)
        if seed:
            description += f"\n検索テーマ: **{seed}**"
        else:
            description += "\n現在の曲名を基準に次の曲を自動検索します。"

    await interaction.response.send_message(
        embed=make_success_embed(
            "ラジオモード",
            description
        )
    )


# ============================================================
# PREFIX VOICE / TTS COMMANDS
# ============================================================

@bot.command(name="tts")
async def prefix_tts(ctx: commands.Context, mode: str = ""):
    if not ctx.guild:
        return
    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await prefix_send_error(ctx, "指定エラー", "例: `n!tts on` / `n!tts off`")
        return
    enabled = mode == "on"
    tts_master_by_guild[ctx.guild.id] = enabled
    await prefix_send_success(ctx, "TTS設定変更", f"VC読み上げを **{'ON' if enabled else 'OFF'}** にしました。")


@bot.command(name="vcnotify")
async def prefix_vcnotify(ctx: commands.Context, mode: str = ""):
    if not ctx.guild:
        return
    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await prefix_send_error(ctx, "指定エラー", "例: `n!vcnotify on` / `n!vcnotify off`")
        return
    enabled = mode == "on"
    tts_user_join_by_guild[ctx.guild.id] = enabled
    await prefix_send_success(ctx, "VC参加通知設定", f"ユーザーのVC参加読み上げを **{'ON' if enabled else 'OFF'}** にしました。")


@bot.command(name="dj")
async def prefix_dj(ctx: commands.Context, mode: str = ""):
    if not ctx.guild:
        return
    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await prefix_send_error(ctx, "指定エラー", "例: `n!dj on` / `n!dj off`")
        return
    enabled = mode == "on"
    tts_dj_by_guild[ctx.guild.id] = enabled
    await prefix_send_success(ctx, "DJモード", f"曲の開始・終了アナウンスを **{'ON' if enabled else 'OFF'}** にしました。")


@bot.command(name="say")
async def prefix_say(ctx: commands.Context, *, text: str = ""):
    if not ctx.guild:
        return
    if not text.strip():
        await prefix_send_error(ctx, "文章がありません", "例: `n!say こんにちは` ")
        return
    player = get_player(ctx.guild.id)
    voice, error = await prefix_ensure_voice(ctx, player)
    if error:
        await prefix_send_error(ctx, "読み上げ失敗", error)
        return
    ok = await speak_in_voice(player, text, force=True)
    if not ok:
        await prefix_send_error(ctx, "読み上げ失敗", "TTS音声を生成またはVCで再生できませんでした。")
        return
    await prefix_send_success(ctx, "読み上げ完了", f"**{truncate(text, 250)}** を読み上げました。")


@bot.command(name="radio")
async def prefix_radio(ctx: commands.Context, mode: str = "", *, query: str = ""):
    if not ctx.guild:
        return
    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await prefix_send_error(ctx, "指定エラー", "例: `n!radio on J-POP` / `n!radio off`")
        return
    enabled = mode == "on"
    tts_radio_by_guild[ctx.guild.id] = enabled
    if query.strip():
        radio_query_by_guild[ctx.guild.id] = truncate(query.strip(), 200)
    desc = f"SoundCloudラジオを **{'ON' if enabled else 'OFF'}** にしました。"
    if enabled and ctx.guild.id in radio_query_by_guild:
        desc += f"\n検索テーマ: **{radio_query_by_guild[ctx.guild.id]}**"
    await prefix_send_success(ctx, "ラジオモード", desc)


# ============================================================
# COMMAND 1 /play
# ============================================================

@bot.tree.command(
    name="play",
    description="曲名またはURLを再生します"
)
@app_commands.describe(
    query="曲名または対応サービスのURL"
)
async def play(
    interaction: discord.Interaction,
    query: str
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    await interaction.response.defer()

    player = get_player(
        interaction.guild.id
    )

    player.text_channel = (
        interaction.channel
        if isinstance(
            interaction.channel,
            discord.TextChannel
        )
        else player.text_channel
    )

    voice, error = await ensure_voice(
        interaction,
        player
    )

    if error:

        await interaction.edit_original_response(
            embed=make_error_embed(
                "ボイスチャンネルエラー",
                error
            )
        )

        return

    query = query.strip()

    if not query:

        await interaction.edit_original_response(
            embed=make_error_embed(
                "検索語がありません",
                "曲名またはURLを指定してください。"
            )
        )

        return

    # ========================================================
    # URL
    # ========================================================

    if is_url(query):

        service = detect_service(
            query
        )

        # ====================================================
        # Spotify URL
        # ====================================================

        if service == "Spotify":

            title, spotify_diagnostics = (
                await spotify_oembed(
                    query
                )
            )

            if not title:

                for diagnostic in spotify_diagnostics:

                    remember_error(
                        interaction.guild.id,
                        diagnostic
                    )

                await interaction.edit_original_response(
                    embed=make_error_embed(
                        "Spotify取得失敗",
                        (
                            "Spotifyの公開メタデータ取得に失敗しました。"
                        ),
                        spotify_diagnostics
                    )
                )

                return

            # =================================================
            # Spotify → SoundCloud → YouTube
            # =================================================

            (
                info,
                actual_service,
                public_diagnostics
            ) = await search_public_sources(
                title
            )

            all_diagnostics = (
                spotify_diagnostics
                + public_diagnostics
            )

            if not info:

                for diagnostic in all_diagnostics:

                    remember_error(
                        interaction.guild.id,
                        diagnostic
                    )

                await interaction.edit_original_response(
                    embed=make_error_embed(
                        "Spotify音源の検索失敗",
                        (
                            f"Spotify曲名: "
                            f"**{truncate(title, 250)}**\n\n"
                            "SoundCloudとYouTubeの公開音源を"
                            "順番に検索しましたが、再生可能な音源を"
                            "取得できませんでした。"
                        ),
                        all_diagnostics
                    )
                )

                return

            service = actual_service

            diagnostics = all_diagnostics

        # ====================================================
        # YouTube URL
        # ====================================================

        elif service == "YouTube":

            info, diagnostics = (
                await extract_youtube(
                    query
                )
            )

        # ====================================================
        # その他URL
        # ====================================================

        else:

            info, diagnostics = (
                await extract_generic(
                    query,
                    service
                )
            )

        # ====================================================
        # URL取得失敗
        # ====================================================

        if not info:

            for diagnostic in diagnostics:

                remember_error(
                    interaction.guild.id,
                    diagnostic
                )

            await interaction.edit_original_response(
                embed=make_error_embed(
                    f"{service} 取得失敗",
                    (
                        f"URL: `{safe_target(query)}`"
                    ),
                    diagnostics
                )
            )

            return

        # ====================================================
        # Track生成
        # ====================================================

        track = info_to_track(
            info,
            interaction.user,
            query,
            service
        )

        if not track:

            diagnostic = Diagnostic(
                service=service,
                stage="Track生成",
                target=query,
                error=(
                    "yt-dlpから曲情報を取得しましたが"
                    "Trackを生成できませんでした。"
                )
            )

            await interaction.edit_original_response(
                embed=make_error_embed(
                    "曲情報の変換失敗",
                    "曲情報をTrackへ変換できませんでした。",
                    [diagnostic]
                )
            )

            return

        # ====================================================
        # Queue
        # ====================================================

        if not player.enqueue(
            track
        ):

            await interaction.edit_original_response(
                embed=make_error_embed(
                    "キューがいっぱいです",
                    f"最大 {MAX_QUEUE_SIZE} 曲までです。"
                )
            )

            return

        ensure_disconnect_watcher(
            player
        )

        await interaction.edit_original_response(
            embed=make_success_embed(
                "キューに追加しました",
                (
                    f"**{truncate(track.title, 250)}**\n"
                    f"サービス: `{service}`\n"
                    f"キュー: `{len(player.queue)}` 曲"
                )
            )
        )

        if not player.playing:

            await play_next(
                player
            )

        return

    # ========================================================
    # 通常の曲名検索
    # ========================================================

    embed = discord.Embed(
        title="🎵 再生元を選択",
        description=(
            f"検索: **{truncate(query, 500)}**\n\n"
            "使用する公開音源サービスを選択してください。"
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="☁️ SoundCloud",
        value="SoundCloudから検索",
        inline=True
    )

    embed.add_field(
        name="▶️ YouTube",
        value="YouTubeから検索",
        inline=True
    )

    embed.add_field(
        name="🟢 Spotify",
        value="公開音源へフォールバック",
        inline=True
    )

    view = SourceSelectView(
        query=query,
        requester=interaction.user,
        player=player
    )

    await interaction.edit_original_response(
        embed=embed,
        view=view
    )


# ============================================================
# COMMAND 2 /source
# ============================================================

@bot.tree.command(
    name="source",
    description="指定したサービスで曲を検索します"
)
@app_commands.describe(
    service="検索サービス",
    query="曲名またはURL"
)
@app_commands.choices(
    service=[
        app_commands.Choice(
            name="SoundCloud",
            value="SoundCloud"
        ),
        app_commands.Choice(
            name="YouTube",
            value="YouTube"
        ),
    ]
)
async def source(
    interaction: discord.Interaction,
    service: app_commands.Choice[str],
    query: str
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    await interaction.response.defer()

    player = get_player(
        interaction.guild.id
    )

    voice, error = await ensure_voice(
        interaction,
        player
    )

    if error:

        await interaction.edit_original_response(
            embed=make_error_embed(
                "ボイス接続エラー",
                error
            )
        )

        return

    info, diagnostics = (
        await search_service(
            query,
            service.value
        )
    )

    if not info:

        await interaction.edit_original_response(
            embed=make_error_embed(
                f"{service.value}検索失敗",
                f"検索: **{truncate(query, 250)}**",
                diagnostics
            )
        )

        return

    track = info_to_track(
        info,
        interaction.user,
        query,
        service.value
    )

    if not track:

        await interaction.edit_original_response(
            embed=make_error_embed(
                "曲情報取得失敗",
                "Trackを生成できませんでした。",
                diagnostics
            )
        )

        return

    if not player.enqueue(
        track
    ):

        await interaction.edit_original_response(
            embed=make_error_embed(
                "キューがいっぱいです",
                f"最大 {MAX_QUEUE_SIZE} 曲までです。"
            )
        )

        return

    player.text_channel = (
        interaction.channel
        if isinstance(
            interaction.channel,
            discord.TextChannel
        )
        else player.text_channel
    )

    ensure_disconnect_watcher(
        player
    )

    await interaction.edit_original_response(
        embed=make_success_embed(
            "追加しました",
            (
                f"**{truncate(track.title, 250)}**\n"
                f"サービス: `{service.value}`"
            )
        )
    )

    if not player.playing:

        await play_next(
            player
        )


# ============================================================
# COMMAND 3 /sources
# ============================================================

@bot.tree.command(
    name="sources",
    description="対応している音源サービスを表示します"
)
async def sources(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="🌐 NEXORA 対応音源",
        description=(
            "yt-dlpで取得可能な公開音源を対象にしています。\n"
            "サービス側の公開状態・地域・アクセス制限などにより、"
            "すべての音源が常に再生できるとは限りません。"
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="☁️ SoundCloud",
        value="曲名検索 / URL",
        inline=True
    )

    embed.add_field(
        name="▶️ YouTube",
        value="曲名検索 / URL",
        inline=True
    )

    embed.add_field(
        name="🟢 Spotify",
        value=(
            "公開メタデータ取得後、"
            "SoundCloud → YouTubeで検索"
        ),
        inline=True
    )

    embed.add_field(
        name="🔗 URL対応",
        value=(
            "Bandcamp / Mixcloud / Vimeo / "
            "Dailymotion / その他yt-dlp対応URL"
        ),
        inline=False
    )

    embed.add_field(
        name="診断",
        value=(
            f"yt-dlp `{get_yt_dlp_version()}`\n"
            "エラー発生時はDiscordへ詳細を表示します。\n"
            f"FFmpeg: `{get_ffmpeg_path() or '見つかりません'}`\n"
            f"JS runtime: `{find_js_runtime() or '見つかりません'}`"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# COMMAND 4 /pause
# ============================================================

@bot.tree.command(
    name="pause",
    description="再生を一時停止します"
)
async def pause(
    interaction: discord.Interaction
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    if (
        not player.voice
        or not player.voice.is_playing()
    ):

        await interaction.response.send_message(
            embed=make_error_embed(
                "再生していません",
                "現在再生中の音源がありません。"
            ),
            ephemeral=True
        )

        return

    player.voice.pause()
    player.paused_at = time.monotonic()

    player.touch()

    await interaction.response.send_message(
        embed=make_success_embed(
            "一時停止",
            "再生を一時停止しました。"
        )
    )

    await update_music_panel(player)


# ============================================================
# COMMAND 5 /resume
# ============================================================

@bot.tree.command(
    name="resume",
    description="一時停止した再生を再開します"
)
async def resume(
    interaction: discord.Interaction
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    if (
        not player.voice
        or not player.voice.is_paused()
    ):

        await interaction.response.send_message(
            embed=make_error_embed(
                "一時停止されていません",
                "現在一時停止中ではありません。"
            ),
            ephemeral=True
        )

        return

    if player.paused_at is not None:
        player.paused_total += max(
            0.0,
            time.monotonic() - player.paused_at
        )
        player.paused_at = None

    player.voice.resume()

    player.touch()

    await interaction.response.send_message(
        embed=make_success_embed(
            "再開",
            "再生を再開しました。"
        )
    )

    await update_music_panel(player)


# ============================================================
# COMMAND 6 /skip
# ============================================================

@bot.tree.command(
    name="skip",
    description="現在の曲をスキップします"
)
async def skip(
    interaction: discord.Interaction
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    if (
        not player.voice
        or not player.voice.is_playing()
    ):

        await interaction.response.send_message(
            embed=make_error_embed(
                "再生していません",
                "スキップできる曲がありません。"
            ),
            ephemeral=True
        )

        return

    title = (
        player.current.title
        if player.current
        else "現在の曲"
    )

    # skipではtrack loopも1回解除
    if player.loop_mode == "track":

        player.loop_mode = "off"

    player.touch()

    player.voice.stop()

    await interaction.response.send_message(
        embed=make_success_embed(
            "スキップ",
            (
                f"**{truncate(title, 250)}** "
                "をスキップしました。"
            )
        )
    )


# ============================================================
# COMMAND 7 /stop
# ============================================================

@bot.tree.command(
    name="stop",
    description="再生を停止してキューを削除します"
)
async def stop(
    interaction: discord.Interaction
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    player.play_generation += 1

    player.loop_mode = "off"

    player.clear_queue()

    player.current = None

    player.audio_source = None
    player.started_at = None
    player.paused_at = None
    player.paused_total = 0.0

    if (
        player.voice
        and player.voice.is_playing()
    ):

        player.voice.stop()

    elif (
        player.voice
        and player.voice.is_paused()
    ):

        player.voice.stop()

    player.playing = False

    player.touch()

    await interaction.response.send_message(
        embed=make_success_embed(
            "停止",
            "再生を停止し、キューを削除しました。"
        )
    )

    await update_music_panel(player)


# ============================================================
# COMMAND 8 /leave
# ============================================================

@bot.tree.command(
    name="leave",
    description="ボイスチャンネルから退出します"
)
async def leave(
    interaction: discord.Interaction
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    player.play_generation += 1

    player.loop_mode = "off"

    player.clear_queue()

    player.current = None

    player.playing = False

    player.audio_source = None
    player.started_at = None
    player.paused_at = None
    player.paused_total = 0.0

    if player.voice:

        try:

            await player.voice.disconnect(
                force=True
            )

        except Exception:
            pass

        player.voice = None

    player.touch()

    await interaction.response.send_message(
        embed=make_success_embed(
            "退出",
            "ボイスチャンネルから退出しました。"
        )
    )

    await update_music_panel(player)


# ============================================================
# COMMAND 9 /join
# ============================================================

@bot.tree.command(
    name="join",
    description="あなたのボイスチャンネルへ参加します"
)
async def join(
    interaction: discord.Interaction
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    await interaction.response.defer()

    player = get_player(
        interaction.guild.id
    )

    voice, error = await ensure_voice(
        interaction,
        player
    )

    if error:

        await interaction.edit_original_response(
            embed=make_error_embed(
                "参加失敗",
                error
            )
        )

        return

    ensure_disconnect_watcher(
        player
    )

    await interaction.edit_original_response(
        embed=make_success_embed(
            "参加しました",
            (
                f"ボイスチャンネル "
                f"**{voice.channel.name}** に参加しました。"
            )
        )
    )


# ============================================================
# COMMAND /voice
# ============================================================

@bot.tree.command(name="voice", description="VC入室時に読み上げるメッセージを設定します")
@app_commands.describe(message="読み上げ文。resetで時間帯別メッセージに戻します")
async def voice_command(interaction: discord.Interaction, message: str = ""):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使用してください。", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    settings = guild_extended_settings(interaction.guild.id)
    value = message.strip()
    if value.lower() in {"reset", "default", "解除", "時間帯"} or not value:
        settings["voice_join_message"] = ""
        save_nexora_extended_data()
        text = "時間帯別メッセージに戻しました。\n\n" + default_time_join_message()
    else:
        settings["voice_join_message"] = truncate(value, 300)
        save_nexora_extended_data()
        text = f"入室メッセージを設定しました。\n\n{settings['voice_join_message']}"
    await interaction.response.send_message(embed=make_success_embed("VC入室メッセージ", text), ephemeral=True)


# ============================================================
# COMMAND 10 /queue
# ============================================================

@bot.tree.command(
    name="queue",
    description="再生キューを表示します"
)
async def queue_command(
    interaction: discord.Interaction
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    embed = discord.Embed(
        title="📜 再生キュー",
        color=discord.Color.blurple()
    )

    if player.current:

        embed.add_field(
            name="🎵 現在再生中",
            value=truncate(
                player.current.title,
                1000
            ),
            inline=False
        )

    if not player.queue:

        embed.add_field(
            name="キュー",
            value="キューは空です。",
            inline=False
        )

    else:

        lines = []

        for index, track in enumerate(
            player.queue[:25],
            start=1
        ):

            lines.append(
                f"`{index}.` "
                f"{truncate(track.title, 80)} "
                f"• `{track.service}`"
            )

        embed.add_field(
            name=(
                f"次の曲 "
                f"({len(player.queue)}曲)"
            ),
            value=truncate(
                "\n".join(lines),
                1000
            ),
            inline=False
        )

        if len(player.queue) > 25:

            embed.set_footer(
                text=(
                    f"残り "
                    f"{len(player.queue) - 25} 曲"
                )
            )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# COMMAND 11 /nowplaying
# ============================================================

@bot.tree.command(
    name="nowplaying",
    description="現在再生中の曲を表示します"
)
async def nowplaying(
    interaction: discord.Interaction
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    if not player.current:

        await interaction.response.send_message(
            embed=make_error_embed(
                "再生していません",
                "現在再生中の曲はありません。"
            ),
            ephemeral=True
        )

        return

    player.text_channel = (
        interaction.channel
        if isinstance(
            interaction.channel,
            discord.TextChannel
        )
        else player.text_channel
    )

    await send_music_panel(
        player
    )

    await interaction.response.send_message(
        embed=make_success_embed(
            "Music Panel",
            "公開Music Panelを表示・更新しました。"
        ),
        ephemeral=True
    )


# ============================================================
# COMMAND 12 /volume
# ============================================================

@bot.tree.command(
    name="volume",
    description="音量を変更します"
)
@app_commands.describe(
    volume="0～600"
)
async def volume(
    interaction: discord.Interaction,
    volume: app_commands.Range[
        int,
        0,
        600
    ]
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    player.volume = (
        volume / 100.0
    )

    # 現在再生中なら即時反映
    if player.audio_source:

        try:

            player.audio_source.volume = (
                player.volume
            )

        except Exception:
            pass

    await interaction.response.send_message(
        embed=make_success_embed(
            "音量変更",
            (
                f"音量を **{volume}%** "
                "に設定しました。"
            )
        )
    )


# ============================================================
# COMMAND 13 /loop
# ============================================================

@bot.tree.command(
    name="loop",
    description="ループモードを変更します"
)
@app_commands.describe(
    mode="ループモード"
)
@app_commands.choices(
    mode=[
        app_commands.Choice(
            name="OFF",
            value="off"
        ),
        app_commands.Choice(
            name="現在の曲",
            value="track"
        ),
        app_commands.Choice(
            name="キュー",
            value="queue"
        ),
    ]
)
async def loop_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str]
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    player.loop_mode = mode.value

    await interaction.response.send_message(
        embed=make_success_embed(
            "ループ変更",
            (
                f"ループモードを "
                f"**{mode.name}** にしました。"
            )
        )
    )


# ============================================================
# COMMAND 14 /clear
# ============================================================

@bot.tree.command(
    name="clear",
    description="再生キューをすべて削除します"
)
async def clear(
    interaction: discord.Interaction
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    count = len(
        player.queue
    )

    player.clear_queue()

    player.touch()

    await interaction.response.send_message(
        embed=make_success_embed(
            "キュー削除",
            (
                f"**{count}曲** をキューから"
                "削除しました。"
            )
        )
    )


# ============================================================
# COMMAND 15 /remove
# ============================================================

@bot.tree.command(
    name="remove",
    description="キューから指定番号の曲を削除します"
)
@app_commands.describe(
    position="キュー番号"
)
async def remove(
    interaction: discord.Interaction,
    position: app_commands.Range[
        int,
        1,
        MAX_QUEUE_SIZE
    ]
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    removed = player.remove(
        position
    )

    if not removed:

        await interaction.response.send_message(
            embed=make_error_embed(
                "削除失敗",
                (
                    f"キュー番号 `{position}` "
                    "は存在しません。"
                )
            ),
            ephemeral=True
        )

        return

    player.touch()

    await interaction.response.send_message(
        embed=make_success_embed(
            "削除しました",
            f"**{truncate(removed.title, 250)}**"
        )
    )


# ============================================================
# COMMAND 16 /shuffle
# ============================================================

@bot.tree.command(
    name="shuffle",
    description="再生キューをシャッフルします"
)
async def shuffle(
    interaction: discord.Interaction
):

    if not interaction.guild:

        await interaction.response.send_message(
            embed=make_error_embed(
                "使用できません",
                "サーバー内で使用してください。"
            ),
            ephemeral=True
        )

        return

    player = get_player(
        interaction.guild.id
    )

    if len(player.queue) < 2:

        await interaction.response.send_message(
            embed=make_error_embed(
                "シャッフルできません",
                "キューに2曲以上必要です。"
            ),
            ephemeral=True
        )

        return

    player.shuffle_queue()

    player.touch()

    await interaction.response.send_message(
        embed=make_success_embed(
            "シャッフル",
            (
                f"キュー **{len(player.queue)}曲** "
                "をシャッフルしました。"
            )
        )
    )


# ============================================================
# COMMAND 17 /musichelp
# ============================================================

@bot.tree.command(
    name="musichelp",
    description="NEXORA Musicのヘルプを表示します"
)
async def musichelp(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="🎧 NEXORA Music Help",
        description=(
            "音楽再生Botのコマンド一覧です。"
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="▶️ 再生",
        value=(
            "`/play` 曲名・URL\n"
            "`/source` サービス指定検索\n"
            "`/sources` 対応サービス"
        ),
        inline=False
    )

    embed.add_field(
        name="🎛️ 再生操作",
        value=(
            "`/pause` 一時停止\n"
            "`/resume` 再開\n"
            "`/skip` スキップ\n"
            "`/stop` 停止\n"
            "`/leave` 退出\n"
            "`/join` 参加"
        ),
        inline=False
    )

    embed.add_field(
        name="📜 キュー",
        value=(
            "`/queue` キュー表示\n"
            "`/nowplaying` 現在曲\n"
            "`/clear` キュー削除\n"
            "`/remove` 曲削除\n"
            "`/shuffle` シャッフル"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ 設定",
        value=(
            "`/volume` 音量\n"
            "`/loop` ループ"
        ),
        inline=False
    )

    embed.add_field(
        name="🔊 VC / TTS",
        value=(
            "`/tts on|off` TTS全体\n"
            "`/vcnotify on|off` ユーザー参加通知（デフォルトOFF）\n"
            "`/dj on|off` 曲開始・終了アナウンス\n"
            "`/say 文章` VC読み上げ\n"
            "`/radio on|off [テーマ]` SoundCloudラジオ"
        ),
        inline=False
    )

    embed.add_field(
        name="🌐 音源処理",
        value=(
            "Spotify URLは公開メタデータから曲名を取得し、"
            "SoundCloud → YouTubeの順に公開音源を検索します。"
        ),
        inline=False
    )

    embed.add_field(
        name="🛠️ エラー診断",
        value=(
            "yt-dlp / FFmpeg / 外部サービスで問題が発生した場合、"
            "Discord上に詳細なエラー情報をEmbedで表示します。"
        ),
        inline=False
    )

    embed.set_footer(
        text=(
            f"NEXORA Music • "
            f"yt-dlp {get_yt_dlp_version()}"
        )
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )



# ============================================================
# NEXORA EXTENDED FEATURES: PERSISTENCE / SEARCH / MANAGEMENT
# ============================================================

try:
    import psutil
except Exception:
    psutil = None

import json

NEXORA_DATA_PATH = "nexora_extended_data.json"
NEXORA_HISTORY_LIMIT = 20
NEXORA_FAVORITE_LIMIT = 100
NEXORA_SEARCH_RESULT_LIMIT = 10
NEXORA_USER_REQUEST_WINDOW = 60.0
NEXORA_DEFAULT_REQUEST_LIMIT = 8

nexora_data: dict[str, Any] = {
    "history": {},
    "favorites": {},
    "queue_presets": {},
    "settings": {},
}
sleep_tasks_by_guild: dict[int, asyncio.Task] = {}
request_times_by_guild_user: dict[tuple[int, int], list[float]] = {}


def _data_key(value: int) -> str:
    return str(value)


def load_nexora_extended_data():
    global nexora_data
    try:
        if not os.path.exists(NEXORA_DATA_PATH):
            return
        with open(NEXORA_DATA_PATH, "r", encoding="utf-8") as fp:
            loaded = json.load(fp)
        if isinstance(loaded, dict):
            for key in nexora_data:
                if isinstance(loaded.get(key), dict):
                    nexora_data[key] = loaded[key]
    except Exception as exc:
        print(f"NEXORA data load error: {type(exc).__name__}: {exc}")


def save_nexora_extended_data():
    try:
        temporary = NEXORA_DATA_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fp:
            json.dump(nexora_data, fp, ensure_ascii=False, indent=2)
        os.replace(temporary, NEXORA_DATA_PATH)
    except Exception as exc:
        print(f"NEXORA data save error: {type(exc).__name__}: {exc}")


def guild_extended_settings(guild_id: int) -> dict[str, Any]:
    key = _data_key(guild_id)
    settings = nexora_data["settings"].setdefault(key, {})
    settings.setdefault("request_limit", NEXORA_DEFAULT_REQUEST_LIMIT)
    settings.setdefault("duplicate_block", True)
    settings.setdefault("dj_mode", False)
    settings.setdefault("bot_join_announce", True)
    settings.setdefault("voice_join_message", "")
    return settings


def default_time_join_message() -> str:
    hour = time.localtime().tm_hour
    if 5 <= hour < 12:
        return "おはよ～はやいね~音楽聞きたくなった？(⌒∇⌒)"
    if 12 <= hour < 18:
        return "こんちは^^なんでよんだんですか～まあ音楽きこ！"
    return "うおw今の時間帯から音楽聞くのw?まあいこ～"


def voice_join_message(guild_id: int) -> str:
    custom = str(guild_extended_settings(guild_id).get("voice_join_message", "")).strip()
    return custom or default_time_join_message()


def auto_tts_channel_ids(guild_id: int) -> set[int]:
    return set(nexora_data["settings"].get(_data_key(guild_id), {}).get("auto_tts_channels", []))


def set_auto_tts_channel_ids(guild_id: int, channel_ids: set[int]):
    settings = guild_extended_settings(guild_id)
    settings["auto_tts_channels"] = sorted(channel_ids)
    save_nexora_extended_data()


def is_bot_join_announce_enabled(guild_id: int) -> bool:
    return bool(guild_extended_settings(guild_id).get("bot_join_announce", True))


def record_play_history(guild_id: int, track: Track):
    key = _data_key(guild_id)
    entries = nexora_data["history"].setdefault(key, [])
    entries.insert(0, {
        "title": truncate(track.title, 300),
        "url": track.webpage_url,
        "service": track.service,
        "uploader": truncate(track.uploader or "", 150),
        "time": int(time.time()),
    })
    nexora_data["history"][key] = entries[:NEXORA_HISTORY_LIMIT]
    save_nexora_extended_data()


def is_duplicate_track(player: MusicPlayer, track: Track) -> bool:
    target_url = track.webpage_url.strip()
    target_title = re.sub(r"\s+", " ", track.title).strip().casefold()
    candidates: list[Track] = []
    if player.current:
        candidates.append(player.current)
    candidates.extend(player.queue)
    for candidate in candidates:
        if target_url and candidate.webpage_url.strip() == target_url:
            return True
        title = re.sub(r"\s+", " ", candidate.title).strip().casefold()
        if target_title and title == target_title:
            return True
    return False


def may_control_player(ctx: commands.Context, player: MusicPlayer) -> bool:
    if not ctx.guild:
        return False
    if ctx.author.guild_permissions.manage_guild:
        return True
    settings = guild_extended_settings(ctx.guild.id)
    if not settings.get("dj_mode", False):
        return True
    if not isinstance(ctx.author, discord.Member):
        return False
    if player.voice and ctx.author.voice and ctx.author.voice.channel:
        return player.voice.channel and ctx.author.voice.channel.id == player.voice.channel.id
    return False


def track_from_entry(entry: dict, requester: discord.Member, original_query: str, service: str) -> Optional[Track]:
    return info_to_track(entry, requester, original_query, service)


async def add_track_to_player(
    player: MusicPlayer,
    track: Track,
    *,
    insert_next: bool = False,
) -> tuple[bool, str]:
    settings = guild_extended_settings(player.guild_id)
    if settings.get("duplicate_block", True) and is_duplicate_track(player, track):
        return False, "同じ曲が現在再生中またはキュー内にあるため、重複追加を防止しました。"
    if len(player.queue) >= MAX_QUEUE_SIZE:
        return False, f"キューがいっぱいです。最大 {MAX_QUEUE_SIZE} 曲までです。"
    if insert_next:
        player.queue.insert(0, track)
    else:
        player.queue.append(track)
    record_play_history(player.guild_id, track)
    ensure_disconnect_watcher(player)
    if not player.playing:
        await play_next(player)
    return True, ""


async def search_soundcloud_candidates(query: str) -> tuple[list[dict], list[Diagnostic]]:
    target = f"scsearch{NEXORA_SEARCH_RESULT_LIMIT}:{query.strip()}"
    result = await ytdlp_extract(target, base_ytdlp_options(flat=True))
    info = result.get("info")
    diagnostics: list[Diagnostic] = []
    if not info:
        diagnostics.append(Diagnostic(
            service="SoundCloud",
            stage="候補検索",
            target=query,
            error=result.get("exception") or "候補を取得できませんでした。",
            warnings=result.get("warnings") or [],
        ))
        return [], diagnostics
    entries = info.get("entries") if isinstance(info, dict) else []
    candidates: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
        if not url:
            continue
        copied = dict(entry)
        copied["webpage_url"] = str(url)
        candidates.append(copied)
        if len(candidates) >= NEXORA_SEARCH_RESULT_LIMIT:
            break
    if not candidates:
        diagnostics.append(Diagnostic(
            service="SoundCloud",
            stage="候補検索",
            target=query,
            error="SoundCloudから選択可能な候補を取得できませんでした。",
            warnings=result.get("warnings") or [],
        ))
    return candidates, diagnostics


class ExtendedServiceSelect(discord.ui.Select):
    def __init__(self, requester: discord.Member, player: MusicPlayer, query: str, insert_next: bool = False):
        self.requester = requester
        self.player = player
        self.query = query
        self.insert_next = insert_next
        options = [
            discord.SelectOption(label="SoundCloud", value="SoundCloud", emoji="☁️", description="SoundCloudの公開音源を検索"),
            discord.SelectOption(label="NicoNico", value="NicoNico", emoji="📺", description="ニコニコ動画の公開音源を検索"),
            discord.SelectOption(label="YouTube", value="YouTube", emoji="▶️", description="YouTubeの公開音源を検索"),
            discord.SelectOption(label="Spotifyメタデータ", value="Spotify", emoji="🟢", description="公開音源サービスへフォールバック"),
        ]
        super().__init__(placeholder="再生サービスを選択", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("この検索を開始したユーザーだけ操作できます。", ephemeral=True)
            return
        service = self.values[0]
        await interaction.response.defer(ephemeral=True)
        info, diagnostics = await search_service(self.query, service)
        if not info:
            await interaction.edit_original_response(embed=make_error_embed(f"{service}検索失敗", f"検索: **{truncate(self.query, 250)}**", diagnostics), view=None)
            return
        actual_service = service
        if service == "Spotify":
            actual_service = "SoundCloud"
            for diagnostic in reversed(diagnostics):
                if diagnostic.success:
                    actual_service = diagnostic.service
                    break
        track = info_to_track(info, self.requester, self.query, actual_service)
        if not track:
            await interaction.edit_original_response(embed=make_error_embed("曲情報取得失敗", "Trackを生成できませんでした。", diagnostics), view=None)
            return
        ok, reason = await add_track_to_player(self.player, track, insert_next=self.insert_next)
        if not ok:
            await interaction.edit_original_response(embed=make_error_embed("キュー追加失敗", reason), view=None)
            return
        await interaction.edit_original_response(embed=make_success_embed("次に追加しました" if self.insert_next else "キューに追加しました", f"**{truncate(track.title, 250)}**\nサービス: `{actual_service}`"), view=None)


class ExtendedServiceView(discord.ui.View):
    def __init__(self, requester: discord.Member, player: MusicPlayer, query: str, insert_next: bool = False):
        super().__init__(timeout=120)
        self.add_item(ExtendedServiceSelect(requester, player, query, insert_next))


class ExtendedSearchResultSelect(discord.ui.Select):
    def __init__(self, requester: discord.Member, player: MusicPlayer, query: str, candidates: list[dict]):
        self.requester = requester
        self.player = player
        self.query = query
        self.candidates = candidates
        options = []
        for index, entry in enumerate(candidates[:NEXORA_SEARCH_RESULT_LIMIT]):
            options.append(discord.SelectOption(
                label=truncate(str(entry.get("title") or "Unknown"), 95),
                description=truncate(str(entry.get("uploader") or "SoundCloud候補"), 95),
                value=str(index),
            ))
        super().__init__(placeholder="曲候補を選択", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("この検索を開始したユーザーだけ操作できます。", ephemeral=True)
            return
        entry = self.candidates[int(self.values[0])]
        title = str(entry.get("title") or self.query)
        url = str(entry.get("webpage_url") or entry.get("original_url") or "")
        description = f"選択曲: **{truncate(title, 300)}**\n再生サービスを選択してください。"
        await interaction.response.edit_message(
            embed=discord.Embed(title="🎵 再生サービスを選択", description=description, color=discord.Color.blurple()),
            view=ExtendedServiceView(self.requester, self.player, url or title),
        )


class ExtendedSearchResultView(discord.ui.View):
    def __init__(self, requester: discord.Member, player: MusicPlayer, query: str, candidates: list[dict]):
        super().__init__(timeout=120)
        self.add_item(ExtendedSearchResultSelect(requester, player, query, candidates))


async def extended_prefix_play(ctx: commands.Context, query: str, insert_next: bool = False):
    if await try_prefix_media(ctx, query, insert_next=insert_next):
        return
    if not ctx.guild:
        await prefix_send_error(ctx, "使用できません", "サーバー内でのみ使用できます。")
        return
    query = query.strip()
    if not query:
        await prefix_send_error(ctx, "曲名がありません", "曲名またはURLを指定してください。")
        return
    player = get_player(ctx.guild.id)
    player.text_channel = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else player.text_channel
    voice, error = await prefix_ensure_voice(ctx, player)
    if error:
        await prefix_send_error(ctx, "ボイス接続エラー", error)
        return
    settings = guild_extended_settings(ctx.guild.id)
    key = (ctx.guild.id, ctx.author.id)
    now = time.monotonic()
    times = [value for value in request_times_by_guild_user.get(key, []) if now - value < NEXORA_USER_REQUEST_WINDOW]
    request_times_by_guild_user[key] = times
    if len(times) >= int(settings.get("request_limit", NEXORA_DEFAULT_REQUEST_LIMIT)):
        await prefix_send_error(ctx, "リクエスト制限", "短時間の連続追加が多すぎます。少し待ってから再試行してください。")
        return
    times.append(now)
    if is_url(query):
        service = detect_service(query)
        if service == "Spotify":
            title, diagnostics = await spotify_oembed(query)
            if not title:
                await prefix_send_error(ctx, "Spotify取得失敗", "Spotify公開メタデータを取得できませんでした。", diagnostics)
                return
            info, actual_service, public_diagnostics = await search_public_sources(title)
            diagnostics.extend(public_diagnostics)
            if not info:
                await prefix_send_error(ctx, "公開音源検索失敗", f"Spotify曲: **{truncate(title, 250)}**", diagnostics)
                return
            service = actual_service or "SoundCloud"
        elif service == "YouTube":
            info, diagnostics = await extract_youtube(query)
        else:
            info, diagnostics = await extract_generic(query, service)
        if not info:
            await prefix_send_error(ctx, f"{service}取得失敗", f"対象: `{safe_target(query)}`", diagnostics)
            return
        track = info_to_track(info, ctx.author, query, service)
        if not track:
            await prefix_send_error(ctx, "曲情報取得失敗", "Trackを生成できませんでした。", diagnostics)
            return
        ok, reason = await add_track_to_player(player, track, insert_next=insert_next)
        if not ok:
            await prefix_send_error(ctx, "キュー追加失敗", reason)
            return
        await prefix_send_success(ctx, "次に追加しました" if insert_next else "キューに追加しました", f"**{truncate(track.title, 250)}**\nサービス: `{service}`")
        return
    await ctx.send(embed=discord.Embed(
        title="🔎 曲を検索",
        description=f"検索: **{truncate(query, 300)}**\n下のメニューから再生サービスを選択してください。",
        color=discord.Color.blurple(),
    ), view=ExtendedServiceView(ctx.author, player, query, insert_next))


@bot.command(name="next")
async def prefix_next(ctx: commands.Context, *, query: str = ""):
    await extended_prefix_play(ctx, query, insert_next=True)


@bot.command(name="history")
async def prefix_history(ctx: commands.Context):
    if not ctx.guild:
        return
    entries = nexora_data["history"].get(_data_key(ctx.guild.id), [])
    embed = discord.Embed(title="🕘 再生履歴", color=discord.Color.blurple())
    if not entries:
        embed.description = "履歴はありません。"
    else:
        lines = [f"`{index}.` {truncate(item.get('title', 'Unknown'), 90)} • `{item.get('service', 'Unknown')}`" for index, item in enumerate(entries[:NEXORA_HISTORY_LIMIT], 1)]
        embed.description = "\n".join(lines)
    await ctx.send(embed=embed)


@bot.command(name="fav")
async def prefix_favorite(ctx: commands.Context, action: str = "list", index: str = ""):
    user_key = _data_key(ctx.author.id)
    favorites = nexora_data["favorites"].setdefault(user_key, [])
    action = action.lower().strip()
    if action == "add":
        if not ctx.guild:
            return
        player = get_player(ctx.guild.id)
        track = player.current
        if not track:
            await prefix_send_error(ctx, "お気に入り追加失敗", "現在再生中の曲がありません。")
            return
        if not track.webpage_url:
            await prefix_send_error(ctx, "お気に入り追加失敗", "この曲には保存可能なURLがありません。")
            return
        if any(item.get("url") == track.webpage_url for item in favorites):
            await prefix_send_error(ctx, "お気に入り追加失敗", "この曲はすでにお気に入りにあります。")
            return
        if len(favorites) >= NEXORA_FAVORITE_LIMIT:
            await prefix_send_error(ctx, "お気に入り上限", f"最大 {NEXORA_FAVORITE_LIMIT} 曲までです。")
            return
        favorites.append({"title": track.title, "url": track.webpage_url, "service": track.service})
        save_nexora_extended_data()
        await prefix_send_success(ctx, "お気に入り追加", f"**{truncate(track.title, 250)}**")
        return
    if action == "rm":
        try:
            number = int(index)
            removed = favorites.pop(number - 1)
        except Exception:
            await prefix_send_error(ctx, "削除失敗", "例: `n!fav rm 1`")
            return
        save_nexora_extended_data()
        await prefix_send_success(ctx, "お気に入り削除", truncate(removed.get("title", "Unknown"), 250))
        return
    if action == "play":
        try:
            number = int(index)
            favorite = favorites[number - 1]
        except Exception:
            await prefix_send_error(ctx, "再生失敗", "例: `n!fav play 1`")
            return
        await extended_prefix_play(ctx, str(favorite.get("url", "")))
        return
    embed = discord.Embed(title=f"⭐ {ctx.author.display_name} のお気に入り", color=discord.Color.gold())
    embed.description = "\n".join(f"`{i}.` {truncate(item.get('title', 'Unknown'), 90)} • `{item.get('service', 'Unknown')}`" for i, item in enumerate(favorites, 1)) or "お気に入りはありません。"
    embed.set_footer(text="n!fav add / n!fav play 番号 / n!fav rm 番号")
    await ctx.send(embed=embed)


@bot.command(name="savequeue")
async def prefix_savequeue(ctx: commands.Context, name: str = ""):
    if not ctx.guild or not name.strip():
        await prefix_send_error(ctx, "保存名エラー", "例: `n!savequeue night`")
        return
    player = get_player(ctx.guild.id)
    tracks = ([] if not player.current else [player.current]) + list(player.queue)
    if not tracks:
        await prefix_send_error(ctx, "保存できません", "保存する曲がありません。")
        return
    preset = []
    for track in tracks:
        if track.webpage_url:
            preset.append({"title": track.title, "url": track.webpage_url, "service": track.service})
    if not preset:
        await prefix_send_error(ctx, "保存できません", "保存可能な公開URLがありません。")
        return
    nexora_data["queue_presets"].setdefault(_data_key(ctx.guild.id), {})[truncate(name.strip().lower(), 40)] = preset[:MAX_QUEUE_SIZE]
    save_nexora_extended_data()
    await prefix_send_success(ctx, "キュー保存", f"`{truncate(name, 40)}` に {len(preset)} 曲を保存しました。")


@bot.command(name="loadqueue")
async def prefix_loadqueue(ctx: commands.Context, name: str = ""):
    if not ctx.guild or not name.strip():
        await prefix_send_error(ctx, "保存名エラー", "例: `n!loadqueue night`")
        return
    preset = nexora_data["queue_presets"].get(_data_key(ctx.guild.id), {}).get(name.strip().lower())
    if not preset:
        await prefix_send_error(ctx, "見つかりません", "その保存キューは存在しません。")
        return
    player = get_player(ctx.guild.id)
    player.text_channel = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else player.text_channel
    voice, error = await prefix_ensure_voice(ctx, player)
    if error:
        await prefix_send_error(ctx, "VC接続エラー", error)
        return
    added = 0
    for item in preset:
        if len(player.queue) >= MAX_QUEUE_SIZE:
            break
        url = str(item.get("url", ""))
        if not url:
            continue
        track = Track(title=str(item.get("title", "Unknown")), webpage_url=url, requester_id=ctx.author.id, requester_name=ctx.author.display_name, service=str(item.get("service", detect_service(url))), original_query=url)
        if not is_duplicate_track(player, track):
            player.queue.append(track)
            added += 1
    ensure_disconnect_watcher(player)
    if not player.playing:
        await play_next(player)
    await prefix_send_success(ctx, "キュー復元", f"`{truncate(name, 40)}` から {added} 曲を追加しました。")


@bot.command(name="sleep")
async def prefix_sleep(ctx: commands.Context, minutes: str = ""):
    if not ctx.guild:
        return
    guild_id = ctx.guild.id
    old = sleep_tasks_by_guild.pop(guild_id, None)
    if old and not old.done():
        old.cancel()
    if minutes.lower().strip() in {"off", "cancel", "解除"}:
        await prefix_send_success(ctx, "睡眠タイマー", "睡眠タイマーを解除しました。")
        return
    try:
        value = int(minutes)
        if not 1 <= value <= 1440:
            raise ValueError
    except Exception:
        await prefix_send_error(ctx, "指定エラー", "例: `n!sleep 30` / `n!sleep off`")
        return
    async def sleeper():
        try:
            await asyncio.sleep(value * 60)
            player = get_player(guild_id)
            player.play_generation += 1
            player.clear_queue()
            player.loop_mode = "off"
            if player.voice and (player.voice.is_playing() or player.voice.is_paused()):
                player.voice.stop()
            player.playing = False
            if player.voice and player.voice.is_connected():
                await player.voice.disconnect(force=True)
            player.voice = None
            if player.text_channel:
                await player.text_channel.send(embed=make_success_embed("睡眠タイマー", "タイマー時間になったため、再生を停止してVCから退出しました。"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Sleep timer error: {exc}")
    sleep_tasks_by_guild[guild_id] = asyncio.create_task(sleeper())
    await prefix_send_success(ctx, "睡眠タイマー", f"{value} 分後に停止・退出します。")


@bot.command(name="requestlimit")
async def prefix_requestlimit(ctx: commands.Context, value: str = ""):
    if not ctx.guild or not ctx.author.guild_permissions.manage_guild:
        await prefix_send_error(ctx, "権限不足", "サーバー管理権限が必要です。")
        return
    try:
        limit = int(value)
        if not 1 <= limit <= 50:
            raise ValueError
    except Exception:
        await prefix_send_error(ctx, "指定エラー", "例: `n!requestlimit 8`（1～50）")
        return
    guild_extended_settings(ctx.guild.id)["request_limit"] = limit
    save_nexora_extended_data()
    await prefix_send_success(ctx, "リクエスト制限", f"同一ユーザーの60秒あたり上限を {limit} 曲にしました。")


@bot.command(name="djmode")
async def prefix_djmode(ctx: commands.Context, mode: str = ""):
    if not ctx.guild or not ctx.author.guild_permissions.manage_guild:
        await prefix_send_error(ctx, "権限不足", "サーバー管理権限が必要です。")
        return
    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await prefix_send_error(ctx, "指定エラー", "例: `n!djmode on` / `n!djmode off`")
        return
    guild_extended_settings(ctx.guild.id)["dj_mode"] = mode == "on"
    save_nexora_extended_data()
    await prefix_send_success(ctx, "DJ権限モード", f"DJモードを **{mode.upper()}** にしました。")


@bot.command(name="botjoinannounce")
async def prefix_bot_join_announce(ctx: commands.Context):
    if not ctx.guild:
        return
    settings = guild_extended_settings(ctx.guild.id)
    settings["bot_join_announce"] = not bool(settings.get("bot_join_announce", True))
    save_nexora_extended_data()
    await prefix_send_success(ctx, "Bot入室読み上げ", f"BotのVC入室読み上げを **{'ON' if settings['bot_join_announce'] else 'OFF'}** にしました。")


@bot.command(name="status")
async def prefix_status(ctx: commands.Context):
    guild = ctx.guild
    player = get_player(guild.id) if guild else None
    process_ram = "N/A"
    cpu_usage = "N/A"
    process_info = "N/A"
    disk_free = "N/A"
    if psutil:
        try:
            process_ram = f"{psutil.Process().memory_info().rss / (1024 * 1024):.1f} MiB"
            cpu_usage = f"{psutil.cpu_percent(interval=0.1):.1f}%"
            rows = []
            for proc in sorted(psutil.process_iter(["name", "cpu_percent"]), key=lambda p: p.info.get("cpu_percent") or 0, reverse=True)[:5]:
                try:
                    rows.append(f"{truncate(proc.info.get('name') or '?', 18)} {proc.info.get('cpu_percent') or 0:.1f}%")
                except Exception:
                    pass
            process_info = "\n".join(rows) or "N/A"
        except Exception:
            pass
    try:
        disk = shutil.disk_usage("/")
        disk_free = f"{disk.free / (1024 ** 3):.2f} GiB"
    except Exception:
        pass
    embed = discord.Embed(title="🩺 NEXORA Status", color=discord.Color.blurple())
    embed.add_field(name="Ping", value=f"`{round(bot.latency * 1000)} ms`", inline=True)
    embed.add_field(name="CPU", value=f"`{cpu_usage}`", inline=True)
    embed.add_field(name="RAM", value=f"`{process_ram}`", inline=True)
    embed.add_field(name="SSD 空き", value=f"`{disk_free}`", inline=True)
    embed.add_field(name="FFmpeg", value=f"`{get_ffmpeg_path() or '見つかりません'}`", inline=False)
    embed.add_field(name="yt-dlp", value=f"`{get_yt_dlp_version()}`", inline=True)
    embed.add_field(name="実行中プロセス上位", value=f"```text\n{truncate(process_info, 900)}\n```", inline=False)
    if player:
        embed.add_field(name="VC", value=f"`{'接続中' if player.voice and player.voice.is_connected() else '未接続'}`", inline=True)
        embed.add_field(name="再生", value=truncate(player.current.title if player.current else "停止中", 200), inline=False)
        embed.add_field(name="キュー", value=f"`{len(player.queue)} 曲`", inline=True)
    if guild:
        embed.add_field(name="自動TTS", value=f"`{'ON' if auto_tts_channel_ids(guild.id) else 'OFF'}`", inline=True)
        embed.add_field(name="Bot入室読み上げ", value=f"`{'ON' if is_bot_join_announce_enabled(guild.id) else 'OFF'}`", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="ping")
async def prefix_ping(ctx: commands.Context):
    start = time.perf_counter()
    message = await ctx.send("計測中...")
    response_ms = round((time.perf_counter() - start) * 1000)
    websocket_ms = round(bot.latency * 1000)
    await message.edit(content=f"🏓 Pong\nWebSocket: `{websocket_ms} ms`\n応答時間: `{response_ms} ms`")


@bot.tree.command(name="search", description="最大10件の候補から曲を選んで再生します")
@app_commands.describe(query="曲名")
async def extended_search_command(interaction: discord.Interaction, query: str):
    if not interaction.guild:
        await interaction.response.send_message(embed=make_error_embed("使用できません", "サーバー内で使用してください。"), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    player = get_player(interaction.guild.id)
    player.text_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else player.text_channel
    voice, error = await ensure_voice(interaction, player)
    if error:
        await interaction.edit_original_response(embed=make_error_embed("VC接続エラー", error))
        return
    candidates, diagnostics = await search_soundcloud_candidates(query)
    if not candidates:
        await interaction.edit_original_response(embed=make_error_embed("候補が見つかりません", f"検索: **{truncate(query, 250)}**", diagnostics))
        return
    await interaction.edit_original_response(
        embed=discord.Embed(title="🔎 検索結果", description=f"検索: **{truncate(query, 250)}**\n最大 {len(candidates)} 件の候補から選択してください。", color=discord.Color.blurple()),
        view=ExtendedSearchResultView(interaction.user, player, query, candidates),
    )


load_nexora_extended_data()

# ============================================================
# REQUIRED COMMANDS / TTS / PANEL / SAFE SLASH COMMAND REFRESH
# ============================================================

TTS_SETTINGS_FILE = "nexora_tts_settings.json"
auto_tts_channels_by_guild: dict[int, set[int]] = {}
bot_join_announce_by_guild: dict[int, bool] = {}


def load_required_tts_settings():
    try:
        if not os.path.exists(TTS_SETTINGS_FILE):
            return
        with open(TTS_SETTINGS_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        tts_master_by_guild.update({int(k): bool(v) for k, v in data.get("tts_master", {}).items()})
        tts_user_join_by_guild.update({int(k): bool(v) for k, v in data.get("user_join", {}).items()})
        tts_dj_by_guild.update({int(k): bool(v) for k, v in data.get("dj", {}).items()})
        tts_radio_by_guild.update({int(k): bool(v) for k, v in data.get("radio", {}).items()})
        radio_query_by_guild.update({int(k): str(v) for k, v in data.get("radio_query", {}).items()})
        auto_tts_channels_by_guild.update({int(k): {int(x) for x in v} for k, v in data.get("auto_tts_channels", {}).items() if isinstance(v, list)})
        bot_join_announce_by_guild.update({int(k): bool(v) for k, v in data.get("bot_join_announce", {}).items()})
    except Exception as exc:
        print(f"TTS settings load error: {type(exc).__name__}: {exc}")


def save_required_tts_settings():
    try:
        data = {
            "tts_master": {str(k): v for k, v in tts_master_by_guild.items()},
            "user_join": {str(k): v for k, v in tts_user_join_by_guild.items()},
            "dj": {str(k): v for k, v in tts_dj_by_guild.items()},
            "radio": {str(k): v for k, v in tts_radio_by_guild.items()},
            "radio_query": {str(k): v for k, v in radio_query_by_guild.items()},
            "auto_tts_channels": {str(k): sorted(v) for k, v in auto_tts_channels_by_guild.items()},
            "bot_join_announce": {str(k): v for k, v in bot_join_announce_by_guild.items()},
        }
        temp_path = TTS_SETTINGS_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(temp_path, TTS_SETTINGS_FILE)
    except Exception as exc:
        print(f"TTS settings save error: {type(exc).__name__}: {exc}")


def bot_join_announce_enabled(guild_id: int) -> bool:
    return bot_join_announce_by_guild.get(guild_id, True)


load_required_tts_settings()


async def delete_and_recreate_music_panel(player: MusicPlayer):
    old_task = player.panel_task
    player.panel_task = None
    if old_task and old_task is not asyncio.current_task() and not old_task.done():
        old_task.cancel()
        try:
            await old_task
        except (asyncio.CancelledError, Exception):
            pass
    old_message = player.panel_message
    player.panel_message = None
    player.panel_view = None
    if old_message:
        try:
            await old_message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    await send_music_panel(player)


@bot.command(name="help")
async def required_help_command(ctx: commands.Context):
    await prefix_music_help(ctx)


@bot.command(name="y")
async def required_auto_tts_command(ctx: commands.Context):
    if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
        await prefix_send_error(ctx, "使用できません", "サーバーの通常テキストチャンネルで使用してください。")
        return
    channel_ids = auto_tts_channels_by_guild.setdefault(ctx.guild.id, set())
    if ctx.channel.id in channel_ids:
        channel_ids.remove(ctx.channel.id)
        enabled = False
    else:
        channel_ids.add(ctx.channel.id)
        enabled = True
    save_required_tts_settings()
    await prefix_send_success(ctx, "自動TTS設定", f"このチャンネルの自動TTSを **{'ON' if enabled else 'OFF'}** にしました。")


@bot.command(name="o")
async def required_bot_join_announce_command(ctx: commands.Context):
    if not ctx.guild:
        return
    enabled = not bot_join_announce_enabled(ctx.guild.id)
    bot_join_announce_by_guild[ctx.guild.id] = enabled
    save_required_tts_settings()
    await prefix_send_success(ctx, "Bot入室読み上げ", f"BotのVC入室読み上げを **{'ON' if enabled else 'OFF'}** にしました。")


@bot.command(name="panelreset")
async def required_panelreset_command(ctx: commands.Context):
    if not ctx.guild:
        return
    player = get_player(ctx.guild.id)
    if isinstance(ctx.channel, discord.TextChannel):
        player.text_channel = ctx.channel
    await delete_and_recreate_music_panel(player)
    await prefix_send_success(ctx, "Music Panel再作成", "古いPanelを削除して新しい公開Panelを投稿しました。")


@bot.command(name="ttsstatus")
async def required_ttsstatus_command(ctx: commands.Context):
    if not ctx.guild:
        return
    channels = auto_tts_channels_by_guild.get(ctx.guild.id, set())
    embed = discord.Embed(title="🔊 NEXORA TTS Status", color=discord.Color.blurple())
    embed.add_field(name="TTS全体", value="ON" if guild_tts_enabled(ctx.guild.id) else "OFF", inline=True)
    embed.add_field(name="自動TTS", value=f"ON ({len(channels)} チャンネル)" if channels else "OFF", inline=True)
    embed.add_field(name="edge-tts", value="利用可能" if edge_tts is not None else "未インストール", inline=True)
    embed.add_field(name="Bot入室読み上げ", value="ON" if bot_join_announce_enabled(ctx.guild.id) else "OFF", inline=True)
    await ctx.send(embed=embed)


async def refresh_global_commands_without_duplicates() -> int:
    synced = await bot.tree.sync()
    return len(synced)


async def remove_old_guild_commands(guild: discord.Guild):
    bot.tree.clear_commands(guild=guild)
    await bot.tree.sync(guild=guild)


@bot.command(name="synccommands", aliases=["refreshcommands", "updatecommands"])
@commands.has_guild_permissions(manage_guild=True)
async def required_synccommands_command(ctx: commands.Context, mode: str = "clean"):
    if not ctx.guild:
        await prefix_send_error(ctx, "使用できません", "サーバー内で使用してください。")
        return
    try:
        mode = mode.lower().strip()
        if mode in {"clean", "cleanup", "fix", "修正", "削除"}:
            await remove_old_guild_commands(ctx.guild)
        count = await refresh_global_commands_without_duplicates()
        await prefix_send_success(ctx, "コマンド更新完了", f"グローバルSlash Commandを `{count}` 個同期しました。\nこのサーバーに残っていた重複用の専用Slash Commandは削除しました。\nPrefix CommandはBot再起動後に反映済みです。")
    except Exception as exc:
        await prefix_send_error(ctx, "コマンド更新失敗", f"{type(exc).__name__}: {exc}")


@required_synccommands_command.error
async def required_synccommands_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await prefix_send_error(ctx, "権限不足", "サーバー管理権限が必要です。")
        return
    await prefix_send_error(ctx, "コマンド更新エラー", f"{type(error).__name__}: {error}")


@bot.tree.command(name="synccommands", description="重複したサーバー専用コマンドを削除して最新化します")
async def required_slash_synccommands(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(embed=make_error_embed("権限不足", "サーバー管理権限が必要です。"), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await remove_old_guild_commands(interaction.guild)
        count = await refresh_global_commands_without_duplicates()
        await interaction.edit_original_response(embed=make_success_embed("コマンド更新完了", f"重複したサーバー専用Slash Commandを削除しました。\nグローバルSlash Command `{count}` 個を同期しました。"))
    except Exception as exc:
        await interaction.edit_original_response(embed=make_error_embed("コマンド更新失敗", f"{type(exc).__name__}: {exc}"))



DELETED_LOG_CHANNEL_NAME = "deleted-messages"
deleted_messages_by_guild: dict[int, list[dict]] = {}

class DeletedLogView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.page = 0

    def build_embed(self) -> discord.Embed:
        items = deleted_messages_by_guild.get(self.guild_id, [])
        per = 5
        total = max(1, (len(items) + per - 1) // per)
        self.page %= total
        embed = discord.Embed(
            title="🗑️ 削除されたメッセージ",
            description=f"ページ {self.page + 1}/{total}・保存件数 {len(items)}",
            color=discord.Color.red(),
        )
        chunk = items[self.page * per:(self.page + 1) * per]
        if not chunk:
            embed.add_field(name="履歴", value="なし", inline=False)
        for item in chunk:
            embed.add_field(
                name=f"{item['author']} • #{item['channel']}",
                value=(item['content'] or "（添付ファイルのみ）")[:1000],
                inline=False,
            )
        return embed

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page -= 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page += 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.danger)
    async def clear_log(self, interaction: discord.Interaction, _: discord.ui.Button):
        deleted_messages_by_guild[self.guild_id] = []
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    history = deleted_messages_by_guild.setdefault(message.guild.id, [])
    history.append({
        "author": str(message.author),
        "channel": getattr(message.channel, "name", "unknown"),
        "content": message.content or "",
    })
    deleted_messages_by_guild[message.guild.id] = history[-100:]
    channel = discord.utils.get(message.guild.text_channels, name=DELETED_LOG_CHANNEL_NAME)
    if channel:
        view = DeletedLogView(message.guild.id)
        try:
            await channel.send(embed=view.build_embed(), view=view)
        except Exception:
            pass


async def groq_chat(prompt: str, system: str) -> str:
    if not GROQ_API_KEY or "ここに" in GROQ_API_KEY:
        raise RuntimeError("main.py の GROQ_API_KEY にGroq APIキーを入力してください。")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.8,
        "max_tokens": 80,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            data = await response.json()
            if response.status != 200:
                raise RuntimeError(str(data.get("error", {}).get("message", f"HTTP {response.status}")))
            return str(data["choices"][0]["message"]["content"]).strip().strip('"')

async def youtube_search_first(query: str) -> dict:
    """YouTubeで実際に検索し、最初の曲を返します。"""
    def _search():
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            raise RuntimeError("YouTube検索結果が見つかりませんでした。")
        return entries[0]
    return await asyncio.to_thread(_search)

async def ai_play_youtube(message: discord.Message, query: str, label: str = "🎵 AIリクエスト"):
    entry = await youtube_search_first(query)
    video_url = entry.get("webpage_url") or entry.get("url")
    title = entry.get("title") or query
    if not video_url:
        raise RuntimeError("YouTube動画URLを取得できませんでした。")
    embed = make_success_embed(label, f"[{title}]({video_url})\nYouTube検索クエリ: {query}")
    await message.channel.send(embed=embed)
    context = await bot.get_context(message)
    play_command = bot.get_command("p") or bot.get_command("play")
    if play_command is None:
        raise RuntimeError("再生コマンド n!p が見つかりません。")
    await context.invoke(play_command, query=video_url)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # .s で削除メッセージ履歴を表示
    if message.content.strip() == ".s":
        view = DeletedLogView(message.guild.id)
        await message.channel.send(embed=view.build_embed(), view=view)
        return

    # .rec でAIおすすめ曲をYouTube検索して追加
    if message.content.strip() in (".rec", ".recommend", ".おすすめ"):
        try:
            player = get_player(message.guild.id)
            now = player.current.title if player and player.current else "なし"
            queue_titles = ", ".join(t.title for t in player.queue[:10]) if player and player.queue else "なし"
            prompt = f"現在再生中: {now}\nキュー: {queue_titles}\nこの流れに合うおすすめ曲を1曲提案してください。"
            query = await groq_chat(
                prompt,
                "あなたは音楽DJです。次に流すべき楽曲を1曲だけ提案し、YouTubeで見つかりやすい検索クエリのみを返してください。説明・引用符・番号は禁止です。",
            )
            await ai_play_youtube(message, query, "✨ AIおすすめ")
        except Exception as exc:
            await message.channel.send(embed=make_error_embed("AIおすすめ", safe_error_text(str(exc))))
        return

    if bot.user in message.mentions:
        prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if prompt:
            try:
                query = await groq_chat(
                    prompt,
                    "ユーザーの雰囲気・歌手・状況から、YouTubeで見つかりやすい楽曲検索クエリを1つだけ返してください。説明・引用符・番号は禁止です。",
                )
                await ai_play_youtube(message, query)
            except Exception as exc:
                await message.channel.send(embed=make_error_embed("Groq / YouTube", safe_error_text(str(exc))))
            return

    try:
        if message.guild and isinstance(message.channel, discord.TextChannel):
            text = message.content.strip()
            if text and not text.lower().startswith(PREFIX.lower()) and message.channel.id in auto_tts_channels_by_guild.get(message.guild.id, set()):
                player = get_player(message.guild.id)
                if player.voice and player.voice.is_connected():
                    await speak_in_voice(player, truncate(text, 300), force=True)
    except Exception as exc:
        if message.guild:
            remember_error(message.guild.id, Diagnostic(service="Auto TTS", stage="on_message", target="message", error=f"{type(exc).__name__}: {exc}"))
    finally:
        await bot.process_commands(message)

# ============================================================
# GLOBAL APP COMMAND ERROR
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    original = error

    if isinstance(
        error,
        app_commands.CommandInvokeError
    ):

        original = error.original

    diagnostic = Diagnostic(
        service="NEXORA",
        stage="Slash Command",
        target=(
            interaction.command.name
            if interaction.command
            else "unknown"
        ),
        error=(
            f"{type(original).__name__}: "
            f"{original}"
        )
    )

    remember_error(
        interaction.guild_id,
        diagnostic
    )

    embed = make_error_embed(
        "コマンド実行エラー",
        (
            "コマンドの実行中に予期しない"
            "エラーが発生しました。"
        ),
        [diagnostic]
    )

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                embed=embed,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )

    except Exception:
        pass


# ============================================================
# BOT PRESENCE
# ============================================================

async def presence_rotation():
    """Bot全体の統計をPlayingへ、曲名を各VC Statusへ3秒ごとに反映する。"""
    index = 0
    while not bot.is_closed():
        try:
            active = sum(1 for player in music_players.values() if player.voice and player.voice.is_connected())
            ping = round(bot.latency * 1000)
            ram = "N/A"
            if psutil:
                try:
                    ram = f"{psutil.Process().memory_info().rss / (1024 * 1024):.0f}MiB"
                except Exception:
                    pass
            try:
                disk = shutil.disk_usage("/")
                ssd = f"{disk.free / (1024 ** 3):.1f}GiB空き"
            except Exception:
                ssd = "N/A"
            statuses = [f"🎧 {active} VC参加中", f"🏓 Ping {ping}ms", f"🧠 RAM {ram}", f"💾 SSD {ssd}"]
            await bot.change_presence(status=discord.Status.online, activity=discord.Game(name=statuses[index % len(statuses)]))
            index += 1
            for player in music_players.values():
                if not player.voice or not player.voice.is_connected() or not player.voice.channel:
                    continue
                status = f"🎵 {truncate(player.current.title, 80)}" if player.playing and player.current else ""
                try:
                    await player.voice.channel.edit(status=status)
                except (discord.Forbidden, discord.HTTPException, AttributeError):
                    pass
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Presence update error: {exc}")
            await asyncio.sleep(3)


# ============================================================
# READY
# ============================================================

@bot.event
async def on_ready():

    try:

        synced = await bot.tree.sync()

        print(
            "NEXORA Music Ready: "
            f"{bot.user} / "
            f"{len(synced)} commands"
        )

        print(
            "FFmpeg:",
            get_ffmpeg_path()
        )

        print(
            "JS runtime:",
            find_js_runtime()
        )

        global presence_task
        if presence_task is None or presence_task.done():
            presence_task = asyncio.create_task(
                presence_rotation()
            )

    except Exception as exc:

        print(
            f"Command sync error: {exc}"
        )


# ============================================================
# VOICE STATE
# ============================================================

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState
):

    if not bot.user:
        return

    guild = member.guild
    player = music_players.get(guild.id)

    # --------------------------------------------------------
    # Bot自身のVC状態
    # --------------------------------------------------------
    if member.id == bot.user.id:
        if not player:
            return

        if after.channel is not None:
            # Discord側から接続された/移動した場合も3秒後に通知
            if before.channel is None or before.channel.id != after.channel.id:
                schedule_voice_announce(
                    player,
                    voice_join_message(guild.id),
                    3.0
                )
            return

        if after.channel is None:
            player.voice = None
            player.playing = False
            player.current = None
            player.audio_source = None
            player.started_at = None
            player.paused_at = None
            player.paused_total = 0.0
        return

    # --------------------------------------------------------
    # ユーザーのVC参加通知（デフォルトOFF）
    # --------------------------------------------------------
    if not player:
        return

    if not guild_user_join_enabled(guild.id):
        return

    if not player.voice or not player.voice.is_connected():
        return

    bot_channel = player.voice.channel
    if not bot_channel:
        return

    joined_same_channel = (
        after.channel is not None
        and after.channel.id == bot_channel.id
        and (
            before.channel is None
            or before.channel.id != after.channel.id
        )
    )

    if joined_same_channel:
        await speak_in_voice(
            player,
            f"{member.display_name}さんがVCに参加しました。"
        )


# ============================================================
# SHUTDOWN
# ============================================================

async def shutdown_players():

    for player in music_players.values():

        try:

            player.play_generation += 1

            if player.voice:

                await player.voice.disconnect(
                    force=True
                )

        except Exception:
            pass

        player.voice = None

        player.playing = False

        player.current = None

        player.audio_source = None
        player.started_at = None
        player.paused_at = None
        player.paused_total = 0.0


# ============================================================
# TOKEN CHECK
# ============================================================

def token_is_invalid(
    token: str
) -> bool:

    if not token:
        return True

    normalized = token.strip()

    invalid_values = {
        "",
        "ここ",
        "ここに",
        "ここにDiscord Bot Token",
        "YOUR_TOKEN",
        "YOUR_DISCORD_TOKEN",
        "YOUR_DISCORD_BOT_TOKEN",
        "DISCORD_TOKEN",
    }

    if normalized in invalid_values:
        return True

    return False


# ============================================================
# MAIN
# ============================================================


@bot.command(name="recommend", aliases=["rec", "おすすめ"])
async def recommend(ctx: commands.Context, *, prompt: str = ""):
    """AIが流れに合うおすすめ曲をYouTube検索して追加します。"""
    try:
        player = get_player(ctx.guild.id)
        now = player.current.title if player and player.current else "なし"
        queue_titles = ", ".join(t.title for t in player.queue[:10]) if player and player.queue else "なし"
        base = prompt.strip() or f"現在再生中: {now}\nキュー: {queue_titles}\nこの流れに合うおすすめ曲を1曲提案してください。"
        query = await groq_chat(
            base,
            "あなたは音楽DJです。次に流すべき楽曲を1曲だけ提案し、YouTubeで見つかりやすい検索クエリのみを返してください。説明・引用符・番号は禁止です。",
        )
        await ai_play_youtube(ctx.message, query, "✨ AIおすすめ")
    except Exception as exc:
        await ctx.send(embed=make_error_embed("AIおすすめ", safe_error_text(str(exc))))

if __name__ == "__main__":

    if token_is_invalid(
        TOKEN
    ):

        print(
            "DISCORD_TOKEN が設定されていません。"
        )

        print(
            ".env に以下を設定してください:"
        )

        print(
            "DISCORD_TOKEN=あなたのBotトークン"
        )

        sys.exit(1)

    try:

        bot.run(
            TOKEN
        )

    except KeyboardInterrupt:

        pass

    except Exception as exc:

        print(
            "NEXORA起動エラー:",
            repr(exc)
        )
