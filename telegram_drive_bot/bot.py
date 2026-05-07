import asyncio
import base64
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL

from telegram_drive_bot.gdrive import upload_file

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "2048") or "2048")
FORMAT_LIMIT = int(os.getenv("FORMAT_LIMIT", "12") or "12")
YOUTUBE_COOKIE_FILE = Path(os.getenv("YOUTUBE_COOKIE_FILE", "/tmp/youtube-cookies.txt"))
YOUTUBE_PLAYER_CLIENTS = [x.strip() for x in os.getenv("YOUTUBE_PLAYER_CLIENTS", "android,ios,web").split(",") if x.strip()]
YOUTUBE_JS_RUNTIME = os.getenv("YOUTUBE_JS_RUNTIME", "node").strip()

JOBS = {}

BLOCKED_TERMS = {x.strip().lower() for x in os.getenv("BLOCKED_TITLE_TERMS", "").split(",") if x.strip()}

ALLOWED_SCHEMES = {"http", "https"}
YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
}
YOUTUBE_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240, 144)


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and (not ALLOWED_USER_IDS or user.id in ALLOWED_USER_IDS)


def host_matches(host: str, allowed_hosts: set[str]) -> bool:
    host = (host or "").lower().strip()
    return any(host == item or host.endswith("." + item) for item in allowed_hosts)


def is_youtube_url(url: str) -> bool:
    try:
        return host_matches(urlparse(url).netloc, YOUTUBE_HOSTS)
    except Exception:
        return False


def looks_like_url(text: str) -> bool:
    try:
        parsed = urlparse(text.strip())
        return parsed.scheme in ALLOWED_SCHEMES and bool(parsed.netloc)
    except Exception:
        return False


def blocked_title(title: str) -> bool:
    text = (title or "").lower()
    return any(term in text for term in BLOCKED_TERMS)


def safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name or "video")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "video"


def file_size_mb(path: str) -> float:
    return os.path.getsize(path) / 1024 / 1024


def prepare_youtube_cookie_file() -> str | None:
    existing = os.getenv("YOUTUBE_COOKIEFILE", "").strip()
    if existing:
        return existing
    text = os.getenv("YOUTUBE_COOKIES_TEXT", "").strip()
    b64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if b64:
        text = base64.b64decode(b64.encode("utf-8")).decode("utf-8")
    if not text:
        return None
    YOUTUBE_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    YOUTUBE_COOKIE_FILE.write_text(text, encoding="utf-8")
    return str(YOUTUBE_COOKIE_FILE)


def ydl_extra_opts(url: str) -> dict:
    if not is_youtube_url(url):
        return {}
    opts = {}
    cookiefile = prepare_youtube_cookie_file()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if YOUTUBE_PLAYER_CLIENTS:
        opts["extractor_args"] = {"youtube": {"player_client": YOUTUBE_PLAYER_CLIENTS}}
    if YOUTUBE_JS_RUNTIME and YOUTUBE_JS_RUNTIME.lower() not in {"0", "false", "off", "none", "no"}:
        opts["js_runtimes"] = {YOUTUBE_JS_RUNTIME: {"path": YOUTUBE_JS_RUNTIME}}
        opts["remote_components"] = {"ejs:github"}
    return opts


def extract_info(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignore_no_formats_error": True,
        **ydl_extra_opts(url),
    }
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def quality_label(fmt: dict) -> str:
    fmt_id = fmt.get("format_id") or "best"
    ext = fmt.get("ext") or "?"
    height = fmt.get("height")
    fps = fmt.get("fps")
    tbr = fmt.get("tbr") or fmt.get("vbr")
    parts = [fmt_id]
    if height:
        parts.append(f"{height}p")
    if fps:
        parts.append(f"{fps}fps")
    parts.append(ext)
    if tbr:
        parts.append(f"{int(tbr)}kbps")
    return " | ".join(str(x) for x in parts if x)


def candidate_formats(info: dict):
    formats = info.get("formats") or []
    out = []
    seen = set()
    for fmt in formats:
        fmt_id = fmt.get("format_id")
        if not fmt_id or fmt_id in seen:
            continue
        if fmt.get("vcodec") == "none":
            continue
        ext = fmt.get("ext") or ""
        if ext in {"mhtml", "storyboard"}:
            continue
        seen.add(fmt_id)
        out.append(fmt)
    out.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or f.get("vbr") or 0), reverse=True)
    return out[:FORMAT_LIMIT]


def youtube_format_selector(fmt_id: str) -> str:
    if fmt_id == "best":
        return "bestvideo+bestaudio/best"
    if fmt_id.startswith("height_"):
        height = int(fmt_id.removeprefix("height_"))
        return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/bestvideo+bestaudio/best"
    return "bestvideo+bestaudio/best"


def build_format_selector(url: str, fmt_id: str) -> str:
    if is_youtube_url(url):
        return youtube_format_selector(fmt_id)
    if fmt_id == "best":
        return "bestvideo+bestaudio/best"
    safe_fmt = str(fmt_id).replace("/", "").replace("\\", "")
    return f"{safe_fmt}+bestaudio/{safe_fmt}/bestvideo+bestaudio/best"


def find_downloaded_file(info: dict, workdir: str, ydl: YoutubeDL) -> str:
    requested = info.get("requested_downloads") or []
    for item in requested:
        filepath = item.get("filepath") or item.get("filename")
        if filepath and os.path.exists(filepath):
            return filepath
    filepath = ydl.prepare_filename(info)
    if os.path.exists(filepath):
        return filepath
    files = [p for p in Path(workdir).glob("*") if p.is_file()]
    if files:
        files.sort(key=lambda p: p.stat().st_size, reverse=True)
        return str(files[0])
    raise RuntimeError("Downloaded file not found")


def download_video(url: str, fmt_id: str, workdir: str):
    outtmpl = os.path.join(workdir, "%(title).100s.%(ext)s")
    opts = {
        "format": build_format_selector(url, fmt_id),
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        **ydl_extra_opts(url),
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return find_downloaded_file(info, workdir, ydl), info


def build_quality_buttons(url: str, info: dict) -> list[list[InlineKeyboardButton]]:
    buttons = [[InlineKeyboardButton("Best", callback_data="fmt:best")]]
    if is_youtube_url(url):
        for height in YOUTUBE_HEIGHTS:
            buttons.append([InlineKeyboardButton(f"{height}p", callback_data=f"fmt:height_{height}")])
        return buttons
    for fmt in candidate_formats(info):
        buttons.append([InlineKeyboardButton(quality_label(fmt)[:60], callback_data=f"fmt:{fmt.get('format_id')}")])
    return buttons


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    await update.message.reply_text("Send a supported public video URL. I will show qualities, download your choice, upload it to Google Drive, and send the link.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    url = (update.message.text or "").strip()
    if not looks_like_url(url):
        await update.message.reply_text("Send a valid video URL.")
        return
    status = await update.message.reply_text("Reading video info...")
    try:
        info = await asyncio.to_thread(extract_info, url)
    except Exception as e:
        await status.edit_text(f"Could not read video info: {type(e).__name__}: {e}")
        return
    title = info.get("title") or "video"
    if blocked_title(title):
        await status.edit_text("Blocked: title appears to contain unsafe terms.")
        return
    key = str(update.effective_user.id)
    JOBS[key] = {"url": url, "title": title}
    await status.edit_text(f"Title: {title}\nSelect quality:", reply_markup=InlineKeyboardMarkup(build_quality_buttons(url, info)))


async def format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not allowed(update):
        await query.message.reply_text("Access denied.")
        return
    fmt_id = (query.data or "").removeprefix("fmt:")
    key = str(update.effective_user.id)
    job = JOBS.get(key)
    if not job:
        await query.message.reply_text("Job expired. Send the URL again.")
        return
    await query.edit_message_text(f"Downloading selected quality: {fmt_id} ...")
    tempdir = tempfile.mkdtemp(prefix="ytbot-")
    try:
        filepath, info = await asyncio.to_thread(download_video, job["url"], fmt_id, tempdir)
        size = file_size_mb(filepath)
        if size > MAX_FILE_MB:
            raise RuntimeError(f"File is too large: {size:.1f}MB. Limit: {MAX_FILE_MB}MB")
        await query.message.reply_text(f"Uploading to Google Drive... ({size:.1f}MB)")
        mime, _ = mimetypes.guess_type(filepath)
        link = await asyncio.to_thread(upload_file, filepath, info.get("title") or job["title"], mime or "video/mp4")
        await query.message.reply_text(f"Uploaded:\n{link}")
    except Exception as e:
        await query.message.reply_text(f"Failed: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)
        JOBS.pop(key, None)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CallbackQueryHandler(format_callback, pattern=r"^fmt:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    print("yt-dlp Telegram Drive bot started.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
