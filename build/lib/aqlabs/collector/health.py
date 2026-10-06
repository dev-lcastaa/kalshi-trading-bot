"""Feed liveness: every feed must keep producing events, and silence is an alert, not a surprise."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Feed:
    max_silent_ms: int
    active: bool = True  # inactive feeds (e.g. quotes while no market is open) are never alerted on
    events: int = 0
    events_at_last_heartbeat: int = 0
    last_ms: int | None = None
    down: bool = False


@dataclass
class FeedHealth:
    started_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    feeds: dict[str, _Feed] = field(default_factory=dict)

    def register(self, name: str, max_silent_ms: int, active: bool = True) -> None:
        self.feeds[name] = _Feed(max_silent_ms=max_silent_ms, active=active)

    def set_active(self, name: str, active: bool) -> None:
        if name in self.feeds:
            self.feeds[name].active = active

    def beat(self, name: str, now_ms: int | None = None, n: int = 1) -> None:
        feed = self.feeds.get(name)
        if feed is None:
            return
        feed.events += n
        feed.last_ms = now_ms if now_ms is not None else int(time.time() * 1000)

    def silent_ms(self, name: str, now_ms: int) -> int:
        """Milliseconds since the last event; a feed that never ticked counts from collector start."""
        feed = self.feeds[name]
        return now_ms - (feed.last_ms if feed.last_ms is not None else self.started_ms)

    def evaluate(self, now_ms: int) -> list[dict]:
        """State transitions only: one 'down' alert per outage and one 'recovered' when it ticks again."""
        alerts = []
        for name, feed in self.feeds.items():
            silent = self.silent_ms(name, now_ms)
            is_down = feed.active and silent > feed.max_silent_ms
            if is_down and not feed.down:
                feed.down = True
                alerts.append({"feed": name, "state": "down", "silent_ms": silent,
                               "message": f"feed {name} silent for {silent / 1000:.0f}s "
                                          f"(limit {feed.max_silent_ms / 1000:.0f}s)"})
            elif feed.down and not is_down:
                feed.down = False
                alerts.append({"feed": name, "state": "recovered", "silent_ms": silent,
                               "message": f"feed {name} recovered"})
        return alerts

    def heartbeat_rows(self, now_ms: int) -> list[dict]:
        """One row per feed: events since the previous heartbeat and current silence."""
        rows = []
        for name, feed in self.feeds.items():
            rows.append({"feed": name, "ts_ms": now_ms, "events": feed.events - feed.events_at_last_heartbeat,
                         "silent_ms": self.silent_ms(name, now_ms),
                         "note": "down" if feed.down else ("inactive" if not feed.active else None)})
            feed.events_at_last_heartbeat = feed.events
        return rows

    def status(self, now_ms: int) -> dict:
        return {name: {"events": f.events, "last_ms": f.last_ms, "silent_ms": self.silent_ms(name, now_ms),
                       "active": f.active, "down": f.down} for name, f in self.feeds.items()}
