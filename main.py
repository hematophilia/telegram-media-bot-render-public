import asyncio
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InputMediaDocument, Message


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _configure_logging() -> None:
    log_path = Path(
        os.getenv(
            "BOT_LOG_PATH",
            str(Path(__file__).with_name(".data") / "bot.log"),
        )
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in root_logger.handlers
    ):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root_logger.addHandler(console_handler)
    if not any(
        isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename) == log_path.resolve()
        for handler in root_logger.handlers
    ):
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root_logger.addHandler(file_handler)

MAX_FILE_SIZE_BYTES = 48_000_000
MAX_CLOUD_DOWNLOAD_BYTES = 20_000_000
TELEGRAM_PHOTO_UPLOAD_LIMIT_BYTES = 10_000_000
DOWNLOAD_QUEUE_LOCK = asyncio.Lock()
MAX_PENDING_REQUESTS = 5
USER_REQUEST_LIMIT = 3
USER_REQUEST_WINDOW_SECONDS = 60
_pending_request_count = 0
URL_PATTERN = re.compile(r"https://[^\s<>]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,!?;:)]}»”’\"'"
TIKTOK_POST_PATTERN = re.compile(
    r"https?://(?:www\.)?tiktok\.com/@(?P<user>[\w.-]+)/"
    r"(?P<kind>video|photo)/(?P<id>\d+)",
    re.IGNORECASE,
)
PINTEREST_DOMAINS = (
    "pinterest.com",
    "pinterest.fr",
    "pinterest.de",
    "pinterest.ch",
    "pinterest.jp",
    "pinterest.cl",
    "pinterest.ca",
    "pinterest.it",
    "pinterest.co.uk",
    "pinterest.nz",
    "pinterest.ru",
    "pinterest.com.au",
    "pinterest.at",
    "pinterest.pt",
    "pinterest.co.kr",
    "pinterest.es",
    "pinterest.com.mx",
    "pinterest.dk",
    "pinterest.ph",
    "pinterest.th",
    "pinterest.com.uy",
    "pinterest.co",
    "pinterest.nl",
    "pinterest.info",
    "pinterest.kr",
    "pinterest.ie",
    "pinterest.vn",
    "pinterest.com.vn",
    "pinterest.ec",
    "pinterest.mx",
    "pinterest.in",
    "pinterest.pe",
    "pinterest.co.at",
    "pinterest.hu",
    "pinterest.co.in",
    "pinterest.co.nz",
    "pinterest.id",
    "pinterest.com.ec",
    "pinterest.com.py",
    "pinterest.tw",
    "pinterest.be",
    "pinterest.uk",
    "pinterest.com.bo",
    "pinterest.com.pe",
)
SUPPORTED_DOMAINS = (
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "pin.it",
    *PINTEREST_DOMAINS,
)
TIKTOK_CDN_DOMAINS = (
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokcdn-eu.com",
    "tiktokcdn-in.com",
    "byteoversea.com",
    "ibytedtos.com",
)
PINTEREST_CDN_DOMAINS = ("pinimg.com",)

START_TEXT = "Кидай ссылки, скачаю."
ACK_TEXT = "Понял, качаю по очереди."
QUEUE_FULL_TEXT = "Очередь занята — попробуй позже."
RATE_LIMIT_TEXT = "Слишком много запросов. Попробуй через минуту."
ERROR_TEXT = "Не качается — либо аккаунт закрыт, либо видео снесли."
TOO_LARGE_TEXT = "Весит больше 50 МБ, телеграм не дает отправить."
VIDEO_CAPTION = "Лови."
CAROUSEL_CAPTION = "Фотки из карусели, лови."
VOICE_STATUS_TEXT = "Вытаскиваю звук..."
VOICE_NO_AUDIO_TEXT = "Тут нет звуковой дорожки."
VOICE_TOO_LARGE_TEXT = "Видео слишком тяжелое, не смогу вытащить звук."
VOICE_ERROR_TEXT = "Не удалось обработать видео."

dp = Dispatcher()


def _rate_limit_database_path() -> Path:
    configured_path = os.getenv("RATE_LIMIT_DB_PATH")
    if configured_path:
        return Path(configured_path)
    return Path(__file__).with_name(".data") / "request_limits.sqlite3"


def _reserve_request(user_id: int, now: float | None = None) -> str | None:
    """Reserve a bounded queue slot or return the user-facing rejection."""
    global _pending_request_count

    # Monotonic clocks are process-local, so persisted timestamps use wall time.
    current_time = time.time() if now is None else now
    if _pending_request_count >= MAX_PENDING_REQUESTS:
        return QUEUE_FULL_TEXT

    database_path = _rate_limit_database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path, timeout=10) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS user_requests ("
            "user_id INTEGER NOT NULL, requested_at REAL NOT NULL)"
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM user_requests WHERE requested_at <= ?",
            (current_time - USER_REQUEST_WINDOW_SECONDS,),
        )
        (user_request_count,) = connection.execute(
            "SELECT COUNT(*) FROM user_requests WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if user_request_count >= USER_REQUEST_LIMIT:
            connection.commit()
            return RATE_LIMIT_TEXT

        connection.execute(
            "INSERT INTO user_requests (user_id, requested_at) VALUES (?, ?)",
            (user_id, current_time),
        )
        connection.commit()

    _pending_request_count += 1
    return None


def _release_request() -> None:
    global _pending_request_count
    if _pending_request_count <= 0:
        raise RuntimeError("Cannot release an unreserved request")
    _pending_request_count -= 1


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


def _utf16_slice(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode(
        "utf-16-le", errors="ignore"
    )


def _message_urls(message: Message) -> list[str]:
    text = message.text or message.caption or ""
    entities = message.entities if message.text is not None else message.caption_entities
    candidates: list[tuple[int, str]] = []

    for match in URL_PATTERN.finditer(text):
        start = len(text[: match.start()].encode("utf-16-le")) // 2
        candidates.append(
            (start, match.group(0).rstrip(TRAILING_PUNCTUATION))
        )

    # Keep clickable Telegram links in their original message order too.
    for entity in entities or []:
        if entity.type == "text_link" and entity.url:
            candidates.append((entity.offset, entity.url))
        elif entity.type == "url":
            raw_url = _utf16_slice(text, entity.offset, entity.length)
            if raw_url:
                url = raw_url if "://" in raw_url else f"https://{raw_url}"
                candidates.append((entity.offset, url.rstrip(TRAILING_PUNCTUATION)))

    ordered_urls = []
    seen = set()
    for _, url in sorted(candidates, key=lambda candidate: candidate[0]):
        if url and url not in seen:
            ordered_urls.append(url)
            seen.add(url)
    return ordered_urls


def _is_supported_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False

    if (
        parsed.scheme.lower() != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or not parsed.hostname
    ):
        return False

    host = parsed.hostname.rstrip(".").lower()
    if not any(host == domain or host.endswith(f".{domain}") for domain in SUPPORTED_DOMAINS):
        return False

    # Accept YouTube Shorts links and youtu.be share links, not arbitrary YouTube pages.
    if host == "youtube.com" or host.endswith(".youtube.com"):
        return parsed.path.startswith("/shorts/")
    return True


class MediaTooLargeError(Exception):
    """Raised when a downloaded media file exceeds the configured size limit."""


@dataclass(frozen=True)
class PinterestImageRendition:
    url: str
    width: int | None
    height: int | None


@dataclass(frozen=True)
class PinterestDownload:
    kind: str
    path: Path
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class PinterestCarousel:
    image_paths: list[Path]


def _limit_download_size(status: dict) -> None:
    if (
        status.get("status") == "downloading"
        and status.get("downloaded_bytes", 0) > MAX_FILE_SIZE_BYTES
    ):
        raise yt_dlp.utils.DownloadError("MEDIA_EXCEEDS_48_MB_LIMIT")


def _video_download_options(directory: str) -> dict:
    output_template = str(Path(directory) / "video.%(ext)s")
    return {
        "format": (
            f"best[ext=mp4][filesize<={MAX_FILE_SIZE_BYTES}]/"
            f"best[ext=mp4][filesize_approx<={MAX_FILE_SIZE_BYTES}]/"
            "best[ext=mp4]"
        ),
        "outtmpl": output_template,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "progress_hooks": [_limit_download_size],
    }


def _find_downloaded_video(directory: str) -> Path:
    video_files = [
        path
        for path in Path(directory).glob("video.*")
        if path.is_file() and path.suffix.lower() == ".mp4"
    ]
    if not video_files:
        raise yt_dlp.utils.DownloadError("No MP4 video was produced")

    video_path = max(video_files, key=lambda path: path.stat().st_mtime)
    if video_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        raise MediaTooLargeError
    return video_path


def _download_video(url: str, directory: str) -> Path:
    try:
        with yt_dlp.YoutubeDL(_video_download_options(directory)) as downloader:
            downloader.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as error:
        message = str(error).lower()
        if (
            "media_exceeds_48_mb_limit" in message
            or "max-filesize" in message
            or "larger than" in message
        ):
            raise MediaTooLargeError from error
        raise

    return _find_downloaded_video(directory)


def _is_pinterest_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return (
        host == "pin.it"
        or host.endswith(".pin.it")
        or any(
            host == domain or host.endswith(f".{domain}")
            for domain in PINTEREST_DOMAINS
        )
    )


def _is_pinterest_cdn_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in PINTEREST_CDN_DOMAINS
    )


def _pinterest_pin_id(downloader: yt_dlp.YoutubeDL, extractor, url: str) -> str:
    match = extractor._match_valid_url(url)
    if not match:
        with downloader.urlopen(url) as response:
            match = extractor._match_valid_url(response.geturl())
    if not match:
        raise yt_dlp.utils.DownloadError("Pinterest URL did not resolve to a pin")
    return match.group("id")


def _pinterest_data_has_video(data: dict) -> bool:
    videos = data.get("videos") or {}
    if isinstance(videos, dict) and videos.get("video_list"):
        return True

    story_pages = (data.get("story_pin_data") or {}).get("pages") or []
    for page in story_pages:
        if not isinstance(page, dict):
            continue
        for block in page.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            video = block.get("video") or {}
            if isinstance(video, dict) and video.get("video_list"):
                return True
    return False


def _pinterest_image_renditions(
    info: dict,
) -> list[PinterestImageRendition]:
    renditions: list[PinterestImageRendition] = []
    seen_urls = set()
    for thumbnail in info.get("thumbnails") or []:
        if not isinstance(thumbnail, dict):
            continue
        image_url = thumbnail.get("url")
        if (
            not isinstance(image_url, str)
            or image_url in seen_urls
            or not _is_pinterest_cdn_url(image_url)
        ):
            continue
        try:
            width = int(thumbnail["width"]) if thumbnail.get("width") else None
        except (TypeError, ValueError):
            width = None
        try:
            height = int(thumbnail["height"]) if thumbnail.get("height") else None
        except (TypeError, ValueError):
            height = None
        renditions.append(
            PinterestImageRendition(image_url, width, height)
        )
        seen_urls.add(image_url)

    renditions.sort(
        key=lambda item: (
            "/originals/" in urlsplit(item.url).path.lower(),
            (item.width or 0) * (item.height or 0),
        ),
        reverse=True,
    )
    return renditions


def _pinterest_image_object_renditions(
    image: dict,
) -> list[PinterestImageRendition]:
    image_data = image.get("images")
    if isinstance(image_data, dict):
        thumbnails = [
            rendition
            for rendition in image_data.values()
            if isinstance(rendition, dict)
        ]
    elif isinstance(image.get("url"), str):
        thumbnails = [image]
    else:
        thumbnails = []
    return _pinterest_image_renditions({"thumbnails": thumbnails})


def _pinterest_story_image_renditions(
    data: dict,
) -> list[list[PinterestImageRendition]]:
    """Return each distinct image in story-page order, with renditions."""
    story_pin_data = data.get("story_pin_data") or {}
    pages = story_pin_data.get("pages") or []
    if not isinstance(pages, list) or len(pages) < 2:
        return []

    page_images: list[list[PinterestImageRendition]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue

        current_page_images: list[list[PinterestImageRendition]] = []
        seen_page_images: set[str] = set()
        for block in page.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            image = block.get("image")
            if not isinstance(image, dict):
                continue
            renditions = _pinterest_image_object_renditions(image)
            if not renditions or renditions[0].url in seen_page_images:
                continue
            current_page_images.append(renditions)
            seen_page_images.add(renditions[0].url)

        # Pinterest includes the page's main image outside its blocks on some
        # Idea Pins. Prefer block images when available to avoid duplicating it.
        if not current_page_images:
            image = page.get("image")
            if isinstance(image, dict):
                renditions = _pinterest_image_object_renditions(image)
                if renditions:
                    current_page_images.append(renditions)

        page_images.extend(current_page_images)
    return page_images


def _download_pinterest_resource(
    downloader: yt_dlp.YoutubeDL,
    image_url: str,
    directory: str,
    filename_stem: str = "pinterest-photo",
) -> Path:
    if not _is_pinterest_cdn_url(image_url):
        raise yt_dlp.utils.DownloadError("Pinterest image URL is not from pinimg.com")

    with downloader.urlopen(image_url) as response:
        if not _is_pinterest_cdn_url(response.geturl()):
            raise yt_dlp.utils.DownloadError(
                "Pinterest image redirected to an unsupported host"
            )

        content_type = response.headers.get("Content-Type", "")
        extension = _media_extension(image_url, content_type, "image")
        output_path = Path(directory) / f"{filename_stem}{extension}"
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_FILE_SIZE_BYTES:
                    raise MediaTooLargeError
            except ValueError:
                pass

        total_bytes = 0
        try:
            with output_path.open("wb") as output_file:
                while chunk := response.read(64 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_FILE_SIZE_BYTES:
                        raise MediaTooLargeError
                    output_file.write(chunk)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise

    if total_bytes == 0:
        output_path.unlink(missing_ok=True)
        raise yt_dlp.utils.DownloadError("Pinterest returned an empty image")
    return output_path


def _download_pinterest_image(
    downloader: yt_dlp.YoutubeDL,
    renditions: list[PinterestImageRendition],
    directory: str,
    filename_stem: str = "pinterest-photo",
) -> tuple[Path, PinterestImageRendition]:
    oversized_rendition_found = False
    for rendition in renditions:
        try:
            return (
                _download_pinterest_resource(
                    downloader, rendition.url, directory, filename_stem
                ),
                rendition,
            )
        except MediaTooLargeError:
            oversized_rendition_found = True
        except Exception:
            continue

    if oversized_rendition_found:
        raise MediaTooLargeError
    raise yt_dlp.utils.DownloadError("Pinterest image could not be downloaded")


def _download_pinterest_carousel_images(
    downloader: yt_dlp.YoutubeDL,
    image_renditions: list[list[PinterestImageRendition]],
    directory: str,
) -> PinterestCarousel:
    paths: list[Path] = []
    try:
        for index, renditions in enumerate(image_renditions, start=1):
            image_path, _ = _download_pinterest_image(
                downloader,
                renditions,
                directory,
                filename_stem=f"pinterest-page-{index:03d}",
            )
            paths.append(image_path)
    except Exception:
        for path in paths:
            path.unlink(missing_ok=True)
        raise
    return PinterestCarousel(paths)


def _download_pinterest_pin(
    url: str, directory: str
) -> PinterestDownload | PinterestCarousel:
    with yt_dlp.YoutubeDL(_video_download_options(directory)) as downloader:
        extractor = downloader.get_info_extractor("Pinterest")
        extractor.initialize()
        pin_id = _pinterest_pin_id(downloader, extractor, url)
        data = extractor._call_api(
            "Pin",
            pin_id,
            {"field_set_key": "unauth_react_main_pin", "id": pin_id},
        )["data"]
        story_images = _pinterest_story_image_renditions(data)
        if story_images and not _pinterest_data_has_video(data):
            return _download_pinterest_carousel_images(
                downloader, story_images, directory
            )

        info = extractor._extract_video(data)

        if (
            info.get("formats")
            or _pinterest_data_has_video(data)
            or info.get("_type") == "url_transparent"
        ):
            downloader.process_ie_result(info, download=True)
            return PinterestDownload("video", _find_downloaded_video(directory))

        renditions = _pinterest_image_renditions(info)
        if not renditions:
            raise yt_dlp.utils.DownloadError("Pinterest pin has no downloadable image")

        image_path, rendition = _download_pinterest_image(
            downloader, renditions, directory
        )
        return PinterestDownload(
            "photo",
            image_path,
            width=rendition.width,
            height=rendition.height,
        )


def _pinterest_photo_can_be_sent_as_photo(media: PinterestDownload) -> bool:
    if (
        media.path.suffix.lower() not in {".jpg", ".jpeg"}
        or media.path.stat().st_size > TELEGRAM_PHOTO_UPLOAD_LIMIT_BYTES
        or not media.width
        or not media.height
    ):
        return False

    longest_side = max(media.width, media.height)
    shortest_side = min(media.width, media.height)
    return (
        media.width + media.height <= 10_000
        and longest_side <= shortest_side * 20
    )


@dataclass(frozen=True)
class TikTokCarousel:
    image_url_options: list[list[str]]
    audio_url: str | None


def _is_tiktok_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def _is_tiktok_cdn_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False

    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in TIKTOK_CDN_DOMAINS
    )


def _image_url_options(image: dict) -> list[str]:
    image_url = (
        image.get("imageURL")
        or image.get("imageUrl")
        or image.get("image_url")
        or {}
    )
    if not isinstance(image_url, dict):
        return []

    urls = image_url.get("urlList") or image_url.get("url_list") or []
    if isinstance(urls, str):
        urls = [urls]
    return [
        url
        for url in urls
        if isinstance(url, str) and _is_tiktok_cdn_url(url)
    ]


def _tiktok_audio_url(post: dict) -> str | None:
    music = post.get("music") or {}
    audio_url = music.get("playUrl") or music.get("play_url")
    if isinstance(audio_url, dict):
        audio_url = audio_url.get("urlList") or audio_url.get("url_list") or []
    if isinstance(audio_url, list):
        audio_url = next(
            (url for url in audio_url if isinstance(url, str)), None
        )
    return audio_url if isinstance(audio_url, str) and audio_url else None


def _extract_tiktok_carousel(url: str) -> TikTokCarousel | None:
    """Read TikTok's raw post data through yt-dlp's TikTok extractor."""
    with yt_dlp.YoutubeDL(
        {
            "quiet": True,
            "no_warnings": True,
            "cachedir": False,
            "socket_timeout": 30,
        }
    ) as downloader:
        extractor = downloader.get_info_extractor("TikTok")
        extractor.initialize()

        match = TIKTOK_POST_PATTERN.search(url)
        if match:
            user_id = match.group("user")
            post_id = match.group("id")
        else:
            match = extractor._match_valid_url(url)
            if not match:
                info = downloader.extract_info(url, download=False)
                canonical_url = info.get("webpage_url") or info.get("original_url")
                if not canonical_url:
                    return None
                match = TIKTOK_POST_PATTERN.search(canonical_url)
                if match:
                    user_id = match.group("user")
                    post_id = match.group("id")
                else:
                    match = extractor._match_valid_url(canonical_url)
                    if not match:
                        return None
                    user_id = match.group("user_id")
                    post_id = match.group("id")
            else:
                user_id = match.group("user_id")
                post_id = match.group("id")

        webpage_url = extractor._create_url(user_id, post_id)
        post, status = extractor._extract_web_data_and_status(webpage_url, post_id)
        if not post or status != 0:
            return None

        image_post = post.get("imagePost") or post.get("image_post") or {}
        images = image_post.get("images") or []
        if not images:
            return None

        image_options = [
            _image_url_options(image)
            for image in images
            if isinstance(image, dict)
        ]
        if not image_options or any(not options for options in image_options):
            raise yt_dlp.utils.DownloadError("TikTok carousel images are unavailable")

        return TikTokCarousel(
            image_url_options=image_options,
            audio_url=_tiktok_audio_url(post),
        )


def _media_extension(url: str, content_type: str, media_kind: str) -> str:
    parsed = urlsplit(url)
    mime_type = content_type.split(";", 1)[0].strip().lower()
    if media_kind == "audio":
        mime_hint = parse_qs(parsed.query).get("mime_type", [""])[0]
        if mime_hint:
            mime_type = mime_hint.replace("_", "/").lower()

    extensions = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/aac": ".aac",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }
    extension = extensions.get(mime_type)
    allowed_extensions = (
        {".jpg", ".jpeg", ".png", ".webp"}
        if media_kind == "image"
        else {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"}
    )

    if not extension:
        extension = Path(parsed.path).suffix.lower()
        if extension not in allowed_extensions:
            guessed = mimetypes.guess_extension(mime_type, strict=False)
            extension = guessed or ""
    if extension == ".jpeg":
        extension = ".jpg"
    if extension not in allowed_extensions:
        raise yt_dlp.utils.DownloadError("TikTok returned an unsupported media type")
    return extension


def _download_tiktok_resource(
    downloader: yt_dlp.YoutubeDL,
    url: str,
    directory: str,
    filename_stem: str,
    media_kind: str,
) -> Path:
    if not _is_tiktok_cdn_url(url):
        raise yt_dlp.utils.DownloadError("TikTok media URL is not from a supported CDN")

    with downloader.urlopen(url) as response:
        if not _is_tiktok_cdn_url(response.geturl()):
            raise yt_dlp.utils.DownloadError("TikTok media redirected to an unsupported host")

        content_type = response.headers.get("Content-Type", "")
        extension = _media_extension(url, content_type, media_kind)
        output_path = Path(directory) / f"{filename_stem}{extension}"

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_FILE_SIZE_BYTES:
                    raise MediaTooLargeError
            except ValueError:
                pass

        total_bytes = 0
        try:
            with output_path.open("wb") as output_file:
                while chunk := response.read(64 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_FILE_SIZE_BYTES:
                        raise MediaTooLargeError
                    output_file.write(chunk)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise

    if total_bytes == 0:
        output_path.unlink(missing_ok=True)
        raise yt_dlp.utils.DownloadError("TikTok returned an empty media file")
    return output_path


def _download_tiktok_images(
    carousel: TikTokCarousel, directory: str
) -> list[Path]:
    paths = []
    with yt_dlp.YoutubeDL(
        {"quiet": True, "no_warnings": True, "cachedir": False, "socket_timeout": 30}
    ) as downloader:
        for index, url_options in enumerate(carousel.image_url_options, start=1):
            for url in url_options:
                try:
                    paths.append(
                        _download_tiktok_resource(
                            downloader,
                            url,
                            directory,
                            f"image_{index:03d}",
                            "image",
                        )
                    )
                    break
                except MediaTooLargeError:
                    raise
                except Exception:
                    continue
            else:
                raise yt_dlp.utils.DownloadError(
                    "A TikTok carousel image could not be downloaded"
                )
    return paths


def _download_tiktok_audio(url: str, directory: str) -> Path:
    with yt_dlp.YoutubeDL(
        {"quiet": True, "no_warnings": True, "cachedir": False, "socket_timeout": 30}
    ) as downloader:
        return _download_tiktok_resource(
            downloader, url, directory, "soundtrack", "audio"
        )


def _album_chunks(paths: list[Path]) -> list[list[Path]]:
    chunks = [paths[index : index + 10] for index in range(0, len(paths), 10)]
    if len(chunks) > 1 and len(chunks[-1]) == 1:
        chunks[-1].insert(0, chunks[-2].pop())
    return chunks


async def _send_pinterest_carousel(
    message: Message,
    image_paths: list[Path],
) -> None:
    try:
        for chunk_index, chunk in enumerate(_album_chunks(image_paths)):
            if len(chunk) == 1:
                await message.answer_document(
                    document=FSInputFile(chunk[0]),
                    caption=CAROUSEL_CAPTION,
                )
                continue

            media = [
                InputMediaDocument(
                    media=FSInputFile(path),
                    caption=CAROUSEL_CAPTION
                    if chunk_index == 0 and item_index == 0
                    else None,
                )
                for item_index, path in enumerate(chunk)
            ]
            await message.answer_media_group(media=media)
    finally:
        for path in image_paths:
            path.unlink(missing_ok=True)


async def _send_tiktok_carousel(
    message: Message,
    photo_paths: list[Path],
    audio_url: str | None,
    directory: str,
) -> None:
    for chunk_index, chunk in enumerate(_album_chunks(photo_paths)):
        try:
            if len(chunk) == 1:
                await message.answer_document(
                    document=FSInputFile(chunk[0]),
                    caption=CAROUSEL_CAPTION,
                )
            else:
                media = [
                    InputMediaDocument(
                        media=FSInputFile(path),
                        caption=CAROUSEL_CAPTION
                        if chunk_index == 0 and item_index == 0
                        else None,
                    )
                    for item_index, path in enumerate(chunk)
                ]
                await message.answer_media_group(media=media)
        finally:
            for path in chunk:
                path.unlink(missing_ok=True)

    if audio_url:
        audio_path = await asyncio.to_thread(
            _download_tiktok_audio, audio_url, directory
        )
        try:
            await message.answer_audio(audio=FSInputFile(audio_path))
        finally:
            audio_path.unlink(missing_ok=True)


def _extract_voice_track(video_path: Path, voice_path: Path) -> bool:
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if probe.returncode != 0:
        raise RuntimeError("ffprobe could not inspect the uploaded video")
    if not probe.stdout.strip():
        return False

    conversion = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "libopus",
            "-b:a",
            "48k",
            "-application",
            "voip",
            "-f",
            "ogg",
            str(voice_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if conversion.returncode != 0 or not voice_path.is_file():
        raise RuntimeError("ffmpeg could not create an OGG/Opus voice message")
    return True


@dp.message(F.video | F.video_note | F.animation)
async def direct_video_voice_handler(message: Message) -> None:
    media = message.video or message.video_note or message.animation
    if media is None:
        return

    if media.file_size is not None and media.file_size > MAX_CLOUD_DOWNLOAD_BYTES:
        await message.answer(VOICE_TOO_LARGE_TEXT)
        return

    status_message = await message.answer(VOICE_STATUS_TEXT)
    try:
        async with DOWNLOAD_QUEUE_LOCK:
            with tempfile.TemporaryDirectory(
                prefix="telegram-voice-"
            ) as temp_dir:
                video_path = Path(temp_dir) / "input.mp4"
                voice_path = Path(temp_dir) / "voice.ogg"

                await message.bot.download(
                    file=media.file_id,
                    destination=video_path,
                )
                if not video_path.is_file():
                    raise RuntimeError("Telegram did not provide the uploaded video")
                if video_path.stat().st_size > MAX_CLOUD_DOWNLOAD_BYTES:
                    raise MediaTooLargeError

                has_audio = await asyncio.to_thread(
                    _extract_voice_track, video_path, voice_path
                )
                if not has_audio:
                    await message.answer(VOICE_NO_AUDIO_TEXT)
                    return

                await message.bot.send_voice(
                    chat_id=message.chat.id,
                    voice=FSInputFile(voice_path),
                    caption=VIDEO_CAPTION,
                )
    except MediaTooLargeError:
        await message.answer(VOICE_TOO_LARGE_TEXT)
    except Exception as error:
        if "file is too big" in str(error).lower():
            await message.answer(VOICE_TOO_LARGE_TEXT)
            return
        logging.exception(
            "Не удалось извлечь звук из сообщения (%s).",
            type(error).__name__,
        )
        await message.answer(VOICE_ERROR_TEXT)
    finally:
        try:
            await status_message.delete()
        except Exception:
            logging.debug("Не удалось удалить сообщение о прогрессе.")


@dp.message()
async def video_handler(message: Message) -> None:
    urls = _message_urls(message)
    if not urls:
        await message.answer(ERROR_TEXT)
        return

    user_id = message.from_user.id if message.from_user else message.chat.id
    rejection = _reserve_request(user_id)
    if rejection:
        await message.answer(rejection)
        return

    try:
        await message.answer(ACK_TEXT)
        total = len(urls)

        # One lock serializes complete messages so different users' downloads never overlap.
        async with DOWNLOAD_QUEUE_LOCK:
            status_message = None
            try:
                for index, url in enumerate(urls, start=1):
                    if not _is_supported_url(url):
                        await message.answer(ERROR_TEXT)
                        continue

                    status_text = f"Качаю {index} из {total}..."
                    if status_message is None:
                        status_message = await message.answer(status_text)
                    else:
                        await status_message.edit_text(status_text)

                    try:
                        with tempfile.TemporaryDirectory(
                            prefix="telegram-media-"
                        ) as temp_dir:
                            carousel = None
                            if _is_tiktok_url(url):
                                try:
                                    carousel = await asyncio.to_thread(
                                        _extract_tiktok_carousel, url
                                    )
                                except yt_dlp.utils.ExtractorError:
                                    # Fall back to the standard yt-dlp video path.
                                    carousel = None

                            if carousel:
                                photo_paths = await asyncio.to_thread(
                                    _download_tiktok_images, carousel, temp_dir
                                )
                                await _send_tiktok_carousel(
                                    message,
                                    photo_paths,
                                    carousel.audio_url,
                                    temp_dir,
                                )
                            elif _is_pinterest_url(url):
                                pinterest_media = await asyncio.to_thread(
                                    _download_pinterest_pin, url, temp_dir
                                )
                                if isinstance(pinterest_media, PinterestCarousel):
                                    await _send_pinterest_carousel(
                                        message, pinterest_media.image_paths
                                    )
                                else:
                                    if pinterest_media.kind == "photo":
                                        if _pinterest_photo_can_be_sent_as_photo(
                                            pinterest_media
                                        ):
                                            await message.answer_photo(
                                                photo=FSInputFile(
                                                    pinterest_media.path
                                                ),
                                                caption=VIDEO_CAPTION,
                                            )
                                        else:
                                            await message.answer_document(
                                                document=FSInputFile(
                                                    pinterest_media.path
                                                ),
                                                caption=VIDEO_CAPTION,
                                            )
                                    else:
                                        await message.answer_video(
                                            video=FSInputFile(
                                                pinterest_media.path
                                            ),
                                            caption=VIDEO_CAPTION,
                                        )
                                    pinterest_media.path.unlink(missing_ok=True)
                            else:
                                video_path = await asyncio.to_thread(
                                    _download_video, url, temp_dir
                                )
                                await message.answer_video(
                                    video=FSInputFile(video_path),
                                    caption=VIDEO_CAPTION,
                                )
                    except MediaTooLargeError:
                        await message.answer(TOO_LARGE_TEXT)
                    except Exception as error:
                        logging.exception(
                            "Не удалось обработать ссылку (%s).",
                            type(error).__name__,
                        )
                        await message.answer(ERROR_TEXT)

            finally:
                if status_message is not None:
                    try:
                        await status_message.delete()
                    except Exception:
                        logging.debug("Не удалось удалить сообщение о прогрессе.")
    finally:
        _release_request()


async def main() -> None:
    _configure_logging()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Добавьте токен бота в Replit Secrets."
        )

    retry_delay = 2
    while True:
        try:
            async with Bot(token=token) as bot:
                logging.info("Запускаю polling Telegram-бота.")
                await dp.start_polling(bot)
            logging.info("Polling остановлен штатно; завершаю процесс.")
            return
        except TelegramUnauthorizedError:
            logging.exception(
                "Telegram отклонил токен. Проверьте TELEGRAM_BOT_TOKEN;"
                " повторный запуск остановлен."
            )
            raise
        except asyncio.CancelledError:
            logging.info("Polling отменён, завершаю работу.")
            raise
        except Exception:
            logging.exception(
                "Сбой polling. Перезапущу соединение через %s сек.",
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
