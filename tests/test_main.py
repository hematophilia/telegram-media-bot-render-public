import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main


class FakeTikTokExtractor:
    def __init__(self, post: dict):
        self.post = post
        self.created_url = None
        self.extracted_post_id = None

    def initialize(self):
        pass

    def _create_url(self, user_id: str, post_id: str) -> str:
        self.created_url = (user_id, post_id)
        return f"https://www.tiktok.com/@{user_id}/photo/{post_id}"

    def _extract_web_data_and_status(self, webpage_url: str, post_id: str):
        self.extracted_post_id = post_id
        return self.post, 0


class FakeYoutubeDL:
    def __init__(self, extractor: FakeTikTokExtractor):
        self.extractor = extractor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_info_extractor(self, name: str):
        if name != "TikTok":
            raise AssertionError(f"Unexpected extractor: {name}")
        return self.extractor


class FakeStatusMessage:
    def __init__(self, events: list[tuple]):
        self.events = events

    async def edit_text(self, text: str):
        self.events.append(("edit_text", text))

    async def delete(self):
        self.events.append(("delete_status",))


class FakeMessage:
    def __init__(self, user_id: int = 123):
        self.events: list[tuple] = []
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id)

    async def answer(self, text: str):
        self.events.append(("answer", text))
        return FakeStatusMessage(self.events)

    async def answer_document(self, document, caption=None):
        self.events.append(("document", document, caption))

    async def answer_media_group(self, media):
        self.events.append(("media_group", media))

    async def answer_audio(self, audio):
        self.events.append(("audio", audio))

    async def answer_video(self, video, caption=None):
        self.events.append(("video", video, caption))


class FakeHttpResponse:
    def __init__(
        self,
        body: bytes = b"image-bytes",
        final_url: str | None = None,
        fail_after_first_chunk: bool = False,
    ):
        self.body = body
        self.final_url = final_url
        self.fail_after_first_chunk = fail_after_first_chunk
        self.headers = {"Content-Type": "image/jpeg"}
        self.read_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.final_url or "https://i.pinimg.com/image.jpg"

    def read(self, _size: int):
        self.read_count += 1
        if self.fail_after_first_chunk:
            if self.read_count == 1:
                return b"partial"
            raise OSError("connection interrupted")
        if self.read_count == 1:
            return self.body
        return b""


class FakePinterestDownloader:
    def __init__(self, response: FakeHttpResponse):
        self.response = response
        self.requested_urls: list[str] = []

    def urlopen(self, url: str):
        self.requested_urls.append(url)
        return self.response


class FakeTelegramBot:
    def __init__(self, events: list[tuple]):
        self.events = events
        self.downloaded_paths: list[Path] = []

    async def download(self, file: str, destination: Path):
        path = Path(destination)
        path.write_bytes(b"uploaded-video")
        self.downloaded_paths.append(path)
        self.events.append(("download", file, path))

    async def send_voice(self, chat_id: int, voice, caption: str):
        voice_path = Path(voice.path)
        self.events.append(
            ("send_voice", chat_id, voice_path, voice_path.is_file(), caption)
        )


class FakeUploadedVideoMessage(FakeMessage):
    def __init__(self, file_size: int | None = 100):
        super().__init__(user_id=456)
        self.video = SimpleNamespace(file_id="uploaded-video-id", file_size=file_size)
        self.video_note = None
        self.animation = None
        self.bot = FakeTelegramBot(self.events)


class CarouselExtractionTests(unittest.TestCase):
    def test_extracts_image_post_original_urls_and_attached_audio(self):
        image_urls = [
            "https://p16-sign-va.tiktokcdn.com/tos-useast5-a5-tx/original-one.jpeg",
            "https://p16-sign-va.tiktokcdn.com/tos-useast5-a5-tx/original-two.jpeg",
        ]
        audio_url = "https://sf16-ies-music.tiktokcdn.com/obj/tos-alisg/audio.mp3"
        extractor = FakeTikTokExtractor(
            {
                "imagePost": {
                    "images": [
                        {"imageURL": {"urlList": [image_urls[0]]}},
                        {"imageURL": {"urlList": [image_urls[1]]}},
                    ]
                },
                "music": {"playUrl": {"urlList": [audio_url]}},
            }
        )

        with patch.object(
            main.yt_dlp, "YoutubeDL", return_value=FakeYoutubeDL(extractor)
        ):
            carousel = main._extract_tiktok_carousel(
                "https://www.tiktok.com/@creator/photo/987654321"
            )

        self.assertIsNotNone(carousel)
        self.assertEqual(
            carousel.image_url_options, [[image_urls[0]], [image_urls[1]]]
        )
        self.assertEqual(carousel.audio_url, audio_url)
        self.assertEqual(extractor.created_url, ("creator", "987654321"))
        self.assertEqual(extractor.extracted_post_id, "987654321")


class CarouselDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._pending_request_count = 0
        self.rate_limit_tempdir = tempfile.TemporaryDirectory()
        self.rate_limit_patch = patch.dict(
            os.environ,
            {"RATE_LIMIT_DB_PATH": str(Path(self.rate_limit_tempdir.name) / "limits.sqlite3")},
        )
        self.rate_limit_patch.start()

    def tearDown(self):
        self.rate_limit_patch.stop()
        self.rate_limit_tempdir.cleanup()

    async def test_handler_sends_carousel_audio_then_next_link_in_order(self):
        message = FakeMessage()
        urls = [
            "https://www.tiktok.com/@creator/photo/111",
            "https://www.youtube.com/shorts/222",
        ]
        audio_url = "https://cdn.example/audio.mp3"
        image_path_holder: list[Path] = []
        audio_path_holder: list[Path] = []

        def download_images(_carousel, directory):
            image_paths = [
                Path(directory) / "image-one.jpg",
                Path(directory) / "image-two.jpg",
            ]
            for image_path in image_paths:
                image_path.write_bytes(b"image")
            image_path_holder.extend(image_paths)
            return image_paths

        def download_audio(_audio_url, directory):
            audio_path = Path(directory) / "soundtrack.mp3"
            audio_path.write_bytes(b"audio")
            audio_path_holder.append(audio_path)
            return audio_path

        def download_video(_url, directory):
            video_path = Path(directory) / "video.mp4"
            video_path.write_bytes(b"video")
            return video_path

        carousel = main.TikTokCarousel(
            image_url_options=[["https://cdn.example/image.jpg"]],
            audio_url=audio_url,
        )
        with (
            patch.object(main, "_message_urls", return_value=urls),
            patch.object(main, "_extract_tiktok_carousel", return_value=carousel),
            patch.object(main, "_download_tiktok_images", side_effect=download_images),
            patch.object(main, "_download_tiktok_audio", side_effect=download_audio),
            patch.object(main, "_download_video", side_effect=download_video),
        ):
            await main.video_handler(message)

        event_types = [event[0] for event in message.events]
        self.assertLess(event_types.index("media_group"), event_types.index("audio"))
        self.assertLess(event_types.index("audio"), event_types.index("video"))
        self.assertIn(("answer", "Понял, качаю по очереди."), message.events)
        self.assertIn(("answer", "Качаю 1 из 2..."), message.events)
        self.assertIn(("edit_text", "Качаю 2 из 2..."), message.events)
        media_group = next(
            event[1] for event in message.events if event[0] == "media_group"
        )
        self.assertEqual(len(media_group), 2)
        self.assertEqual(media_group[0].caption, main.CAROUSEL_CAPTION)
        video_event = next(event for event in message.events if event[0] == "video")
        self.assertEqual(video_event[2], main.VIDEO_CAPTION)
        self.assertFalse(image_path_holder[0].exists())
        self.assertFalse(audio_path_holder[0].exists())

    async def test_carousel_image_files_are_removed_after_successful_send(self):
        message = FakeMessage()
        with tempfile.TemporaryDirectory() as directory:
            photos = [Path(directory) / "one.jpg", Path(directory) / "two.jpg"]
            audio = Path(directory) / "soundtrack.mp3"
            for path in [*photos, audio]:
                path.write_bytes(b"media")

            with patch.object(main, "_download_tiktok_audio", return_value=audio):
                await main._send_tiktok_carousel(
                    message, photos, "https://cdn.example/audio.mp3", directory
                )

            self.assertFalse(any(path.exists() for path in [*photos, audio]))
        self.assertEqual(
            [event[0] for event in message.events], ["media_group", "audio"]
        )

    async def test_carousel_images_are_removed_when_album_send_fails(self):
        message = FakeMessage()

        async def fail_to_send(media):
            del media
            raise RuntimeError("Telegram send failed")

        message.answer_media_group = fail_to_send
        with tempfile.TemporaryDirectory() as directory:
            photos = [Path(directory) / "one.jpg", Path(directory) / "two.jpg"]
            for path in photos:
                path.write_bytes(b"image")

            with self.assertRaisesRegex(RuntimeError, "Telegram send failed"):
                await main._send_tiktok_carousel(message, photos, None, directory)

            self.assertFalse(any(path.exists() for path in photos))

    async def test_audio_file_is_removed_when_audio_send_fails(self):
        message = FakeMessage()

        async def fail_to_send(audio):
            del audio
            raise RuntimeError("Telegram audio send failed")

        message.answer_audio = fail_to_send
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "one.jpg"
            audio = Path(directory) / "soundtrack.mp3"
            photo.write_bytes(b"image")
            audio.write_bytes(b"audio")

            with (
                patch.object(main, "_download_tiktok_audio", return_value=audio),
                self.assertRaisesRegex(RuntimeError, "Telegram audio send failed"),
            ):
                await main._send_tiktok_carousel(
                    message, [photo], "https://cdn.example/audio.mp3", directory
                )

            self.assertFalse(photo.exists())
            self.assertFalse(audio.exists())


class ReplyTextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._pending_request_count = 0
        self.rate_limit_tempdir = tempfile.TemporaryDirectory()
        self.rate_limit_patch = patch.dict(
            os.environ,
            {"RATE_LIMIT_DB_PATH": str(Path(self.rate_limit_tempdir.name) / "limits.sqlite3")},
        )
        self.rate_limit_patch.start()

    def tearDown(self):
        self.rate_limit_patch.stop()
        self.rate_limit_tempdir.cleanup()

    async def test_start_command_uses_exact_reply(self):
        message = FakeMessage()

        await main.cmd_start(message)

        self.assertEqual(message.events, [("answer", "Кидай ссылки, скачаю.")])

    async def test_missing_link_uses_exact_error_reply(self):
        message = FakeMessage()

        with patch.object(main, "_message_urls", return_value=[]):
            await main.video_handler(message)

        self.assertEqual(
            message.events,
            [("answer", "Не качается — либо аккаунт закрыт, либо видео снесли.")],
        )

    async def test_download_failure_uses_exact_error_reply(self):
        message = FakeMessage()
        url = "https://www.youtube.com/shorts/123"

        with (
            patch.object(main, "_message_urls", return_value=[url]),
            patch.object(
                main,
                "_download_video",
                side_effect=RuntimeError("download failed"),
            ),
        ):
            await main.video_handler(message)

        self.assertIn(
            ("answer", "Не качается — либо аккаунт закрыт, либо видео снесли."),
            message.events,
        )
        self.assertIn(("answer", "Качаю 1 из 1..."), message.events)
        self.assertEqual(message.events[-1], ("delete_status",))

    async def test_oversized_media_uses_exact_reply_and_removes_status(self):
        message = FakeMessage()
        url = "https://www.youtube.com/shorts/123"

        with (
            patch.object(main, "_message_urls", return_value=[url]),
            patch.object(
                main,
                "_download_video",
                side_effect=main.MediaTooLargeError,
            ),
        ):
            await main.video_handler(message)

        self.assertIn(
            ("answer", "Весит больше 50 МБ, телеграм не дает отправить."),
            message.events,
        )
        self.assertIn(("answer", "Понял, качаю по очереди."), message.events)
        self.assertIn(("answer", "Качаю 1 из 1..."), message.events)
        self.assertEqual(message.events[-1], ("delete_status",))


class PinterestImageTests(unittest.TestCase):
    def test_prefers_original_rendition_and_returns_its_dimensions(self):
        original_url = "https://i.pinimg.com/originals/a/b/photo.jpg"
        largest_preview_url = "https://i.pinimg.com/1200x/preview.jpg"
        renditions = main._pinterest_image_renditions(
            {
                "thumbnails": [
                    {
                        "url": largest_preview_url,
                        "width": 1200,
                        "height": 1800,
                    },
                    {"url": original_url, "width": 800, "height": 600},
                    {
                        "url": "https://example.com/unsupported.jpg",
                        "width": 4000,
                        "height": 4000,
                    },
                ]
            }
        )
        downloader = FakePinterestDownloader(FakeHttpResponse(body=b"original-image"))

        with tempfile.TemporaryDirectory() as directory:
            image_path, selected = main._download_pinterest_image(
                downloader, renditions, directory
            )

            self.assertEqual(downloader.requested_urls, [original_url])
            self.assertEqual(selected.url, original_url)
            self.assertEqual((selected.width, selected.height), (800, 600))
            self.assertEqual(image_path.read_bytes(), b"original-image")

    def test_falls_back_to_largest_preview_if_original_is_too_large(self):
        original_url = "https://i.pinimg.com/originals/a/b/photo.jpg"
        largest_preview_url = "https://i.pinimg.com/1200x/preview.jpg"
        renditions = main._pinterest_image_renditions(
            {
                "thumbnails": [
                    {
                        "url": "https://i.pinimg.com/236x/small.jpg",
                        "width": 236,
                        "height": 354,
                    },
                    {
                        "url": largest_preview_url,
                        "width": 1200,
                        "height": 1800,
                    },
                    {"url": original_url, "width": 800, "height": 600},
                ]
            }
        )
        attempted_urls: list[str] = []

        def download_rendition(
            _downloader,
            image_url: str,
            directory: str,
            filename_stem: str = "pinterest-photo",
        ):
            attempted_urls.append(image_url)
            if image_url == original_url:
                raise main.MediaTooLargeError
            image_path = Path(directory) / "best-preview.jpg"
            image_path.write_bytes(b"preview")
            return image_path

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                main, "_download_pinterest_resource", side_effect=download_rendition
            ):
                image_path, selected = main._download_pinterest_image(
                    object(), renditions, directory
                )

            self.assertEqual(
                attempted_urls, [original_url, largest_preview_url]
            )
            self.assertEqual(selected.url, largest_preview_url)
            self.assertEqual((selected.width, selected.height), (1200, 1800))
            self.assertEqual(image_path.read_bytes(), b"preview")

    def test_rejects_redirect_to_unsupported_host(self):
        image_url = "https://i.pinimg.com/originals/a/b/photo.jpg"
        downloader = FakePinterestDownloader(
            FakeHttpResponse(final_url="https://attacker.example/photo.jpg")
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(main.yt_dlp.utils.DownloadError):
                main._download_pinterest_resource(downloader, image_url, directory)

            self.assertEqual(downloader.requested_urls, [image_url])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_removes_partial_image_when_download_is_interrupted(self):
        image_url = "https://i.pinimg.com/originals/a/b/photo.jpg"
        downloader = FakePinterestDownloader(
            FakeHttpResponse(fail_after_first_chunk=True)
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(OSError, "connection interrupted"):
                main._download_pinterest_resource(downloader, image_url, directory)

            self.assertFalse((Path(directory) / "pinterest-photo.jpg").exists())


class DirectVideoVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_sends_voice_and_cleans_temp_files_and_status(self):
        message = FakeUploadedVideoMessage()
        inspected_directories: list[Path] = []

        def extract_audio(video_path: Path, voice_path: Path):
            inspected_directories.append(video_path.parent)
            self.assertTrue(video_path.is_file())
            voice_path.write_bytes(b"voice-track")
            return True

        with patch.object(main, "_extract_voice_track", side_effect=extract_audio):
            await main.direct_video_voice_handler(message)

        self.assertIn(("answer", "Вытаскиваю звук..."), message.events)
        send_event = next(
            event for event in message.events if event[0] == "send_voice"
        )
        self.assertEqual(send_event[1], message.chat.id)
        self.assertTrue(send_event[3])
        self.assertEqual(send_event[4], "Лови.")
        self.assertFalse(inspected_directories[0].exists())
        self.assertEqual(message.events[-1], ("delete_status",))

    async def test_no_audio_uses_exact_reply_and_cleans_temp_files(self):
        message = FakeUploadedVideoMessage()
        inspected_directories: list[Path] = []

        def no_audio(video_path: Path, _voice_path: Path):
            inspected_directories.append(video_path.parent)
            return False

        with patch.object(main, "_extract_voice_track", side_effect=no_audio):
            await main.direct_video_voice_handler(message)

        self.assertIn(("answer", "Тут нет звуковой дорожки."), message.events)
        self.assertFalse(any(event[0] == "send_voice" for event in message.events))
        self.assertFalse(inspected_directories[0].exists())
        self.assertEqual(message.events[-1], ("delete_status",))

    async def test_oversized_video_uses_exact_reply_without_downloading(self):
        message = FakeUploadedVideoMessage(
            file_size=main.MAX_CLOUD_DOWNLOAD_BYTES + 1
        )

        await main.direct_video_voice_handler(message)

        self.assertEqual(
            message.events,
            [("answer", "Видео слишком тяжелое, не смогу вытащить звук.")],
        )
        self.assertEqual(message.bot.downloaded_paths, [])

    async def test_processing_error_uses_exact_reply_and_cleans_temp_files(self):
        message = FakeUploadedVideoMessage()
        inspected_directories: list[Path] = []

        def fail_to_extract(video_path: Path, _voice_path: Path):
            inspected_directories.append(video_path.parent)
            raise RuntimeError("ffmpeg failed")

        with patch.object(main, "_extract_voice_track", side_effect=fail_to_extract):
            await main.direct_video_voice_handler(message)

        self.assertIn(("answer", "Не удалось обработать видео."), message.events)
        self.assertFalse(inspected_directories[0].exists())
        self.assertEqual(message.events[-1], ("delete_status",))


if __name__ == "__main__":
    unittest.main()
