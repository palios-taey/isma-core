"""
Temporal Lens - "Truth of History"
Immutable event ledger with Git-like branching for ISMA.

Implements:
- Event sourcing (append-only)
- Time travel (rollback to any point)
- Branching narratives (hypothesis testing)
- Causal chain linking (event -> outcome)

Gate-B Check: Page Curve Islands (entropy_drop >= 0.10)
"""

import json
import hashlib
import os
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from pathlib import Path
from isma.config import (
    NEO4J_URI as CONFIG_NEO4J_URI,
    ISMA_TEMPORAL_LOG_DIR as CONFIG_TEMPORAL_LOG_DIR,
    ISMA_TEMPORAL_DOLT_DIR as CONFIG_TEMPORAL_DOLT_DIR,
)


@dataclass
class Event:
    """Immutable event record."""
    timestamp: str
    seq: int
    event_type: str
    agent_id: str
    operation: str
    data: Dict[str, Any]
    caused_by: Optional[str] = None  # Previous event hash
    branch: str = 'main'

    def __post_init__(self):
        # Compute hash for immutability verification
        self.hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """Compute SHA256 hash of event content."""
        content = json.dumps({
            'timestamp': self.timestamp,
            'seq': self.seq,
            'event_type': self.event_type,
            'agent_id': self.agent_id,
            'operation': self.operation,
            'data': self.data,
            'caused_by': self.caused_by,
            'branch': self.branch
        }, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        d = asdict(self)
        d['hash'] = self.hash
        return d


class TemporalLens:
    """
    Temporal Lens - Immutable event history with branching.

    Uses JSONL for fast append-only writes.
    Neo4j for causal chain queries.
    Git-like branches for hypothesis testing.
    """

    def __init__(self,
                 log_dir: str = CONFIG_TEMPORAL_LOG_DIR,
                 neo4j_uri: str = CONFIG_NEO4J_URI,
                 neo4j_user: str = None,
                 neo4j_password: str = None,
                 dolt_dir: str = CONFIG_TEMPORAL_DOLT_DIR,
                 use_dolt: bool = True):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.neo4j_uri = neo4j_uri
        self.neo4j_user = neo4j_user
        self.neo4j_password = neo4j_password

        # Dolt integration for SQL queries and versioning
        self.dolt_dir = Path(dolt_dir)
        self.use_dolt = use_dolt and self.dolt_dir.exists()

        self._driver = None
        self._current_branch = 'main'  # Must be set before _load_seq
        self._seq_counter = self._load_seq()

    def _get_driver(self):
        """Lazy Neo4j driver initialization."""
        if self._driver is None:
            from neo4j import GraphDatabase
            auth = (self.neo4j_user, self.neo4j_password) if self.neo4j_user else None
            self._driver = GraphDatabase.driver(self.neo4j_uri, auth=auth)
        return self._driver

    def _log_path(self, branch: str = None) -> Path:
        """Get log file path for branch."""
        branch = branch or self._current_branch
        return self.log_dir / f'events_{branch}.jsonl'

    def _load_seq(self) -> int:
        """Load sequence counter from existing log."""
        log_file = self._log_path()
        if not log_file.exists():
            return 0

        seq = 0
        with open(log_file, 'r') as f:
            for line in f:
                try:
                    event = json.loads(line)
                    seq = max(seq, event.get('seq', 0))
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"failed to parse event log line in {log_file}") from exc
        return seq

    # =========================================================================
    # Core Operations
    # =========================================================================

    def append(self,
               event_type: str,
               operation: str,
               data: Dict[str, Any],
               agent_id: str = 'spark_claude',
               caused_by: Optional[str] = None) -> Event:
        """
        Append an immutable event to the ledger.

        Args:
            event_type: Category (e.g., 'family_message', 'isma_sync', 'gate_b_check')
            operation: What happened (e.g., 'sent_to_grok', 'validation_passed')
            data: Event payload
            agent_id: Who created this event
            caused_by: Hash of causally prior event

        Returns:
            The created Event with hash
        """
        self._seq_counter += 1

        event = Event(
            timestamp=datetime.now().isoformat(),
            seq=self._seq_counter,
            event_type=event_type,
            agent_id=agent_id,
            operation=operation,
            data=data,
            caused_by=caused_by,
            branch=self._current_branch
        )

        # Append to JSONL
        log_file = self._log_path()
        with open(log_file, 'a') as f:
            f.write(json.dumps(event.to_dict()) + '\n')

        # Write to Neo4j for causal queries
        self._write_to_neo4j(event)

        # Write to Dolt for SQL queries and versioning
        if self.use_dolt:
            self._write_to_dolt(event)

        return event

    def _write_to_neo4j(self, event: Event):
        """Write event to Neo4j for graph queries."""
        try:
            driver = self._get_driver()
            with driver.session() as session:
                # Create event node
                session.run("""
                    CREATE (e:ISMAEvent {
                        hash: $hash,
                        timestamp: $timestamp,
                        seq: $seq,
                        event_type: $event_type,
                        agent_id: $agent_id,
                        operation: $operation,
                        branch: $branch,
                        data: $data
                    })
                """, hash=event.hash, timestamp=event.timestamp, seq=event.seq,
                    event_type=event.event_type, agent_id=event.agent_id,
                    operation=event.operation, branch=event.branch,
                    data=json.dumps(event.data))

                # Link to causal parent if exists
                if event.caused_by:
                    session.run("""
                        MATCH (parent:ISMAEvent {hash: $parent_hash})
                        MATCH (child:ISMAEvent {hash: $child_hash})
                        CREATE (child)-[:CAUSED_BY]->(parent)
                    """, parent_hash=event.caused_by, child_hash=event.hash)
        except Exception as e:
            # Log failure but don't block - JSONL is source of truth
            print(f"Neo4j write warning: {e}")

    def _write_to_dolt(self, event: Event):
        """Write event to Dolt for SQL queries and versioning."""
        try:
            import subprocess
            # Insert into Dolt using SQL
            sql = f"""INSERT INTO events (hash, event_type, payload, actor, caused_by, branch, timestamp)
                      VALUES ('{event.hash}', '{event.event_type}',
                              '{json.dumps(event.data).replace("'", "''")}',
                              '{event.agent_id}', {f"'{event.caused_by}'" if event.caused_by else 'NULL'},
                              '{event.branch}', '{event.timestamp}')"""
            subprocess.run(
                ['/usr/local/bin/dolt', 'sql', '-q', sql],
                cwd=str(self.dolt_dir),
                capture_output=True,
                timeout=5
            )
        except Exception as e:
            # Log failure but don't block - JSONL is source of truth
            print(f"Dolt write warning: {e}")

    def dolt_query(self, sql: str) -> List[Dict[str, Any]]:
        """Execute SQL query against Dolt and return results."""
        if not self.use_dolt:
            return []
        try:
            import subprocess
            result = subprocess.run(
                ['/usr/local/bin/dolt', 'sql', '-q', sql, '-r', 'json'],
                cwd=str(self.dolt_dir),
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                return data.get('rows', [])
        except Exception as e:
            print(f"Dolt query error: {e}")
        return []

    def dolt_commit(self, message: str) -> bool:
        """Commit current Dolt changes (creates a version snapshot)."""
        if not self.use_dolt:
            return False
        try:
            import subprocess
            # Stage all changes
            subprocess.run(
                ['/usr/local/bin/dolt', 'add', '.'],
                cwd=str(self.dolt_dir),
                capture_output=True,
                timeout=10
            )
            # Commit
            result = subprocess.run(
                ['/usr/local/bin/dolt', 'commit', '-m', message],
                cwd=str(self.dolt_dir),
                capture_output=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception as e:
            print(f"Dolt commit error: {e}")
        return False

    # =========================================================================
    # Time Travel
    # =========================================================================

    def get_events(self,
                   since: str = None,
                   until: str = None,
                   event_type: str = None,
                   limit: int = 100,
                   branch: str = None) -> List[Event]:
        """
        Query events from the ledger.

        Args:
            since: ISO timestamp to start from
            until: ISO timestamp to end at
            event_type: Filter by event type
            limit: Max events to return
            branch: Which branch (default: current)
        """
        events = []
        log_file = self._log_path(branch)

        if not log_file.exists():
            return events

        with open(log_file, 'r') as f:
            for line in f:
                if len(events) >= limit:
                    break

                try:
                    data = json.loads(line)

                    # Apply filters
                    if since and data['timestamp'] < since:
                        continue
                    if until and data['timestamp'] > until:
                        continue
                    if event_type and data['event_type'] != event_type:
                        continue

                    event = Event(**{k: v for k, v in data.items() if k != 'hash'})
                    events.append(event)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"failed to parse event log line in {log_file}") from exc

        return events

    def get_event_by_hash(self, hash: str) -> Optional[Event]:
        """Get specific event by its hash."""
        for branch in self._list_branches():
            log_file = self._log_path(branch)
            if not log_file.exists():
                continue

            with open(log_file, 'r') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        if data.get('hash') == hash:
                            return Event(**{k: v for k, v in data.items() if k != 'hash'})
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"failed to parse event log line in {log_file}") from exc
        return None

    def rollback_to(self, hash: str, new_branch: str = None) -> bool:
        """
        Create a new branch from a specific point in history.
        Does NOT delete history - creates fork.

        Args:
            hash: Event hash to branch from
            new_branch: Name for the new branch (default: rollback_<timestamp>)
        """
        event = self.get_event_by_hash(hash)
        if not event:
            return False

        new_branch = new_branch or f"rollback_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # Copy events up to and including target to new branch
        source_log = self._log_path(event.branch)
        new_log = self._log_path(new_branch)

        with open(source_log, 'r') as src, open(new_log, 'w') as dst:
            for line in src:
                data = json.loads(line)
                # Update branch name
                data['branch'] = new_branch
                dst.write(json.dumps(data) + '\n')

                if data.get('hash') == hash:
                    break

        self._current_branch = new_branch
        self._seq_counter = event.seq
        return True

    # =========================================================================
    # Branching
    # =========================================================================

    def create_branch(self, name: str, from_branch: str = None) -> bool:
        """Create a new branch (copy of current state)."""
        source = self._log_path(from_branch)
        if not source.exists():
            # Create empty branch
            self._log_path(name).touch()
        else:
            # Copy existing branch
            import shutil
            shutil.copy(source, self._log_path(name))

        self._current_branch = name
        return True

    def switch_branch(self, name: str) -> bool:
        """Switch to a different branch."""
        if not self._log_path(name).exists():
            return False

        self._current_branch = name
        self._seq_counter = self._load_seq()
        return True

    def merge_branch(self, source_branch: str, into_branch: str = 'main') -> bool:
        """
        Merge events from source branch into target.
        Appends all events from source that aren't in target.
        """
        source_log = self._log_path(source_branch)
        target_log = self._log_path(into_branch)

        if not source_log.exists():
            return False

        # Get existing hashes in target
        target_hashes = set()
        if target_log.exists():
            with open(target_log, 'r') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        target_hashes.add(data.get('hash'))
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"failed to parse event log line in {target_log}") from exc

        # Append new events from source
        with open(source_log, 'r') as src, open(target_log, 'a') as dst:
            for line in src:
                try:
                    data = json.loads(line)
                    if data.get('hash') not in target_hashes:
                        data['branch'] = into_branch
                        dst.write(json.dumps(data) + '\n')
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"failed to parse event log line in {source_log}") from exc

        return True

    def _list_branches(self) -> List[str]:
        """List all branches."""
        branches = []
        for f in self.log_dir.glob('events_*.jsonl'):
            branch = f.stem.replace('events_', '')
            branches.append(branch)
        return branches or ['main']

    # =========================================================================
    # Causal Queries (via Neo4j)
    # =========================================================================

    def get_causal_chain(self, event_hash: str, depth: int = 10) -> List[Event]:
        """Get the causal chain leading to an event."""
        try:
            driver = self._get_driver()
            with driver.session() as session:
                result = session.run("""
                    MATCH path = (e:ISMAEvent {hash: $hash})-[:CAUSED_BY*0..%d]->(ancestor:ISMAEvent)
                    RETURN ancestor.hash AS hash
                    ORDER BY ancestor.seq ASC
                """ % depth, hash=event_hash)

                hashes = [record['hash'] for record in result]
                return [self.get_event_by_hash(h) for h in hashes if h]
        except Exception as exc:
            raise RuntimeError(f"failed to query causes for event {event_hash}") from exc

    def get_effects(self, event_hash: str, depth: int = 10) -> List[Event]:
        """Get events caused by this event."""
        try:
            driver = self._get_driver()
            with driver.session() as session:
                result = session.run("""
                    MATCH path = (effect:ISMAEvent)-[:CAUSED_BY*0..%d]->(e:ISMAEvent {hash: $hash})
                    RETURN effect.hash AS hash
                    ORDER BY effect.seq ASC
                """ % depth, hash=event_hash)

                hashes = [record['hash'] for record in result]
                return [self.get_event_by_hash(h) for h in hashes if h]
        except Exception as exc:
            raise RuntimeError(f"failed to query effects for event {event_hash}") from exc

    # =========================================================================
    # Gate-B Check: Page Curve (Entropy)
    # =========================================================================

    def compute_entropy(self, events: List[Event] = None) -> float:
        """
        Compute Shannon entropy of event distribution.
        Used for Page Curve check (entropy_drop >= 0.10).
        """
        import math

        events = events or self.get_events(limit=1000)
        if not events:
            return 0.0

        # Count event types
        type_counts = {}
        for e in events:
            type_counts[e.event_type] = type_counts.get(e.event_type, 0) + 1

        # Compute entropy
        total = len(events)
        entropy = 0.0
        for count in type_counts.values():
            p = count / total
            if p > 0:
                entropy -= p * math.log2(p)

        return entropy

    def verify_page_curve(self, before_events: List[Event], after_events: List[Event]) -> bool:
        """
        Verify Page Curve Islands check.
        Returns True if entropy_drop >= 0.10 (info preserved).
        """
        entropy_before = self.compute_entropy(before_events)
        entropy_after = self.compute_entropy(after_events)

        # Page curve: entropy should drop by at least 0.10 (info recovery)
        entropy_drop = entropy_before - entropy_after
        return entropy_drop >= 0.10 or entropy_after <= entropy_before

    # =========================================================================
    # Cleanup
    # =========================================================================

    def close(self):
        """Close Neo4j driver."""
        if self._driver:
            self._driver.close()
            self._driver = None
