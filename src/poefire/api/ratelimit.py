"""Client-side limiter driven by GGG's rate-limit headers.

Headers (https://www.pathofexile.com/developer/docs#ratelimits):
    X-Rate-Limit-Policy: <policy name>
    X-Rate-Limit-Rules: client,account          (comma-separated rule names)
    X-Rate-Limit-<Rule>: 10:5:10,30:300:300      (max hits : period s : penalty s, per window)
    X-Rate-Limit-<Rule>-State: 1:5:0,4:300:0     (current hits : period s : active restriction s)
    Retry-After: <seconds>                       (on 429)

Limits are dynamic, so nothing is hard-coded: each endpoint bucket learns its policy from the
first response. We keep our own request history per policy, and pad it whenever the server
reports more hits than we know of (other tools sharing the account limit).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Window:
    hits: int  # max hits (limit) or current hits (state)
    period: int
    penalty: int  # penalty on violation (limit) or active restriction (state)


@dataclass(frozen=True)
class RuleInfo:
    name: str
    limits: tuple[Window, ...]
    states: tuple[Window, ...]


@dataclass(frozen=True)
class RateLimitInfo:
    policy: str
    rules: tuple[RuleInfo, ...]
    retry_after: float | None


def _parse_windows(value: str | None) -> tuple[Window, ...]:
    if not value:
        return ()
    out = []
    for part in value.split(","):
        try:
            a, b, c = (int(x) for x in part.strip().split(":"))
        except ValueError:
            continue
        out.append(Window(a, b, c))
    return tuple(out)


def parse_headers(headers: Mapping[str, str]) -> RateLimitInfo | None:
    """`headers` must be case-insensitive (httpx.Headers is)."""
    policy = headers.get("x-rate-limit-policy")
    retry_after: float | None = None
    if ra := headers.get("retry-after"):
        try:
            retry_after = float(ra)
        except ValueError:
            retry_after = None
    if policy is None:
        return None if retry_after is None else RateLimitInfo("", (), retry_after)
    rules = []
    for name in (headers.get("x-rate-limit-rules") or "").split(","):
        name = name.strip()
        if not name:
            continue
        rules.append(
            RuleInfo(
                name,
                _parse_windows(headers.get(f"x-rate-limit-{name}")),
                _parse_windows(headers.get(f"x-rate-limit-{name}-state")),
            )
        )
    return RateLimitInfo(policy, tuple(rules), retry_after)


@dataclass
class _PolicyState:
    # (max_hits, period) for every window of every rule
    windows: list[tuple[int, int]] = field(default_factory=list)
    history: deque[float] = field(default_factory=deque)
    blocked_until: float = 0.0


class RateLimiter:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        margin: int = 1,
    ) -> None:
        """`margin` keeps that many hits of headroom below every limit."""
        self.clock = clock
        self.sleep = sleep
        self.margin = margin
        self._bucket_policy: dict[str, str] = {}
        self._policies: dict[str, _PolicyState] = {}
        self._blocked: dict[str, float] = {}  # bucket -> until (before its policy is known)
        self._lock = threading.Lock()

    def wait_time(self, bucket: str) -> float:
        with self._lock:
            return self._wait_time(bucket, self.clock())

    def _wait_time(self, bucket: str, now: float) -> float:
        wait = self._blocked.get(bucket, 0.0) - now
        policy = self._bucket_policy.get(bucket)
        if policy is None:
            return max(0.0, wait)
        st = self._policies[policy]
        wait = max(wait, st.blocked_until - now)
        longest = max((p for _, p in st.windows), default=0)
        while st.history and st.history[0] <= now - longest:
            st.history.popleft()
        for max_hits, period in st.windows:
            allowed = max(1, max_hits - self.margin)
            recent = [t for t in st.history if t > now - period]
            if len(recent) >= allowed:
                # wait until enough of the oldest hits leave the window
                wait = max(wait, recent[len(recent) - allowed] + period - now)
        return max(0.0, wait)

    def acquire(self, bucket: str) -> None:
        """Block until a request on `bucket` is allowed, then record it."""
        while True:
            with self._lock:
                now = self.clock()
                wait = self._wait_time(bucket, now)
                if wait <= 0:
                    policy = self._bucket_policy.get(bucket)
                    if policy is not None:
                        self._policies[policy].history.append(now)
                    return
            self.sleep(wait)

    def update(self, bucket: str, headers: Mapping[str, str], status: int) -> RateLimitInfo | None:
        info = parse_headers(headers)
        if info is None:
            if status != 429:
                return None
            info = RateLimitInfo("", (), None)  # 429 without headers: back off the default
        with self._lock:
            now = self.clock()
            st: _PolicyState | None = None
            if info.policy:
                self._bucket_policy[bucket] = info.policy
                st = self._policies.setdefault(info.policy, _PolicyState())
                st.windows = [(w.hits, w.period) for r in info.rules for w in r.limits]
                for rule in info.rules:
                    for state in rule.states:
                        if state.penalty > 0:
                            st.blocked_until = max(st.blocked_until, now + state.penalty)
                        # Server counts more hits than we sent (first request, other tools):
                        # pad our history so the sliding window accounts for them.
                        known = sum(1 for t in st.history if t > now - state.period)
                        st.history.extend([now] * (state.hits - known))
            if status == 429 or info.retry_after is not None:
                until = now + (info.retry_after if info.retry_after is not None else 60.0)
                if st is not None:
                    st.blocked_until = max(st.blocked_until, until)
                else:
                    self._blocked[bucket] = max(self._blocked.get(bucket, 0.0), until)
        return info
