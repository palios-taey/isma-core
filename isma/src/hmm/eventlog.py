"""
HMM Temporal Event Log - append-only JSONL truth.

Every mutation of memory is a new event. Events are immutable once written.
The event log is the source of truth; all derived structures (Neo4j indexes,
Redis fields) can be rebuilt from replay.
"""

import json
import os
import time
import uuid
import fcntl
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, Iterator, List
from pathlib import Path
from isma.config import ISMA_STATE_DIR as STATE_DIR


# Event types (v0)
EVENT_TYPES = {
    "ARTIFACT_DISCOVERED",
    "ARTIFACT_INGESTED",
    "TILE_CREATED",
    "MOTIFS_ASSIGNED",
    "INDEX_UPDATED",
    "FIELD_UPDATED",
    "QUERY_RECEIVED",
    "CANDIDATES_SELECTED",
    "RESPONSE_EMITTED",
    "CONTRADICTION_DETECTED",
    "BRANCH_CREATED",
    "BRANCH_MERGED",
}

DEFAULT_LOG_PATH = "{STATE_DIR}/hmm_events.jsonl"


@dataclass
class Actor:
    id: str = "spark-claude"
    node: str = "DGX-Spark-1"
    embodiment: str = "CLI"


@dataclass
class GateSnapshot:
    phi: float = 0.0
    trust: float = 0.0
    flags: List[str] = field(default_factory=list)


@dataclass
class Event:
    event_id: str = ""
    ts: str = ""
    actor: Actor = field(default_factory=Actor)
    type: str = ""
    refs: Dict[str, str] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)
    gate: GateSnapshot = field(default_factory=GateSnapshot)

    def __post_init__(self):
        if not self.event_id:
            self.event_id = str(uuid.uuid4())
        if not self.ts:
            self.ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        actor = Actor(**d.get("actor", {}))
        gate = GateSnapshot(**d.get("gate", {}))
        return cls(
            event_id=d.get("event_id", ""),
            ts=d.get("ts", ""),
            actor=actor,
            type=d.get("type", ""),
            refs=d.get("refs", {}),
            payload=d.get("payload", {}),
            gate=gate,
        )


class EventLog:
    """Append-only JSONL event log with atomic writes."""

    def __init__(self, path: str = DEFAULT_LOG_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: Event) -> str:
        """
        Atomically append an event to the log.

        Returns the event_id.
        """
        line = json.dumps(event.to_dict(), separators=(",", ":")) + "\n"
        with open(self.path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return event.event_id

    def emit(
        self,
        event_type: str,
        refs: Optional[Dict[str, str]] = None,
        payload: Optional[Dict[str, Any]] = None,
        gate: Optional[GateSnapshot] = None,
    ) -> Event:
        """Create and append an event in one call."""
        event = Event(
            type=event_type,
            refs=refs or {},
            payload=payload or {},
            gate=gate or GateSnapshot(),
        )
        self.append(event)
        return event

    def tail(self, n: int = 20) -> List[Event]:
        """Read the last n events."""
        if not self.path.exists():
            return []
        events = []
        with open(self.path, "r") as f:
            lines = f.readlines()
        for line in lines[-n:]:
            line = line.strip()
            if line:
                events.append(Event.from_dict(json.loads(line)))
        return events

    def iter_all(self) -> Iterator[Event]:
        """Iterate over all events in order."""
        if not self.path.exists():
            return
        with open(self.path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield Event.from_dict(json.loads(line))

    def count(self) -> int:
        """Count total events."""
        if not self.path.exists():
            return 0
        count = 0
        with open(self.path, "r") as f:
            for _ in f:
                count += 1
        return count

    def replay(self, handler) -> int:
        """
        Replay all events through a handler function.

        handler(event: Event) is called for each event in order.
        Returns the number of events replayed.
        """
        count = 0
        for event in self.iter_all():
            handler(event)
            count += 1
        return count

    def clear(self):
        """Clear the event log (for testing/rebuild only)."""
        if self.path.exists():
            self.path.unlink()
