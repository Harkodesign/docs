"""ALPHAONE's memory between runs.

Remembers which links it has already considered and which stories it has
already sent you, so each hourly email only carries what's new. In GitHub
Actions this file is carried from run to run with the Actions cache.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("alphaone.state")

SEEN_RETENTION = timedelta(days=14)
COVERED_RETENTION = timedelta(hours=72)


@dataclass
class State:
    seen: dict[str, str] = field(default_factory=dict)  # item key -> first-seen ISO time
    covered: list[dict] = field(default_factory=list)  # {"at", "headline", "urls"}
    last_run: str | None = None
    issue_number: int = 0

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            log.info("no saved memory at %s - starting fresh", path)
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                seen=dict(raw.get("seen", {})),
                covered=list(raw.get("covered", [])),
                last_run=raw.get("last_run"),
                issue_number=int(raw.get("issue_number", 0)),
            )
        except (OSError, ValueError, TypeError) as exc:
            log.warning("could not read memory at %s (%s) - starting fresh", path, exc)
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def seen_keys(self) -> set[str]:
        return set(self.seen)

    def recent_headlines(self) -> list[str]:
        return [f"{entry['headline']} (sent {entry.get('at', '')[:16].replace('T', ' ')} UTC)" for entry in self.covered]

    def record_run(self, now: datetime, considered_keys: list[str], covered: list[dict], sent: bool) -> None:
        stamp = now.isoformat()
        for key in considered_keys:
            self.seen.setdefault(key, stamp)
        if sent:  # an issue that wasn't sent reached nobody: it covers nothing and isn't a briefing
            self.covered += [{"at": stamp, **entry} for entry in covered]
            self.last_run = stamp
            self.issue_number += 1
        self.prune(now)

    def prune(self, now: datetime) -> None:
        def older_than(stamp: str, age: timedelta) -> bool:
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                return True
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return now - when > age

        self.seen = {k: v for k, v in self.seen.items() if not older_than(v, SEEN_RETENTION)}
        self.covered = [c for c in self.covered if not older_than(c.get("at", ""), COVERED_RETENTION)]
