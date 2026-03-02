"""
Channel rate limiting — token bucket implementation.

Two independent buckets per channel message:
  1. Per-sender-per-channel bucket: tracks one (channel_idx, sender_id) pair.
     Exhaustion → warn sender once, then silent drop until refill.
  2. Per-channel bucket: tracks all senders on a channel_idx combined.
     Exhaustion → silent drop + log. No warning sent (would worsen flooding).

Token bucket behaviour:
  - Starts full (capacity tokens available).
  - Each command consumes one token.
  - Refills continuously at refill_rate tokens/second up to capacity.
  - Allows natural bursts (new user trying !help, !about, !ping) while
    throttling sustained floods.

Configuration (read live from config so !rehash picks up changes):
  channels.rate_limit.enabled            true
  channels.rate_limit.per_sender.capacity        5
  channels.rate_limit.per_sender.refill_rate     0.1
  channels.rate_limit.per_channel.capacity       15
  channels.rate_limit.per_channel.refill_rate    0.5
  channels.rate_limit.warn_on_limit      true

Rate limit state is intentionally in-memory only — does not survive
restarts. A restart clears all buckets, which is the desired behaviour.
"""

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import logging
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class TokenBucket:
    """
    Single token bucket.

    Attributes:
        capacity     — maximum tokens (= max burst size)
        refill_rate  — tokens added per second
        _tokens      — current token count (float for smooth refill)
        _last_refill — time.time() of last refill calculation
        _warned      — True if we've sent the rate-limit warning for the
                       current exhaustion period; reset when bucket refills
                       past the warn threshold (1 token).
    """

    def __init__(self, capacity: float, refill_rate: float):
        self.capacity    = capacity
        self.refill_rate = refill_rate
        self._tokens     = float(capacity)   # start full
        self._last_refill = time.monotonic()
        self._warned     = False

    def _refill(self):
        now     = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self.capacity,
            self._tokens + elapsed * self.refill_rate,
        )
        self._last_refill = now

        # Reset warned flag once the bucket has refilled enough for a new command
        if self._tokens >= 1.0:
            self._warned = False

    def consume(self) -> Tuple[bool, float]:
        """
        Attempt to consume one token.

        Returns:
            (allowed, tokens_remaining)
            allowed          — True if token was available and consumed
            tokens_remaining — float; negative means how far below zero we'd be
        """
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True, self._tokens
        return False, self._tokens

    def seconds_until_token(self) -> float:
        """
        Estimate seconds until at least one token is available.
        Returns 0 if a token is already available.
        """
        self._refill()
        if self._tokens >= 1.0:
            return 0.0
        deficit = 1.0 - self._tokens
        return deficit / self.refill_rate if self.refill_rate > 0 else float("inf")

    @property
    def warned(self) -> bool:
        return self._warned

    def mark_warned(self):
        self._warned = True


class ChannelRateLimiter:
    """
    Manages token buckets for channel rate limiting.

    Buckets are created lazily on first command from a sender/channel.
    Config values are read from the Config object each time a bucket is
    created, so !rehash affects new buckets but not existing ones
    (existing buckets drain/refill naturally).

    Usage in dispatcher.handle():

        result = self._rate_limiter.check(msg)
        if result == RateLimit.CHANNEL_LIMIT:
            return  # silent drop, already logged
        if result == RateLimit.SENDER_LIMIT:
            if warn_text:
                await self._enqueue_reply(msg, warn_text)
            return
        # proceed normally
    """

    def __init__(self, config):
        self._config = config
        # (channel_idx, sender_id) → TokenBucket
        self._sender_buckets: Dict[Tuple, TokenBucket] = {}
        # channel_idx → TokenBucket
        self._channel_buckets: Dict[int, TokenBucket] = {}

    def _sender_key(self, msg) -> Tuple:
        return (msg.raw.get("channel_idx") if msg.raw else None, msg.sender_id)

    def _channel_key(self, msg) -> Optional[int]:
        if msg.raw:
            v = msg.raw.get("channel_idx", msg.raw.get("channel"))
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        return None

    def _get_sender_bucket(self, key: Tuple) -> TokenBucket:
        if key not in self._sender_buckets:
            cap  = float(self._config.get("channels.rate_limit.per_sender.capacity",  5))
            rate = float(self._config.get("channels.rate_limit.per_sender.refill_rate", 0.1))
            self._sender_buckets[key] = TokenBucket(cap, rate)
        return self._sender_buckets[key]

    def _get_channel_bucket(self, idx: int) -> TokenBucket:
        if idx not in self._channel_buckets:
            cap  = float(self._config.get("channels.rate_limit.per_channel.capacity",  15))
            rate = float(self._config.get("channels.rate_limit.per_channel.refill_rate", 0.5))
            self._channel_buckets[idx] = TokenBucket(cap, rate)
        return self._channel_buckets[idx]

    def check(self, msg) -> "RateLimitResult":
        """
        Check rate limits for an inbound channel command.

        Returns a RateLimitResult indicating what (if anything) should happen.
        Only meaningful for channel messages — always returns ALLOWED for DMs.

        Check order: channel bucket first (shared resource), sender second.
        """
        if not self._config.get("channels.rate_limit.enabled", True):
            return RateLimitResult.ALLOWED

        if msg.is_dm:
            return RateLimitResult.ALLOWED

        ch_idx = self._channel_key(msg)
        if ch_idx is None:
            return RateLimitResult.ALLOWED

        # ── 1. Per-channel bucket (shared) ────────────────────────────────────
        ch_bucket = self._get_channel_bucket(ch_idx)
        ch_allowed, _ = ch_bucket.consume()
        if not ch_allowed:
            secs = ch_bucket.seconds_until_token()
            logger.warning(
                f"RATE LIMIT (channel): channel_idx={ch_idx} "
                f"sender={msg.sender_id} ({msg.sender_name!r}) "
                f"retry_in={secs:.0f}s — silent drop"
            )
            return RateLimitResult.CHANNEL_LIMIT

        # ── 2. Per-sender bucket ──────────────────────────────────────────────
        s_key    = self._sender_key(msg)
        s_bucket = self._get_sender_bucket(s_key)
        s_allowed, _ = s_bucket.consume()
        if not s_allowed:
            secs       = s_bucket.seconds_until_token()
            warn_once  = self._config.get("channels.rate_limit.warn_on_limit", True)
            already_warned = s_bucket.warned

            logger.warning(
                f"RATE LIMIT (sender): channel_idx={ch_idx} "
                f"sender={msg.sender_id} ({msg.sender_name!r}) "
                f"retry_in={secs:.0f}s "
                f"warn={'yes' if warn_once and not already_warned else 'no (already warned)'}"
            )

            if warn_once and not already_warned:
                s_bucket.mark_warned()
                return RateLimitResult.sender_warn(secs)

            return RateLimitResult.SENDER_LIMIT_SILENT

        return RateLimitResult.ALLOWED


class RateLimitResult:
    """
    Result object from ChannelRateLimiter.check().

    Use the class-level sentinels for comparisons:
        result == RateLimitResult.ALLOWED
        result == RateLimitResult.CHANNEL_LIMIT
        result == RateLimitResult.SENDER_LIMIT_SILENT
        result.is_sender_warn  → True if a warning should be sent
        result.retry_seconds   → estimated seconds until next token
    """

    ALLOWED             = None   # replaced below after class definition
    CHANNEL_LIMIT       = None
    SENDER_LIMIT_SILENT = None

    def __init__(self, kind: str, retry_seconds: float = 0.0):
        self._kind          = kind
        self.retry_seconds  = retry_seconds

    @classmethod
    def sender_warn(cls, retry_seconds: float) -> "RateLimitResult":
        return cls("sender_warn", retry_seconds)

    @property
    def is_allowed(self) -> bool:
        return self._kind == "allowed"

    @property
    def is_channel_limit(self) -> bool:
        return self._kind == "channel_limit"

    @property
    def is_sender_warn(self) -> bool:
        return self._kind == "sender_warn"

    @property
    def is_sender_silent(self) -> bool:
        return self._kind == "sender_silent"

    def __eq__(self, other):
        if other is RateLimitResult.ALLOWED:
            return self.is_allowed
        if other is RateLimitResult.CHANNEL_LIMIT:
            return self.is_channel_limit
        if other is RateLimitResult.SENDER_LIMIT_SILENT:
            return self.is_sender_silent
        return NotImplemented

    def __repr__(self):
        return f"RateLimitResult({self._kind!r}, retry={self.retry_seconds:.1f}s)"


# Initialise sentinels after class definition
RateLimitResult.ALLOWED             = RateLimitResult("allowed")
RateLimitResult.CHANNEL_LIMIT       = RateLimitResult("channel_limit")
RateLimitResult.SENDER_LIMIT_SILENT = RateLimitResult("sender_silent")
