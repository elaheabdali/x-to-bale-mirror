"""Message formatting utilities for Bale Messenger (Markdown + RTL/LTR safe)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Bale Bot API caption limit matches Telegram's 1024-char limit.
BALE_MAX_CAPTION_LENGTH = 1024
BALE_MAX_MESSAGE_LENGTH = 4096

# Unicode directional isolate characters to keep LTR handles/URLs clean inside RTL text.
LRI = "\u2066"
PDI = "\u2069"

_MD_ESCAPE_RE = re.compile(r"([*_`\[\]])")


@dataclass(slots=True)
class NormalizedMedia:
    """Normalized media attachment extracted from an X post."""

    media_type: str  # "photo" | "video" | "animated_gif"
    url: str
    width: int | None = None
    height: int | None = None


@dataclass(slots=True)
class NormalizedQuotedTweet:
    """Normalized quoted tweet context."""

    id: str
    author_name: str
    author_username: str
    text: str
    url: str


@dataclass(slots=True)
class NormalizedTweet:
    """Normalized representation of an X Home Timeline post."""

    id: str
    author_name: str
    author_username: str
    text: str
    url: str
    created_at: str | None = None
    is_retweet: bool = False
    retweeted_by_name: str | None = None
    retweeted_by_username: str | None = None
    quoted_tweet: NormalizedQuotedTweet | None = None
    media: list[NormalizedMedia] = field(default_factory=list)
    possibly_sensitive: bool = False


def escape_markdown(text: str) -> str:
    """Escape Bale Markdown special characters in untrusted text."""
    return _MD_ESCAPE_RE.sub(r"\\\1", text)


def _format_handle(username: str) -> str:
    return f"{LRI}@{escape_markdown(username)}{PDI}"


def format_tweet_header(tweet: NormalizedTweet) -> str:
    """Build the compact author/retweet header line(s) for a tweet."""
    lines: list[str] = []
    if tweet.is_retweet and tweet.retweeted_by_username:
        rt_name = escape_markdown(tweet.retweeted_by_name or tweet.retweeted_by_username)
        lines.append(f"🔁 *Retweeted by* {rt_name} ({_format_handle(tweet.retweeted_by_username)})")

    author_name = escape_markdown(tweet.author_name or tweet.author_username)
    lines.append(f"👤 *{author_name}* ({_format_handle(tweet.author_username)})")
    return "\n".join(lines)


def format_tweet_message(
    tweet: NormalizedTweet,
    extra_media_urls: list[str] | None = None,
) -> str:
    """Format a NormalizedTweet into a clean Bale Markdown message."""
    sections: list[str] = [format_tweet_header(tweet)]

    body = tweet.text.strip()
    if body:
        sections.append(escape_markdown(body))

    if tweet.quoted_tweet:
        qt = tweet.quoted_tweet
        qt_name = escape_markdown(qt.author_name or qt.author_username)
        qt_header = f"💬 *Quoting* {qt_name} ({_format_handle(qt.author_username)}):"
        qt_body_lines = [
            f"> {escape_markdown(line)}" if line.strip() else ">"
            for line in qt.text.strip().splitlines()
        ]
        qt_block = "\n".join([qt_header, *qt_body_lines])
        sections.append(qt_block)

    footer_parts = [f"🔗 [View on X]({tweet.url})"]
    if extra_media_urls:
        for idx, media_url in enumerate(extra_media_urls, start=1):
            label = "Direct Media" if len(extra_media_urls) == 1 else f"Direct Media {idx}"
            footer_parts.append(f"📎 [{label}]({media_url})")

    sections.append(" | ".join(footer_parts))
    return "\n\n".join(sections)


def split_caption_and_followup(
    tweet: NormalizedTweet,
    max_caption_len: int = BALE_MAX_CAPTION_LENGTH,
) -> tuple[str, str | None]:
    """Return (caption, followup_text) respecting Bale's caption length limit.

    If the full formatted post fits within `max_caption_len`, returns `(full_text, None)`.
    Otherwise returns `(short_header_with_link, full_text)` so media can be sent first
    followed immediately by `sendMessage` with the complete text.
    """
    full_text = format_tweet_message(tweet)
    if len(full_text) <= max_caption_len:
        return full_text, None

    short_caption = f"{format_tweet_header(tweet)}\n\n🔗 [View on X]({tweet.url})"
    if len(short_caption) > max_caption_len:
        short_caption = short_caption[: max_caption_len - 3] + "..."
    return short_caption, full_text


def chunk_long_message(text: str, max_len: int = BALE_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split an unusually long message into chunks within Bale's message limit."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
