"""Dual-backend deduplication store (PostgreSQL primary, SQLite fallback)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import aiosqlite
import asyncpg

logger = logging.getLogger(__name__)


class StateStore:
    """Persists processed tweet IDs in PostgreSQL or local SQLite."""

    def __init__(
        self,
        database_url: str | None = None,
        sqlite_path: str = "data/state.db",
    ) -> None:
        self.database_url = self._normalize_pg_url(database_url) if database_url else None
        self.sqlite_path = Path(sqlite_path)
        self._pg_pool: asyncpg.Pool | None = None
        self._sqlite_conn: aiosqlite.Connection | None = None

    @staticmethod
    def _normalize_pg_url(url: str) -> str:
        if url.startswith("postgres://"):
            return "postgresql://" + url[len("postgres://") :]
        return url

    @property
    def backend_name(self) -> str:
        return "postgresql" if self.database_url else "sqlite"

    async def init(self) -> None:
        """Initialize connection pool / database and ensure schema exists."""
        if self.database_url:
            logger.info("Initializing PostgreSQL state store")
            self._pg_pool = await asyncpg.create_pool(
                dsn=self.database_url,
                min_size=1,
                max_size=5,
            )
            async with self._pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS processed_tweets (
                        tweet_id TEXT PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
        else:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Initializing SQLite state store at %s", self.sqlite_path)
            self._sqlite_conn = await aiosqlite.connect(self.sqlite_path)
            await self._sqlite_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_tweets (
                    tweet_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            await self._sqlite_conn.commit()

    async def close(self) -> None:
        """Close active database connections cleanly."""
        if self._pg_pool is not None:
            await self._pg_pool.close()
            self._pg_pool = None
        if self._sqlite_conn is not None:
            await self._sqlite_conn.close()
            self._sqlite_conn = None

    async def get_processed_count(self) -> int:
        """Return total number of recorded tweet IDs."""
        if self._pg_pool is not None:
            async with self._pg_pool.acquire() as conn:
                val = await conn.fetchval("SELECT COUNT(*) FROM processed_tweets;")
                return int(val or 0)
        if self._sqlite_conn is not None:
            async with self._sqlite_conn.execute(
                "SELECT COUNT(*) FROM processed_tweets;"
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else 0
        raise RuntimeError("StateStore is not initialized")

    async def is_empty(self) -> bool:
        """Return True on cold start when no tweets have been recorded yet."""
        return (await self.get_processed_count()) == 0

    async def is_processed(self, tweet_id: str) -> bool:
        """Check whether a single tweet ID has already been processed."""
        if self._pg_pool is not None:
            async with self._pg_pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT 1 FROM processed_tweets WHERE tweet_id = $1 LIMIT 1;",
                    tweet_id,
                )
                return row is not None
        if self._sqlite_conn is not None:
            async with self._sqlite_conn.execute(
                "SELECT 1 FROM processed_tweets WHERE tweet_id = ? LIMIT 1;",
                (tweet_id,),
            ) as cursor:
                return (await cursor.fetchone()) is not None
        raise RuntimeError("StateStore is not initialized")

    async def get_processed_ids(self, tweet_ids: Iterable[str]) -> set[str]:
        """Return the subset of tweet_ids that already exist in the store."""
        ids = list(dict.fromkeys(str(tid) for tid in tweet_ids))
        if not ids:
            return set()

        if self._pg_pool is not None:
            async with self._pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT tweet_id FROM processed_tweets WHERE tweet_id = ANY($1::text[]);",
                    ids,
                )
                return {row["tweet_id"] for row in rows}

        if self._sqlite_conn is not None:
            placeholders = ",".join("?" for _ in ids)
            query = f"SELECT tweet_id FROM processed_tweets WHERE tweet_id IN ({placeholders});"
            async with self._sqlite_conn.execute(query, ids) as cursor:
                rows = await cursor.fetchall()
                return {row[0] for row in rows}

        raise RuntimeError("StateStore is not initialized")

    async def mark_processed(self, tweet_id: str) -> None:
        """Record a single tweet ID as processed."""
        await self.mark_many_processed([tweet_id])

    async def mark_many_processed(self, tweet_ids: Iterable[str]) -> None:
        """Record multiple tweet IDs as processed (idempotent)."""
        ids = [(str(tid),) for tid in dict.fromkeys(tweet_ids)]
        if not ids:
            return

        if self._pg_pool is not None:
            async with self._pg_pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO processed_tweets (tweet_id)
                    VALUES ($1)
                    ON CONFLICT (tweet_id) DO NOTHING;
                    """,
                    ids,
                )
            return

        if self._sqlite_conn is not None:
            await self._sqlite_conn.executemany(
                "INSERT OR IGNORE INTO processed_tweets (tweet_id) VALUES (?);",
                ids,
            )
            await self._sqlite_conn.commit()
            return

        raise RuntimeError("StateStore is not initialized")
