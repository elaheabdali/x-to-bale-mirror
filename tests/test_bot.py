"""Unit tests for formatter, deduplication storage, and tweet normalization."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.formatter import (
    NormalizedMedia,
    NormalizedQuotedTweet,
    NormalizedTweet,
    format_tweet_message,
    split_caption_and_followup,
)
from src.storage import StateStore
from src.x_client import XTimelineClient


class FormatterTests(unittest.TestCase):
    def test_format_retweet_and_quote_with_rtl_text(self) -> None:
        tweet = NormalizedTweet(
            id="1001",
            author_name="علی",
            author_username="ali_dev",
            text="سلام دنیا! این یک پست آزمایشی است #تست",
            url="https://x.com/ali_dev/status/1001",
            is_retweet=True,
            retweeted_by_name="Ela",
            retweeted_by_username="dontcallmeela",
            quoted_tweet=NormalizedQuotedTweet(
                id="999",
                author_name="Bob",
                author_username="bob",
                text="Original insight here",
                url="https://x.com/bob/status/999",
            ),
            possibly_sensitive=True,
        )
        msg = format_tweet_message(tweet)
        self.assertIn("Retweeted by", msg)
        self.assertIn("dontcallmeela", msg)
        self.assertIn("سلام دنیا!", msg)
        self.assertIn("Quoting", msg)
        self.assertIn("> Original insight here", msg)
        self.assertIn("https://x.com/ali_dev/status/1001", msg)

    def test_split_caption_and_followup_when_long(self) -> None:
        long_body = "A" * 1100
        tweet = NormalizedTweet(
            id="1002",
            author_name="Tester",
            author_username="tester",
            text=long_body,
            url="https://x.com/tester/status/1002",
            media=[NormalizedMedia(media_type="photo", url="https://pbs.twimg.com/media/test.jpg")],
        )
        caption, followup = split_caption_and_followup(tweet)
        self.assertLessEqual(len(caption), 1024)
        self.assertIsNotNone(followup)
        assert followup is not None
        self.assertIn(long_body, followup)


class StorageTests(unittest.TestCase):
    def test_sqlite_deduplication_lifecycle(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = str(Path(tmpdir) / "state.db")
                store = StateStore(database_url=None, sqlite_path=db_path)
                await store.init()
                try:
                    self.assertTrue(await store.is_empty())
                    await store.mark_many_processed(["1", "2", "2"])
                    self.assertFalse(await store.is_empty())
                    self.assertEqual(await store.get_processed_count(), 2)
                    self.assertTrue(await store.is_processed("1"))
                    self.assertFalse(await store.is_processed("3"))
                    seen = await store.get_processed_ids(["1", "2", "3"])
                    self.assertEqual(seen, {"1", "2"})
                finally:
                    await store.close()

        asyncio.run(_run())


class NormalizationTests(unittest.TestCase):
    def test_extract_media_and_expanded_urls_preserves_nsfw(self) -> None:
        raw = SimpleNamespace(
            id="555",
            user=SimpleNamespace(name="User", screen_name="user1"),
            retweeted_tweet=None,
            full_text="Check https://t.co/abc123 https://t.co/media99",
            urls=[{"url": "https://t.co/abc123", "expanded_url": "https://example.com/article"}],
            media=[
                SimpleNamespace(
                    type="video",
                    width=1280,
                    height=720,
                    streams=[
                        SimpleNamespace(content_type="video/mp4", bitrate=832000, url="https://video.twimg.com/low.mp4"),
                        SimpleNamespace(content_type="video/mp4", bitrate=2176000, url="https://video.twimg.com/high.mp4"),
                    ],
                )
            ],
            quote=None,
            possibly_sensitive=True,
            created_at="2026-11-20",
        )
        client = XTimelineClient(auth_token="tok", ct0="ct0")
        try:
            norm = client._normalize_tweet(raw)
            self.assertEqual(norm.text, "Check https://example.com/article")
            self.assertTrue(norm.possibly_sensitive)
            self.assertEqual(len(norm.media), 1)
            self.assertEqual(norm.media[0].url, "https://video.twimg.com/high.mp4")
        finally:
            asyncio.run(client.close())


if __name__ == "__main__":
    unittest.main()
