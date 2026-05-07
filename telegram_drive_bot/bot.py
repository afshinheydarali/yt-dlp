import asyncio
import base64
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

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
YTDLP_IMPERSONATE = os.getenv("YTDLP_IMPERSONATE", "chrome").strip()
COOKIE_FILE = Path(os.getenv("YTDLP_COOKIE_FILE", "/tmp/yt-dlp-cookies.txt"))

JOBS = {}

BLOCKED_TERMS = {
    "child", "children", "kid", "kids", "minor", "underage", "loli", "shota",
    "teen schoolgirl", "schoolgirl", "schoolboy", "15 years old", "14 years old",
    "13 years old", "12 years old", "11 years old", "10 years old",
}

ALLOWED_SCHEMES = {"http", "https"}


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and (not ALLOWED_USER_IDS or user.id in ALLOWED_USER_IDS)


def normalize_url(url: str) -> str:
    """Normalize known alternate domains before handing the URL to yt-dlp."""
    url = (url or "").strip()
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    host = (parsed.netloc or "").lower()
    # eporner.video often falls back to the generic extractor and Cloudflare 403.
    # The canonical domain has a dedicated yt-dlp extractor.
    if host in {"eporner.video", "www.eporner.video"}:
        parsed = parsed._replace(netloc="www.eporner.com")
        return urlunparse(parsed)
    return url


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


def prepare_cookie_file() -> str | None:
    """Create a Netscape cookies.txt file from env if provided."""
    existing = os.getenv("YTDLP_COOKIEFILE", "").strip()
    if existing:
        return existing

    text = os.getenv("YTDLP_COOKIES_TEXT", "").strip()
    b64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
    if b64:
        text = base64.b64decode(b64.encode("utf-8")).decode("utf-8")

    if not text:
        return None

    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(text, encoding="utf-8")
    return str(COOKIE_FILE)


def ydl_common_opts() -> dict:
    opts = {}
    cookiefile = prepare_cookie_file()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    opts.update(ydl_impersonation_opts())
    return opts


def ydl_impersonation_opts() -> dict:
    target = (YTDLP_IMPERSONATE or "").strip()
    if not target or target.lower() in {"0", "false", "off", "none", "no"}:
        return {}

    targets = [part.strip() for part in target.split(",") if part.strip()]
    if not targets:
        targets = ["chrome"]

    # Equivalent to CLI: --extractor-args "generic:impersonate=chrome"
    # Do not set the global `impersonate` option here; some yt-dlp builds assert
    # when it is passed as a plain string through the Python API.
    return {"extractor_args": {"generic": {"impersonate": targets}}}


def extract_info(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        **ydl_common_opts(),
    }
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def quality_label(fmt: dict) -> str:
    fmt_id = fmt.get("format_id") or "best"
    ext = fmt.get("ext") or "?"
    height = fmt.get("height")
    width = fmt.get("width")
    fps = fmt.get("fps")
    filesize = fmt.get("filesize") or fmt.get("filesize_approx")
    acodec = fmt.get("acodec")
    vcodec = fmt.get("vcodec")

    parts = [fmt_id]
    if height:
        parts.append(f"{height}p")
    elif width:
        parts.append(f"{width}w")
    if fps:
        parts.append(f"{fps}fps")
    parts.append(ext)
    if vcodec == "none":
        parts.append("audio")
    elif acodec == "none":
        parts.append("video-only")
    if filesize:
        parts.append(f"{filesize / 1024 / 1024:.1f}MB")
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
        height = fmt.get("height") or 0
        ext = fmt.get("ext") or ""
        if ext in {"mhtml", "storyboard"}:
            continue
        seen.add(fmt_id)
        out.append(fmt)

    out.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    return out[:FORMAT_LIMIT]


def download_video(url: str, fmt_id: str, workdir: str):
    outtmpl = os.path.join(workdir, "%(title).100s.%(ext)s")
    fmt = fmt_id
    if fmt_id != "best":
        fmt = f"{fmt_id}+ba/best[format_id={fmt_id}]/best"

    opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        **ydl_common_opts(),
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        requested = info.get("requested_downloads") or []
        if requested and requested[0].get("filepath"):
            return requested[0]["filepath"], info
        filepath = ydl.prepare_filename(info)
        if os.path.exists(filepath):
            return filepath, info
        stem = Path(filepath).with_suffix("").name
        matches = list(Path(workdir).glob(stem + ".*"))
        if matches:
            return str(matches[0]), info
        raise RuntimeError("Downloaded file not found")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return
    await update.message.reply_text(
        "Send a supported public video URL. I will show available qualities, download your choice, upload it to Google Drive, and send the link.\n\n"
        "DRM, private, paywalled, or unauthorized content is not supported."
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Access denied.")
        return

    url = normalize_url((update.message.text or "").strip())
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
        await status.edit_text("Blocked: title appears to contain unsafe underage terms.")
        return

    key = str(update.effective_user.id)
    JOBS[key] = {"url": url, "title": title}

    buttons = [[InlineKeyboardButton("Best", callback_data="fmt:best")]]
    for fmt in candidate_formats(info):
        label = quality_label(fmt)
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"fmt:{fmt.get('format_id')}")])

    await status.edit_text(
        f"Title: {title}\nSelect quality:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


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
