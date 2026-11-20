"""Async Bale Messenger Bot API client using httpx."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from src.formatter import (
    NormalizedTweet,
    chunk_long_message,
    format_tweet_message,
    split_caption_and_followup,
)
from src.x_client import MediaTooLargeError, XTimelineClient

logger = logging.getLogger(__name__)


class BaleAPIError(Exception):
    """Raised when the Bale Bot API returns ok=false or an HTTP error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BaleClient:
    """Dedicated async wrapper for https://tapi.bale.ai/bot<token>/<method>."""

    def __init__(
        self,
        bot_token: str,
        base_url: str = "https://tapi.bale.ai",
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self.base_url = f"{base_url.rstrip('/')}/bot{bot_token}"
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=15.0),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{method}"
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                if files is not None:
                    resp = await self._http.post(url, data=data, files=files)
                else:
                    resp = await self._http.post(url, json=data or {})

                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = float(resp.headers.get("Retry-After", 2**attempt))
                    logger.warning(
                        "Bale API %s returned HTTP %s (attempt %d/%d); retrying in %.1fs",
                        method,
                        resp.status_code,
                        attempt,
                        self.max_retries,
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                payload = resp.json()
                if resp.status_code >= 400 or not payload.get("ok", False):
                    desc = payload.get("description") or resp.text
                    raise BaleAPIError(
                        f"Bale API {method} failed ({resp.status_code}): {desc}",
                        status_code=resp.status_code,
                    )
                return payload.get("result", {})

            except (httpx.RequestError, ValueError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = 2**attempt
                    logger.warning(
                        "Bale API %s network error (%s); retrying in %ds",
                        method,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise BaleAPIError(f"Bale API {method} failed: {exc}") from exc

        raise BaleAPIError(f"Bale API {method} failed after retries: {last_exc}")

    async def send_message(self, chat_id: str, text: str) -> None:
        for chunk in chunk_long_message(text):
            await self._request("sendMessage", data={"chat_id": chat_id, "text": chunk})

    async def send_photo(
        self,
        chat_id: str,
        photo_path: Path,
        caption: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        with photo_path.open("rb") as fh:
            await self._request(
                "sendPhoto",
                data=data,
                files={"photo": (photo_path.name, fh, "image/jpeg")},
            )

    async def send_media_group(
        self,
        chat_id: str,
        photo_paths: list[Path],
        caption: str | None = None,
    ) -> None:
        """Send 2+ photos as an album, falling back to sequential sendPhoto if rejected."""
        media_items = []
        opened_handles = []
        files: dict[str, Any] = {}

        try:
            for idx, path in enumerate(photo_paths):
                field_name = f"photo_{idx}"
                item: dict[str, Any] = {"type": "photo", "media": f"attach://{field_name}"}
                if idx == 0 and caption:
                    item["caption"] = caption
                media_items.append(item)

                fh = path.open("rb")
                opened_handles.append(fh)
                files[field_name] = (path.name, fh, "image/jpeg")

            await self._request(
                "sendMediaGroup",
                data={"chat_id": chat_id, "media": json.dumps(media_items)},
                files=files,
            )
        except BaleAPIError as exc:
            logger.warning(
                "Bale sendMediaGroup failed (%s); falling back to sequential sendPhoto",
                exc,
            )
            for idx, path in enumerate(photo_paths):
                await self.send_photo(
                    chat_id,
                    path,
                    caption=caption if idx == 0 else None,
                )
        finally:
            for fh in opened_handles:
                fh.close()

    async def send_video(
        self,
        chat_id: str,
        video_path: Path,
        caption: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        with video_path.open("rb") as fh:
            await self._request(
                "sendVideo",
                data=data,
                files={"video": (video_path.name, fh, "video/mp4")},
            )

    async def send_animation(
        self,
        chat_id: str,
        animation_path: Path,
        caption: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        with animation_path.open("rb") as fh:
            try:
                await self._request(
                    "sendAnimation",
                    data=data,
                    files={"animation": (animation_path.name, fh, "video/mp4")},
                )
            except BaleAPIError as exc:
                logger.warning(
                    "Bale sendAnimation failed (%s); falling back to sendVideo",
                    exc,
                )
                fh.seek(0)
                await self._request(
                    "sendVideo",
                    data=data,
                    files={"video": (animation_path.name, fh, "video/mp4")},
                )

    async def deliver_tweet(
        self,
        chat_id: str,
        tweet: NormalizedTweet,
        x_client: XTimelineClient,
    ) -> None:
        """Deliver a NormalizedTweet (text, photos, videos, GIFs) to Bale with safe fallbacks."""
        if not tweet.media:
            await self.send_message(chat_id, format_tweet_message(tweet))
            return

        caption, followup_text = split_caption_and_followup(tweet)
        photos = [m for m in tweet.media if m.media_type == "photo"]
        videos = [m for m in tweet.media if m.media_type in ("video", "animated_gif")]

        # 1. Deliver photos (single or album)
        if photos:
            downloaded_photos: list[Path] = []
            try:
                for p in photos:
                    suffix = Path(p.url.split("?")[0]).suffix or ".jpg"
                    downloaded_photos.append(await x_client.download_media(p.url, suffix=suffix))

                if len(downloaded_photos) == 1:
                    await self.send_photo(chat_id, downloaded_photos[0], caption=caption)
                else:
                    await self.send_media_group(chat_id, downloaded_photos, caption=caption)
            except Exception as exc:
                logger.warning(
                    "Photo upload failed for tweet %s (%s); falling back to text+links",
                    tweet.id,
                    exc,
                )
                fallback = format_tweet_message(
                    tweet,
                    extra_media_urls=[p.url for p in photos],
                )
                await self.send_message(chat_id, fallback)
                followup_text = None
            finally:
                for path in downloaded_photos:
                    path.unlink(missing_ok=True)

        # 2. Deliver videos / animated GIFs
        for idx, v in enumerate(videos):
            use_caption = caption if (not photos and idx == 0) else None
            temp_video: Path | None = None
            try:
                temp_video = await x_client.download_media(v.url, suffix=".mp4")
                if v.media_type == "animated_gif":
                    await self.send_animation(chat_id, temp_video, caption=use_caption)
                else:
                    await self.send_video(chat_id, temp_video, caption=use_caption)
            except (MediaTooLargeError, BaleAPIError, httpx.RequestError) as exc:
                logger.warning(
                    "Video/GIF upload failed for tweet %s (%s); falling back to text+link",
                    tweet.id,
                    exc,
                )
                fallback = format_tweet_message(tweet, extra_media_urls=[v.url])
                await self.send_message(chat_id, fallback)
                followup_text = None
            finally:
                if temp_video is not None:
                    temp_video.unlink(missing_ok=True)

        # 3. Send full text follow-up if caption was truncated
        if followup_text:
            await self.send_message(chat_id, followup_text)
