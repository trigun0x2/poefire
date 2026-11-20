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

Buckets should be one per endpoint: endpoints can share a policy (the website's inventory and
stash reads do), and a bucket that mixes endpoints with different policies would only ever
know the policy of its last response.

Every window keeps a share of its hits free (`share`, at least `margin`): the account's and the
IP's limits are shared with the website itself and other tools, and GGG restricts apps that
make many invalid requests (429s count). The state can be saved and restored (`to_json`,
`restore`), so a restart doesn't forget a penalty in force or the hits made just before.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)
LOG_WAITS_FROM = 5.0  # seconds: longer waits for a rate limit are logged
SHORT_WINDOW = 120.0  # seconds: windows up to this long only space requests out (see fits)


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
        share: float = 0.1,
        wall: Callable[[], float] = time.time,
    ) -> None:
        """Every window keeps `share` of its hits free, and at least `margin` hits. `wall` is
        the clock saved states use (monotonic time doesn't survive a restart)."""
        self.clock = clock
        self.sleep = sleep
        self.margin = margin
        self.share = share
        self.wall = wall
        self._bucket_policy: dict[str, str] = {}
        self._policies: dict[str, _PolicyState] = {}
        self._blocked: dict[str, float] = {}  # bucket -> until (before its policy is known)
        self._lock = threading.Lock()

    def allowed(self, max_hits: int) -> int:
        """How many of a window's `max_hits` we use: the rest is headroom."""
        return max(1, max_hits - max(self.margin, math.ceil(max_hits * self.share)))

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
        self._expire(st, now)
        for max_hits, period in st.windows:
            allowed = self.allowed(max_hits)
            recent = [t for t in st.history if t > now - period]
            if len(recent) >= allowed:
                # wait until enough of the oldest hits leave the window
                wait = max(wait, recent[len(recent) - allowed] + period - now)
        return max(0.0, wait)

    @staticmethod
    def _expire(st: _PolicyState, now: float) -> None:
        longest = max((p for _, p in st.windows), default=0)
        while st.history and st.history[0] <= now - longest:
            st.history.popleft()

    def fits(self, buckets: Sequence[str], n: int, reserve: int = 0,
             short: float = SHORT_WINDOW) -> bool:  # fmt: skip
        """Whether `n` more requests, each on any of `buckets`, fit in the long windows now,
        with `reserve` hits to spare in each; and no penalty is in force. Windows of up to
        `short` seconds don't count: they only space the requests out (short waits). Each
        policy behind the buckets is counted for all `n` (exact when they share one,
        cautious when they don't); a bucket whose policy isn't known yet doesn't count."""
        with self._lock:
            now = self.clock()
            policies = set()
            for b in buckets:
                if self._blocked.get(b, 0.0) > now:
                    return False
                if (policy := self._bucket_policy.get(b)) is not None:
                    policies.add(policy)
            for policy in policies:
                st = self._policies[policy]
                if st.blocked_until > now:
                    return False
                self._expire(st, now)
                for max_hits, period in st.windows:
                    if period <= short:
                        continue
                    recent = sum(1 for t in st.history if t > now - period)
                    if recent + n + reserve > self.allowed(max_hits):
                        return False
            return True

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
            if wait >= LOG_WAITS_FROM:
                log.info("waiting %.0fs for the website's rate limit (%s requests)", wait, bucket)
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
                    inner = 0  # the period of the rule's next shorter window
                    for state in sorted(rule.states, key=lambda w: w.period):
                        if state.penalty > 0:
                            st.blocked_until = max(st.blocked_until, now + state.penalty)
                        # Server counts more hits than we sent (first request, other tools):
                        # pad our history so the sliding window accounts for them. Hits the
                        # shorter window didn't report are older than it: they go just
                        # outside it (as recent as they can be), not into it.
                        known = sum(1 for t in st.history if t > now - state.period)
                        if state.hits > known:
                            st.history.extend([now - inner] * (state.hits - known))
                            st.history = deque(sorted(st.history))
                        inner = state.period
            if status == 429 or info.retry_after is not None:
                until = now + (info.retry_after if info.retry_after is not None else 60.0)
                if st is not None:
                    st.blocked_until = max(st.blocked_until, until)
                else:
                    self._blocked[bucket] = max(self._blocked.get(bucket, 0.0), until)
        return info

    # --- carried over a restart ---

    def to_json(self) -> dict[str, Any]:
        """What the limiter knows, with wall-clock times (see `restore`)."""
        with self._lock:
            now = self.clock()
            offset = self.wall() - now
            policies: dict[str, Any] = {}
            for name, st in self._policies.items():
                self._expire(st, now)
                policies[name] = {
                    "windows": [list(w) for w in st.windows],
                    "history": [t + offset for t in st.history],
                    "blocked_until": st.blocked_until + offset if st.blocked_until > now else None,
                }
            return {
                "buckets": dict(self._bucket_policy),
                "policies": policies,
                "blocked": {b: u + offset for b, u in self._blocked.items() if u > now},
            }

    def restore(self, data: Any) -> None:
        """Take over a state saved by `to_json`: hits still inside their windows and penalties
        still in force. Anything malformed is ignored."""
        if not isinstance(data, dict):
            return
        with self._lock:
            now = self.clock()
            offset = now - self.wall()  # wall clock -> ours
            try:
                for bucket, policy in dict(data.get("buckets") or {}).items():
                    self._bucket_policy.setdefault(str(bucket), str(policy))
                for name, saved in dict(data.get("policies") or {}).items():
                    st = self._policies.setdefault(str(name), _PolicyState())
                    if not st.windows:
                        st.windows = [(int(h), int(p)) for h, p in saved.get("windows") or ()]
                    longest = max((p for _, p in st.windows), default=0)
                    # a hit "in the future" (the wall clock went back) counts as just made
                    hits = [min(float(t) + offset, now) for t in saved.get("history") or ()]
                    st.history = deque(sorted([*st.history, *(t for t in hits
                                                              if t > now - longest)]))  # fmt: skip
                    if (until := saved.get("blocked_until")) is not None:
                        st.blocked_until = max(st.blocked_until, float(until) + offset)
                for bucket, until in dict(data.get("blocked") or {}).items():
                    self._blocked[str(bucket)] = max(self._blocked.get(str(bucket), 0.0),
                                                     float(until) + offset)  # fmt: skip
            except (TypeError, ValueError, AttributeError) as e:
                log.info("saved rate-limit state not used: %s", e)
            # a bucket must never point at a policy we know nothing about
            for bucket, policy in list(self._bucket_policy.items()):
                if policy not in self._policies:
                    del self._bucket_policy[bucket]
