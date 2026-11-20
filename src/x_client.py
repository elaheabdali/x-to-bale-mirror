"""X (Twitter) authenticated Home Timeline client and media downloader."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
from twikit import Client
from twikit.errors import Forbidden, TooManyRequests, Unauthorized

from src.formatter import NormalizedMedia, NormalizedQuotedTweet, NormalizedTweet

logger = logging.getLogger(__name__)

_TRAILING_TCO_RE = re.compile(r"\s*https://t\.co/\w+\s*$")


class XAuthError(Exception):
    """Raised when X cookies are invalid or expired (401/403)."""


class XRateLimitError(Exception):
    """Raised when X returns HTTP 429 rate-limit."""


class MediaTooLargeError(Exception):
    """Raised when a remote media file exceeds the configured upload size limit."""


class XTimelineClient:
    """Cookie-authenticated X client fetching the chronological Following feed."""

    def __init__(
        self,
        auth_token: str,
        ct0: str,
        target_username: str = "dontcallmeela",
        temp_dir: str = "/tmp/x_bale_media",
        max_video_size_mb: int = 45,
        language: str = "en-US",
    ) -> None:
        self.auth_token = auth_token
        self.ct0 = ct0
        self.target_username = target_username.lstrip("@")
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.max_video_bytes = max_video_size_mb * 1024 * 1024
        self.language = language

        self.client = self._build_client()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )

    def _build_client(self) -> Client:
        try:
            client = Client(self.language, impersonate="chrome124")
        except TypeError:
            client = Client(self.language)
        client.set_cookies({"auth_token": self.auth_token, "ct0": self.ct0})

        # Guard against X returning an anonymous stub page without ondemand.s
        ct = getattr(client, "client_transaction", None)
        if ct is not None:
            orig_init = ct.init
            orig_gen = ct.generate_transaction_id

            async def _safe_init(session: Any, headers: Any) -> None:
                try:
                    await orig_init(session, headers)
                except Exception as exc:
                    logger.warning("ClientTransaction.init skipped (%s)", exc)

            def _safe_gen(method: str, path: str, *args: Any, **kwargs: Any) -> str:
                try:
                    return orig_gen(method=method, path=path, *args, **kwargs)
                except Exception:
                    return ""

            ct.init = _safe_init  # type: ignore[method-assign]
            ct.generate_transaction_id = _safe_gen  # type: ignore[method-assign]

        return client

    async def close(self) -> None:
        await self._http.aclose()

    async def fetch_home_timeline(self, count: int = 20) -> list[NormalizedTweet]:
        """Fetch Home Timeline ('Following' feed) in chronological order (oldest first)."""
        try:
            raw_tweets = await self.client.get_latest_timeline(count=count)
        except (Unauthorized, Forbidden) as exc:
            raise XAuthError(f"X authentication failed (cookies may be expired): {exc}") from exc
        except TooManyRequests as exc:
            raise XRateLimitError(f"X rate limit reached: {exc}") from exc
        except Exception as exc:
            msg = str(exc).lower()
            if "401" in msg or "403" in msg or "unauthorized" in msg:
                raise XAuthError(f"X authentication failed: {exc}") from exc
            if "429" in msg or "rate limit" in msg:
                raise XRateLimitError(f"X rate limit reached: {exc}") from exc
            if "key_byte" in msg or "clienttransaction" in msg or "'key'" in msg:
                logger.warning("Resetting X client after ClientTransaction error: %s", exc)
                self.client = self._build_client()
            raise

        normalized: list[NormalizedTweet] = []
        for raw in raw_tweets:
            try:
                normalized.append(self._normalize_tweet(raw))
            except Exception:
                logger.exception(
                    "Failed to normalize tweet id=%s; skipping",
                    getattr(raw, "id", "unknown"),
                )

        # Reverse so oldest unseen tweets are delivered first in chronological order.
        normalized.reverse()
        return normalized

    def _normalize_tweet(self, raw: Any) -> NormalizedTweet:
        """Convert a twikit Tweet object into a NormalizedTweet (zero content filtering)."""
        outer_id = str(raw.id)
        outer_user = getattr(raw, "user", None)
        outer_name = getattr(outer_user, "name", "") or ""
        outer_username = getattr(outer_user, "screen_name", "") or "unknown"

        rt = getattr(raw, "retweeted_tweet", None)
        is_retweet = rt is not None
        source = rt if is_retweet else raw

        src_user = getattr(source, "user", None)
        author_name = getattr(src_user, "name", "") or outer_name
        author_username = getattr(src_user, "screen_name", "") or outer_username
        source_id = str(getattr(source, "id", outer_id))

        text = self._extract_expanded_text(source)
        media = self._extract_media(source)
        quoted = self._extract_quoted_tweet(source)

        # Strip trailing t.co link if it was only a media/quote wrapper.
        if media or quoted:
            text = _TRAILING_TCO_RE.sub("", text).strip()

        return NormalizedTweet(
            id=outer_id,
            author_name=author_name,
            author_username=author_username,
            text=text,
            url=f"https://x.com/{author_username}/status/{source_id}",
            created_at=str(getattr(raw, "created_at", "") or ""),
            is_retweet=is_retweet,
            retweeted_by_name=outer_name if is_retweet else None,
            retweeted_by_username=outer_username if is_retweet else None,
            quoted_tweet=quoted,
            media=media,
            possibly_sensitive=bool(getattr(source, "possibly_sensitive", False)),
        )

    @staticmethod
    def _extract_expanded_text(tweet: Any) -> str:
        """Prefer full note_tweet text if available and expand t.co URLs."""
        text = getattr(tweet, "full_text", None) or getattr(tweet, "text", "") or ""

        urls = getattr(tweet, "urls", None) or []
        for url_obj in urls:
            if isinstance(url_obj, dict):
                short_url = url_obj.get("url")
                expanded = url_obj.get("expanded_url")
            else:
                short_url = getattr(url_obj, "url", None)
                expanded = getattr(url_obj, "expanded_url", None)
            if short_url and expanded:
                text = text.replace(short_url, expanded)
        return text

    @staticmethod
    def _extract_quoted_tweet(tweet: Any) -> NormalizedQuotedTweet | None:
        quote = getattr(tweet, "quote", None)
        if quote is None:
            return None
        q_user = getattr(quote, "user", None)
        q_username = getattr(q_user, "screen_name", "") or "unknown"
        q_name = getattr(q_user, "name", "") or q_username
        q_id = str(getattr(quote, "id", ""))
        q_text = XTimelineClient._extract_expanded_text(quote)
        return NormalizedQuotedTweet(
            id=q_id,
            author_name=q_name,
            author_username=q_username,
            text=q_text,
            url=f"https://x.com/{q_username}/status/{q_id}" if q_id else "",
        )

    @staticmethod
    def _extract_media(tweet: Any) -> list[NormalizedMedia]:
        """Extract all photos, videos, and animated GIFs without any NSFW filtering."""
        raw_media = getattr(tweet, "media", None) or []
        items: list[NormalizedMedia] = []

        for m in raw_media:
            m_type = getattr(m, "type", None)
            if not m_type and isinstance(m, dict):
                m_type = m.get("type")

            if m_type == "photo":
                url = (
                    getattr(m, "media_url", None)
                    or getattr(m, "media_url_https", None)
                    or (m.get("media_url_https") if isinstance(m, dict) else None)
                )
                if url:
                    items.append(
                        NormalizedMedia(
                            media_type="photo",
                            url=url,
                            width=getattr(m, "width", None),
                            height=getattr(m, "height", None),
                        )
                    )
            elif m_type in ("video", "animated_gif"):
                best_url = XTimelineClient._pick_best_mp4_url(m)
                if best_url:
                    items.append(
                        NormalizedMedia(
                            media_type=m_type,
                            url=best_url,
                            width=getattr(m, "width", None),
                            height=getattr(m, "height", None),
                        )
                    )
        return items

    @staticmethod
    def _pick_best_mp4_url(media_obj: Any) -> str | None:
        """Select the highest-bitrate MP4 stream for a video or animated GIF."""
        streams = getattr(media_obj, "streams", None) or []
        mp4_streams = []
        for s in streams:
            ctype = getattr(s, "content_type", "") or ""
            url = getattr(s, "url", None)
            bitrate = getattr(s, "bitrate", 0) or 0
            if url and ("mp4" in ctype or ".mp4" in url):
                mp4_streams.append((int(bitrate), url))

        if mp4_streams:
            mp4_streams.sort(key=lambda item: item[0], reverse=True)
            return mp4_streams[0][1]

        # Fallback to raw video_info dict if streams attribute is absent.
        video_info = getattr(media_obj, "video_info", None)
        if not video_info and isinstance(media_obj, dict):
            video_info = media_obj.get("video_info")
        if isinstance(video_info, dict):
            variants = video_info.get("variants") or []
            mp4_variants = [
                (int(v.get("bitrate", 0) or 0), v["url"])
                for v in variants
                if v.get("url") and v.get("content_type") == "video/mp4"
            ]
            if mp4_variants:
                mp4_variants.sort(key=lambda item: item[0], reverse=True)
                return mp4_variants[0][1]
        return None

    async def download_media(self, url: str, suffix: str = ".bin") -> Path:
        """Stream-download media into temp_dir, enforcing max_video_bytes."""
        filename = f"{uuid.uuid4().hex}{suffix}"
        dest = self.temp_dir / filename
        total_bytes = 0

        try:
            async with self._http.stream("GET", url) as resp:
                resp.raise_for_status()
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > self.max_video_bytes:
                    raise MediaTooLargeError(
                        f"Media size {content_length} bytes exceeds limit {self.max_video_bytes}"
                    )

                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        total_bytes += len(chunk)
                        if total_bytes > self.max_video_bytes:
                            raise MediaTooLargeError(
                                f"Streamed media exceeded limit {self.max_video_bytes} bytes"
                            )
                        fh.write(chunk)
            return dest
        except Exception:
            if dest.exists():
                dest.unlink(missing_ok=True)
            raise