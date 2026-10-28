import os
import tempfile
import unittest
from pathlib import Path

import main


class QueueLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "limits.sqlite3"
        self.previous_database_path = os.environ.get("RATE_LIMIT_DB_PATH")
        os.environ["RATE_LIMIT_DB_PATH"] = str(self.database_path)
        main._pending_request_count = 0

    def tearDown(self):
        main._pending_request_count = 0
        if self.previous_database_path is None:
            os.environ.pop("RATE_LIMIT_DB_PATH", None)
        else:
            os.environ["RATE_LIMIT_DB_PATH"] = self.previous_database_path
        self.temp_dir.cleanup()

    def test_accepts_no_more_than_five_pending_requests(self):
        results = [
            main._reserve_request(user_id, now=0)
            for user_id in range(main.MAX_PENDING_REQUESTS + 1)
        ]
        self.assertEqual(results[:-1], [None] * main.MAX_PENDING_REQUESTS)
        self.assertEqual(results[-1], main.QUEUE_FULL_TEXT)
        self.assertEqual(main._pending_request_count, main.MAX_PENDING_REQUESTS)
        for _ in range(main.MAX_PENDING_REQUESTS):
            main._release_request()
        self.assertEqual(main._pending_request_count, 0)

    def test_queue_rejection_does_not_consume_user_rate_quota(self):
        for user_id in range(main.MAX_PENDING_REQUESTS):
            self.assertIsNone(main._reserve_request(user_id, now=0))
        self.assertEqual(main._reserve_request(999, now=1), main.QUEUE_FULL_TEXT)
        main._release_request()
        self.assertIsNone(main._reserve_request(999, now=2))

    def test_fourth_request_within_a_minute_is_rejected(self):
        for timestamp in (0, 10, 20):
            self.assertIsNone(main._reserve_request(123, now=timestamp))
            main._release_request()
        self.assertEqual(main._reserve_request(123, now=59.999), main.RATE_LIMIT_TEXT)
        self.assertEqual(main._pending_request_count, 0)
        self.assertIsNone(main._reserve_request(456, now=59.999))

    def test_quota_is_available_again_when_a_request_leaves_the_window(self):
        for timestamp in (0, 10, 20):
            self.assertIsNone(main._reserve_request(123, now=timestamp))
            main._release_request()
        self.assertIsNone(main._reserve_request(123, now=60))
        main._release_request()
        self.assertEqual(main._reserve_request(123, now=60.001), main.RATE_LIMIT_TEXT)

    def test_rate_limit_survives_process_restart(self):
        for timestamp in (1000, 1010, 1020):
            self.assertIsNone(main._reserve_request(123, now=timestamp))
            main._release_request()
        # Pending work is in-memory; accepted request history is persisted.
        main._pending_request_count = 0
        self.assertEqual(main._reserve_request(123, now=1030), main.RATE_LIMIT_TEXT)
        self.assertEqual(main._pending_request_count, 0)


if __name__ == "__main__":
    unittest.main()


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_failure_is_retried_after_backoff(self):
        from unittest.mock import AsyncMock, patch

        bot_context = AsyncMock()
        bot_context.__aenter__.return_value = object()
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}),
            patch.object(main, "_configure_logging"),
            patch.object(main, "Bot", return_value=bot_context),
            patch.object(
                main.dp,
                "start_polling",
                new_callable=AsyncMock,
                side_effect=[RuntimeError("temporary polling failure"), None],
            ) as polling,
            patch("asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            await main.main()

        self.assertEqual(polling.await_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_debug_log_is_written_to_rotating_file(self):
        import logging
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "bot.log"
            with patch.dict(os.environ, {"BOT_LOG_PATH": str(log_path)}):
                main._configure_logging()
                logger = logging.getLogger("test.reliability")
                logger.error("diagnostic test entry")
                for handler in logging.getLogger().handlers:
                    handler.flush()
                self.assertIn("diagnostic test entry", log_path.read_text())
                for handler in logging.getLogger().handlers[:]:
                    if getattr(handler, "baseFilename", None) == str(log_path):
                        logging.getLogger().removeHandler(handler)
                        handler.close()
