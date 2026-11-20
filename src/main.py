"""Main entrypoint: FastAPI health server + 24/7 X-to-Bale polling worker."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from src.bale_client import BaleClient
from src.storage import StateStore
from src.x_client import XAuthError, XRateLimitError, XTimelineClient

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("x_bale_mirror")


@dataclass(slots=True)
class Settings:
    bale_bot_token: str
    bale_chat_id: str
    x_auth_token: str
    x_ct0: str
    x_target_username: str = "dontcallmeela"
    bale_admin_chat_id: str | None = None
    database_url: str | None = None
    sqlite_path: str = "data/state.db"
    poll_interval_seconds: int = 180
    poll_jitter_seconds: int = 15
    cold_start_seed_count: int = 8
    timeline_fetch_count: int = 25
    max_video_size_mb: int = 45
    temp_media_dir: str = "/tmp/x_bale_media"
    port: int = 8000

    @classmethod
    def from_env(cls) -> Settings:
        def _req(name: str) -> str:
            val = os.getenv(name, "").strip()
            if not val:
                raise RuntimeError(f"Missing required environment variable: {name}")
            return val

        return cls(
            bale_bot_token=_req("BALE_BOT_TOKEN"),
            bale_chat_id=_req("BALE_CHAT_ID"),
            x_auth_token=_req("X_AUTH_TOKEN"),
            x_ct0=_req("X_CT0"),
            x_target_username=os.getenv("X_TARGET_USERNAME", "dontcallmeela").strip(),
            bale_admin_chat_id=os.getenv("BALE_ADMIN_CHAT_ID", "").strip() or None,
            database_url=os.getenv("DATABASE_URL", "").strip() or None,
            sqlite_path=os.getenv("SQLITE_PATH", "data/state.db").strip(),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "180")),
            poll_jitter_seconds=int(os.getenv("POLL_JITTER_SECONDS", "15")),
            cold_start_seed_count=int(os.getenv("COLD_START_SEED_COUNT", "8")),
            timeline_fetch_count=int(os.getenv("TIMELINE_FETCH_COUNT", "25")),
            max_video_size_mb=int(os.getenv("MAX_VIDEO_SIZE_MB", "45")),
            temp_media_dir=os.getenv("TEMP_MEDIA_DIR", "/tmp/x_bale_media").strip(),
            port=int(os.getenv("PORT", "8000")),
        )


@dataclass(slots=True)
class RuntimeMetrics:
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_sync_at: str | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    auth_alert_sent: bool = False


class MirrorWorker:
    """Coordinates polling X Home Timeline, deduplicating, and forwarding to Bale."""

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        x_client: XTimelineClient,
        bale_client: BaleClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.x_client = x_client
        self.bale_client = bale_client
        self.metrics = RuntimeMetrics()
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def sync_once(self) -> int:
        """Run one poll-and-forward cycle. Returns number of newly forwarded tweets."""
        is_cold_start = await self.store.is_empty()
        fetch_count = (
            self.settings.cold_start_seed_count
            if is_cold_start
            else self.settings.timeline_fetch_count
        )

        tweets = await self.x_client.fetch_home_timeline(count=fetch_count)
        if not tweets:
            self.metrics.last_sync_at = datetime.now(timezone.utc).isoformat()
            self.metrics.consecutive_errors = 0
            self.metrics.last_error = None
            return 0

        if is_cold_start:
            seed_tweets = tweets[-self.settings.cold_start_seed_count :]
            await self.store.mark_many_processed([t.id for t in seed_tweets])
            logger.info(
                "Cold start complete: seeded %d latest tweet IDs without flooding Bale",
                len(seed_tweets),
            )
            self.metrics.last_sync_at = datetime.now(timezone.utc).isoformat()
            self.metrics.consecutive_errors = 0
            self.metrics.last_error = None
            return 0

        already_seen = await self.store.get_processed_ids([t.id for t in tweets])
        unseen = [t for t in tweets if t.id not in already_seen]

        forwarded = 0
        for tweet in unseen:
            await self.bale_client.deliver_tweet(
                chat_id=self.settings.bale_chat_id,
                tweet=tweet,
                x_client=self.x_client,
            )
            await self.store.mark_processed(tweet.id)
            forwarded += 1
            logger.info("Forwarded tweet %s (@%s)", tweet.id, tweet.author_username)
            await asyncio.sleep(0.5)

        self.metrics.last_sync_at = datetime.now(timezone.utc).isoformat()
        self.metrics.consecutive_errors = 0
        self.metrics.last_error = None
        self.metrics.auth_alert_sent = False
        return forwarded

    async def run_forever(self) -> None:
        """24/7 polling loop with randomized jitter and exponential backoff."""
        logger.info(
            "Starting mirror worker for @%s (interval=%ds ±%ds, store=%s)",
            self.settings.x_target_username,
            self.settings.poll_interval_seconds,
            self.settings.poll_jitter_seconds,
            self.store.backend_name,
        )

        while not self._stop_event.is_set():
            sleep_seconds = float(self.settings.poll_interval_seconds) + random.uniform(
                -self.settings.poll_jitter_seconds,
                self.settings.poll_jitter_seconds,
            )
            sleep_seconds = max(30.0, sleep_seconds)

            try:
                forwarded = await self.sync_once()
                logger.info("Sync cycle finished; forwarded=%d", forwarded)
            except XAuthError as exc:
                self.metrics.consecutive_errors += 1
                self.metrics.last_error = str(exc)
                logger.error("X authentication error: %s", exc)
                if not self.metrics.auth_alert_sent:
                    await self._send_admin_alert(
                        "⚠️ *X Mirror Bot Alert*\n\n"
                        "X cookies (`X_AUTH_TOKEN` / `X_CT0`) appear expired or invalid "
                        "(HTTP 401/403). Please refresh cookies in your deployment environment."
                    )
                    self.metrics.auth_alert_sent = True
                sleep_seconds = min(1800.0, 60.0 * (2 ** min(self.metrics.consecutive_errors, 5)))
            except XRateLimitError as exc:
                self.metrics.consecutive_errors += 1
                self.metrics.last_error = str(exc)
                backoff = min(1800.0, 60.0 * (2 ** min(self.metrics.consecutive_errors, 5)))
                logger.warning("X rate limit hit (%s); backing off %.0fs", exc, backoff)
                sleep_seconds = backoff
            except Exception as exc:
                self.metrics.consecutive_errors += 1
                self.metrics.last_error = str(exc)
                backoff = min(900.0, 30.0 * (2 ** min(self.metrics.consecutive_errors, 5)))
                logger.exception("Unexpected error during sync; backing off %.0fs", backoff)
                sleep_seconds = backoff

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_seconds)
            except asyncio.TimeoutError:
                continue

    async def _send_admin_alert(self, message: str) -> None:
        target = self.settings.bale_admin_chat_id or self.settings.bale_chat_id
        try:
            await self.bale_client.send_message(target, message)
        except Exception:
            logger.exception("Failed to send admin alert to Bale chat %s", target)


def create_app() -> FastAPI:
    """Create the FastAPI application with background worker lifespan."""
    worker_holder: dict[str, MirrorWorker] = {}
    store_holder: dict[str, StateStore] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings = Settings.from_env()
        store = StateStore(
            database_url=settings.database_url,
            sqlite_path=settings.sqlite_path,
        )
        await store.init()
        x_client = XTimelineClient(
            auth_token=settings.x_auth_token,
            ct0=settings.x_ct0,
            target_username=settings.x_target_username,
            temp_dir=settings.temp_media_dir,
            max_video_size_mb=settings.max_video_size_mb,
        )
        bale_client = BaleClient(bot_token=settings.bale_bot_token)
        worker = MirrorWorker(settings, store, x_client, bale_client)

        store_holder["store"] = store
        worker_holder["worker"] = worker
        task = asyncio.create_task(worker.run_forever())

        try:
            yield
        finally:
            worker.request_stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await x_client.close()
            await bale_client.close()
            await store.close()

    app = FastAPI(title="X-to-Bale Mirror Bot", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, object]:
        store = store_holder.get("store")
        worker = worker_holder.get("worker")
        processed_count = await store.get_processed_count() if store else 0
        metrics = worker.metrics if worker else RuntimeMetrics()
        return {
            "status": "ok",
            "target_account": worker.settings.x_target_username if worker else "dontcallmeela",
            "storage_backend": store.backend_name if store else "uninitialized",
            "processed_count": processed_count,
            "started_at": metrics.started_at,
            "last_sync_at": metrics.last_sync_at,
            "consecutive_errors": metrics.consecutive_errors,
            "last_error": metrics.last_error,
        }

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("src.main:app", host="0.0.0.0", port=port, log_level="info")
