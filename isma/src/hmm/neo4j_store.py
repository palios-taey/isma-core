"""
HMM Neo4j Store - relational meaning layer.

Idempotent upserts for Artifact, Tile, Motif nodes and their relationships.
All writes are MERGE-based (safe to re-run).

Phase 4 additions:
  - SUPERSEDES: tracks re-enrichment version chains
  - REVISES: tracks non-destructive mind-change lineage
  - CONTRADICTS: first-class contradiction edges with confidence
  - IN_SESSION: links HMMTile to ISMASession via shared content_hash
  - Temporal chain queries and session reconstruction
"""

import logging
import threading
import time
from typing import List, Dict, Any, Optional
from dataclasses import asdict
from neo4j import GraphDatabase
from isma.config import NEO4J_URI

from .motifs import MotifAssignment, DICTIONARY_VERSION

log = logging.getLogger(__name__)

# Shared driver singleton — avoids creating new connection pools per request
_shared_driver = None
_driver_lock = threading.Lock()


def get_shared_driver(uri: str = NEO4J_URI):
    """Get or create the shared Neo4j driver singleton (thread-safe)."""
    global _shared_driver
    if _shared_driver is None:
        with _driver_lock:
            if _shared_driver is None:
                _shared_driver = GraphDatabase.driver(uri, auth=None)
    return _shared_driver


class HMMNeo4jStore:
    """Neo4j store for HMM relational data."""

    def __init__(self, uri: str = NEO4J_URI):
        self.driver = get_shared_driver(uri)
        self._ensure_indexes()

    def close(self):
        # Don't close the shared driver — it's reused across instances
        pass

    def _ensure_indexes(self):
        """Create required indexes if they don't exist."""
        indexes = [
            "CREATE INDEX hmm_artifact_id IF NOT EXISTS FOR (a:HMMArtifact) ON (a.artifact_id)",
            "CREATE INDEX hmm_tile_id IF NOT EXISTS FOR (t:HMMTile) ON (t.tile_id)",
            "CREATE INDEX hmm_motif_id IF NOT EXISTS FOR (m:HMMMotif) ON (m.motif_id)",
            "CREATE INDEX hmm_event_id IF NOT EXISTS FOR (e:HMMEvent) ON (e.event_id)",
            "CREATE INDEX hmm_tile_artifact IF NOT EXISTS FOR (t:HMMTile) ON (t.artifact_id)",
            "CREATE INDEX hmm_bridge_hash IF NOT EXISTS FOR (b:WeaviateBridge) ON (b.content_hash)",
            "CREATE INDEX hmm_bridge_status IF NOT EXISTS FOR (b:WeaviateBridge) ON (b.status)",
            # Phase 4: temporal truth indexes
            "CREATE INDEX hmm_tile_hash IF NOT EXISTS FOR (t:HMMTile) ON (t.content_hash)",
            "CREATE INDEX hmm_tile_enriched IF NOT EXISTS FOR (t:HMMTile) ON (t.enriched_at)",
        ]
        with self.driver.session() as session:
            for idx in indexes:
                session.run(idx)

    # --- Artifact operations ---

    def upsert_artifact(
        self,
        artifact_id: str,
        path: str,
        size_bytes: int = 0,
        content_type: str = "text/plain",
        labels: Optional[List[str]] = None,
    ):
        """Upsert an Artifact node."""
        query = """
        MERGE (a:HMMArtifact {artifact_id: $artifact_id})
        ON CREATE SET a.created_at = datetime()
        SET a.path = $path,
            a.size_bytes = $size_bytes,
            a.content_type = $content_type,
            a.labels = [x IN (coalesce(a.labels, []) + $labels) WHERE x <> '' | x],
            a.updated_at = datetime()
        """
        with self.driver.session() as session:
            session.run(
                query,
                artifact_id=artifact_id,
                path=path,
                size_bytes=size_bytes,
                content_type=content_type,
                labels=labels or [],
            )

    # --- Tile operations ---

    def upsert_tile(
        self,
        tile_id: str,
        artifact_id: str,
        index: int,
        start_char: int,
        end_char: int,
        estimated_tokens: int,
        layer: str = "",
        scale: str = "",
        lineage_root: str = "",
        provenance_hash: str = "",
    ):
        """Upsert a Tile node and link to its Artifact."""
        query = """
        MERGE (t:HMMTile {tile_id: $tile_id})
        ON CREATE SET t.created_at = datetime(),
                      t.valid_from = datetime()
        SET t.artifact_id = $artifact_id,
            t.index = $index,
            t.start_char = $start_char,
            t.end_char = $end_char,
            t.estimated_tokens = $estimated_tokens,
            t.layer = $layer,
            t.scale = $scale,
            t.lineage_root = CASE WHEN $lineage_root = "" THEN coalesce(t.lineage_root, "") ELSE $lineage_root END,
            t.provenance_hash = CASE WHEN $provenance_hash = "" THEN coalesce(t.provenance_hash, "") ELSE $provenance_hash END,
            t.superseded_by = coalesce(t.superseded_by, ""),
            t.invalidated_at = coalesce(t.invalidated_at, ""),
            t.valid_from = coalesce(t.valid_from, datetime()),
            t.updated_at = datetime()

        WITH t
        MATCH (a:HMMArtifact {artifact_id: $artifact_id})
        MERGE (a)-[:HAS_TILE]->(t)
        """
        with self.driver.session() as session:
            session.run(
                query,
                tile_id=tile_id,
                artifact_id=artifact_id,
                index=index,
                start_char=start_char,
                end_char=end_char,
                estimated_tokens=estimated_tokens,
                layer=layer,
                scale=scale,
                lineage_root=lineage_root,
                provenance_hash=provenance_hash,
            )

    # --- Motif operations ---

    def upsert_motif(
        self,
        motif_id: str,
        definition: str,
        dictionary_version: str = DICTIONARY_VERSION,
        band: str = "",
    ):
        """Upsert a Motif node."""
        query = """
        MERGE (m:HMMMotif {motif_id: $motif_id})
        ON CREATE SET m.created_at = datetime()
        SET m.definition = $definition,
            m.dictionary_version = $dictionary_version,
            m.band = $band,
            m.updated_at = datetime()
        """
        with self.driver.session() as session:
            session.run(
                query,
                motif_id=motif_id,
                definition=definition,
                dictionary_version=dictionary_version,
                band=band,
            )

    def seed_motifs(self, motifs: Dict[str, Any]):
        """Seed all motifs from the dictionary into Neo4j."""
        for motif_id, motif in motifs.items():
            self.upsert_motif(
                motif_id=motif_id,
                definition=motif.definition,
                dictionary_version=DICTIONARY_VERSION,
                band=motif.band,
            )

    # --- Relationship operations ---

    def link_tile_motif(
        self,
        tile_id: str,
        assignment: MotifAssignment,
        model_id: Optional[str] = None,
    ):
        """Create EXPRESSES relationship between Tile and Motif."""
        query = """
        MATCH (t:HMMTile {tile_id: $tile_id})
        MATCH (m:HMMMotif {motif_id: $motif_id})
        MERGE (t)-[r:EXPRESSES]->(m)
        SET r.amp = $amp,
            r.phase = $phase,
            r.confidence = $confidence,
            r.source = $source,
            r.dictionary_version = $dictionary_version,
            r.model_id = $model_id
        """
        with self.driver.session() as session:
            session.run(
                query,
                tile_id=tile_id,
                motif_id=assignment.motif_id,
                amp=assignment.amp,
                phase=assignment.phase,
                confidence=assignment.confidence,
                source=assignment.source,
                dictionary_version=assignment.dictionary_version,
                model_id=model_id or "",
            )

    def link_tile_motifs_batch(
        self,
        tile_id: str,
        assignments: List[MotifAssignment],
        model_id: Optional[str] = None,
    ):
        """Batch link tile to multiple motifs."""
        for assignment in assignments:
            self.link_tile_motif(tile_id, assignment, model_id=model_id)

    # --- Query operations ---

    def find_tiles_by_motif(
        self,
        motif_id: str,
        min_amp: float = 0.0,
        limit: int = 50,
    ) -> List[Dict]:
        """Find tiles that express a motif strongly."""
        query = """
        MATCH (t:HMMTile)-[r:EXPRESSES]->(m:HMMMotif {motif_id: $motif_id})
        WHERE r.amp >= $min_amp
        RETURN t.tile_id AS tile_id, t.artifact_id AS artifact_id,
               t.index AS index, t.layer AS layer, t.scale AS scale,
               r.amp AS amp, r.confidence AS confidence
        ORDER BY r.amp DESC
        LIMIT $limit
        """
        with self.driver.session() as session:
            result = session.run(
                query, motif_id=motif_id, min_amp=min_amp, limit=limit
            )
            return [dict(r) for r in result]

    def get_motif_distribution(self, artifact_id: str) -> List[Dict]:
        """Get motif distribution for an artifact."""
        query = """
        MATCH (a:HMMArtifact {artifact_id: $artifact_id})-[:HAS_TILE]->
              (t:HMMTile)-[r:EXPRESSES]->(m:HMMMotif)
        RETURN m.motif_id AS motif_id, m.band AS band,
               avg(r.amp) AS mean_amp, count(*) AS tile_count
        ORDER BY mean_amp DESC
        """
        with self.driver.session() as session:
            result = session.run(query, artifact_id=artifact_id)
            return [dict(r) for r in result]

    def get_tile_motifs(self, tile_id: str) -> List[Dict]:
        """Get all motif assignments for a tile."""
        query = """
        MATCH (t:HMMTile {tile_id: $tile_id})-[r:EXPRESSES]->(m:HMMMotif)
        RETURN m.motif_id AS motif_id, m.band AS band, m.definition AS definition,
               r.amp AS amp, r.confidence AS confidence, r.source AS source
        ORDER BY r.amp DESC
        """
        with self.driver.session() as session:
            result = session.run(query, tile_id=tile_id)
            return [dict(r) for r in result]

    def get_artifact_tiles(self, artifact_id: str) -> List[Dict]:
        """Get all tiles for an artifact."""
        query = """
        MATCH (a:HMMArtifact {artifact_id: $artifact_id})-[:HAS_TILE]->(t:HMMTile)
        RETURN t.tile_id AS tile_id, t.index AS index,
               t.start_char AS start_char, t.end_char AS end_char,
               t.estimated_tokens AS estimated_tokens, t.scale AS scale
        ORDER BY t.index
        """
        with self.driver.session() as session:
            result = session.run(query, artifact_id=artifact_id)
            return [dict(r) for r in result]

    def count_nodes(self) -> Dict[str, int]:
        """Count all HMM node types."""
        counts = {}
        with self.driver.session() as session:
            for label in ["HMMArtifact", "HMMTile", "HMMMotif", "HMMEvent", "WeaviateBridge"]:
                result = session.run(f"MATCH (n:{label}) RETURN count(n) AS c")
                counts[label] = result.single()["c"]
        return counts

    # --- WeaviateBridge operations ---

    def upsert_bridge_status(
        self,
        content_hash: str,
        status: str,
        tiles_enriched: int = 0,
        version: str = "1.0.0",
        error: str = "",
    ):
        """Upsert a WeaviateBridge tracking node and link to HMMTile."""
        query = """
        MERGE (b:WeaviateBridge {content_hash: $content_hash})
        ON CREATE SET b.created_at = datetime(),
                      b.retry_count = 0
        SET b.status = $status,
            b.tiles_enriched = $tiles_enriched,
            b.enrichment_version = $version,
            b.error_message = $error,
            b.updated_at = datetime()

        WITH b
        OPTIONAL MATCH (t:HMMTile {tile_id: $content_hash})
        FOREACH (_ IN CASE WHEN t IS NOT NULL THEN [1] ELSE [] END |
            MERGE (t)-[:BRIDGED_TO]->(b)
        )
        """
        # Increment retry_count on failure
        if status == "FAILED":
            query = """
            MERGE (b:WeaviateBridge {content_hash: $content_hash})
            ON CREATE SET b.created_at = datetime(),
                          b.retry_count = 0
            SET b.status = $status,
                b.tiles_enriched = $tiles_enriched,
                b.enrichment_version = $version,
                b.error_message = $error,
                b.retry_count = coalesce(b.retry_count, 0) + 1,
                b.updated_at = datetime()

            WITH b
            OPTIONAL MATCH (t:HMMTile {tile_id: $content_hash})
            FOREACH (_ IN CASE WHEN t IS NOT NULL THEN [1] ELSE [] END |
                MERGE (t)-[:BRIDGED_TO]->(b)
            )
            """

        with self.driver.session() as session:
            session.run(
                query,
                content_hash=content_hash,
                status=status,
                tiles_enriched=tiles_enriched,
                version=version,
                error=error,
            )

    def get_pending_bridges(self, limit: int = 100) -> List[Dict]:
        """Find HMMTiles that don't have a COMPLETED bridge status."""
        query = """
        MATCH (t:HMMTile)
        WHERE NOT EXISTS {
            MATCH (t)-[:BRIDGED_TO]->(b:WeaviateBridge {status: 'COMPLETED'})
        }
        RETURN t.tile_id AS tile_id, t.artifact_id AS artifact_id
        LIMIT $limit
        """
        with self.driver.session() as session:
            result = session.run(query, limit=limit)
            return [dict(r) for r in result]

    def get_bridge_stats(self) -> Dict[str, int]:
        """Count WeaviateBridge nodes by status."""
        query = """
        MATCH (b:WeaviateBridge)
        RETURN b.status AS status, count(b) AS count
        """
        stats = {}
        with self.driver.session() as session:
            result = session.run(query)
            for r in result:
                stats[r["status"] or "NULL"] = r["count"]
        return stats

    # --- Phase 4: Temporal Truth Methods ---

    def mark_superseded(
        self,
        old_tile_id: str,
        new_tile_id: str,
        evidence: str = "",
        old_rosetta: str = "",
        old_motifs: Optional[List[str]] = None,
    ):
        """Create SUPERSEDES edge when a tile is re-enriched.

        The new tile supersedes the old one. If old_tile_id == new_tile_id
        (same content re-enriched), we create a snapshot node first.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        if old_tile_id == new_tile_id:
            # Same tile re-enriched — create a version snapshot
            snapshot_id = f"{old_tile_id}_v{int(time.time())}"
            query = """
            MATCH (t:HMMTile {tile_id: $old_tile_id})
            CREATE (snap:HMMTile {
                tile_id: $snapshot_id,
                content_hash: t.content_hash,
                rosetta_summary: $old_rosetta,
                dominant_motifs: $old_motifs,
                enrichment_version: t.enrichment_version,
                enriched_at: t.enriched_at,
                platform: t.platform,
                created_at: t.created_at,
                updated_at: $now,
                is_snapshot: true,
                valid_from: t.enriched_at,
                superseded_by: $new_tile_id,
                invalidated_at: $now
            })
            CREATE (t)-[r:SUPERSEDES {
                valid_from: t.enriched_at,
                superseded_at: $now,
                evidence: $evidence
            }]->(snap)
            SET t.valid_from = coalesce(t.valid_from, t.enriched_at)
            RETURN snap.tile_id AS snapshot_id
            """
            with self.driver.session() as session:
                result = session.run(
                    query,
                    old_tile_id=old_tile_id,
                    snapshot_id=snapshot_id,
                    new_tile_id=new_tile_id,
                    old_rosetta=old_rosetta or "",
                    old_motifs=old_motifs or [],
                    now=now,
                    evidence=evidence,
                )
                rec = result.single()
                if rec:
                    log.info("Created snapshot %s for re-enrichment of %s",
                             snapshot_id, old_tile_id)
        else:
            # Different tiles — direct SUPERSEDES
            query = """
            MATCH (new:HMMTile {tile_id: $new_tile_id})
            MATCH (old:HMMTile {tile_id: $old_tile_id})
            MERGE (new)-[r:SUPERSEDES]->(old)
            SET r.valid_from = new.enriched_at,
                r.superseded_at = $now,
                r.evidence = $evidence,
                old.valid_from = coalesce(old.valid_from, old.enriched_at, $now),
                old.superseded_by = $new_tile_id,
                old.invalidated_at = $now,
                new.valid_from = coalesce(new.valid_from, new.enriched_at, $now)
            """
            with self.driver.session() as session:
                session.run(
                    query,
                    new_tile_id=new_tile_id,
                    old_tile_id=old_tile_id,
                    now=now,
                    evidence=evidence,
                )

    def mark_revised(
        self,
        old_tile_id: str,
        new_tile_id: str,
        evidence: str = "",
        old_memory_zone: str = "sandbox",
        old_authority: str = "advisory",
        new_memory_zone: str = "canon",
        new_authority: str = "binding",
    ):
        """Create a REVISES edge for non-destructive mind-change lineage."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        query = """
        MATCH (new:HMMTile {tile_id: $new_tile_id})
        MATCH (old:HMMTile {tile_id: $old_tile_id})
        MERGE (new)-[r:REVISES]->(old)
        SET r.valid_from = coalesce(new.enriched_at, new.valid_from, $now),
            r.revised_at = $now,
            r.evidence = $evidence,
            old.correction_status = 'revised',
            old.memory_zone = $old_memory_zone,
            old.authority = $old_authority,
            old.is_superseded = false,
            old.superseded_by = "",
            old.invalidated_at = "",
            old.valid_from = coalesce(old.valid_from, old.enriched_at, $now),
            new.correction_status = 'current',
            new.memory_zone = $new_memory_zone,
            new.authority = $new_authority,
            new.is_superseded = false,
            new.valid_from = coalesce(new.valid_from, new.enriched_at, $now)
        RETURN count(r) AS revised
        """
        with self.driver.session() as session:
            result = session.run(
                query,
                new_tile_id=new_tile_id,
                old_tile_id=old_tile_id,
                now=now,
                evidence=evidence,
                old_memory_zone=old_memory_zone,
                old_authority=old_authority,
                new_memory_zone=new_memory_zone,
                new_authority=new_authority,
            )
            rec = result.single()
            if not rec or rec["revised"] < 1:
                raise RuntimeError(
                    f"REVISES edge not created for {new_tile_id} -> {old_tile_id}"
                )

    def mark_contradiction(
        self,
        tile_a_id: str,
        tile_b_id: str,
        confidence: float,
        resolution: str = "",
        detected_by: str = "reranker",
    ):
        """Create first-class CONTRADICTS edge between two tiles.

        Unlike RELATES_TO {type: "contradicts"} which comes from HMM enrichment,
        CONTRADICTS edges are programmatically verified with confidence scores.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        query = """
        MATCH (a:HMMTile {tile_id: $tile_a})
        MATCH (b:HMMTile {tile_id: $tile_b})
        MERGE (a)-[r:CONTRADICTS]->(b)
        SET r.detected_at = $now,
            r.confidence = $confidence,
            r.resolution = $resolution,
            r.detected_by = $detected_by
        RETURN count(r) AS contradicted
        """
        with self.driver.session() as session:
            result = session.run(
                query,
                tile_a=tile_a_id,
                tile_b=tile_b_id,
                now=now,
                confidence=confidence,
                resolution=resolution,
                detected_by=detected_by,
            )
            rec = result.single()
            if not rec or rec["contradicted"] < 1:
                raise RuntimeError(
                    f"CONTRADICTS edge not created for {tile_a_id} -> {tile_b_id}"
                )

    def link_tile_to_session(self, tile_id: str, session_id: str, exchange_index: int = -1):
        """Create IN_SESSION edge from HMMTile to ISMASession.

        Links tiles to their originating session via the shared content_hash.
        """
        query = """
        MATCH (t:HMMTile {tile_id: $tile_id})
        MATCH (s:ISMASession {session_id: $session_id})
        MERGE (t)-[r:IN_SESSION]->(s)
        SET r.exchange_index = $exchange_index
        """
        with self.driver.session() as session:
            session.run(
                query,
                tile_id=tile_id,
                session_id=session_id,
                exchange_index=exchange_index,
            )

    def backfill_session_links(self, limit: int = 5000) -> int:
        """Bulk link HMMTiles to ISMASessions via shared content_hash.

        Returns count of new IN_SESSION edges created.
        """
        query = """
        MATCH (t:HMMTile)
        WHERE NOT EXISTS { MATCH (t)-[:IN_SESSION]->() }
        WITH t LIMIT $limit
        MATCH (e:ISMAExchange {content_hash: t.content_hash})
        MATCH (s:ISMASession)-[:CONTAINS]->(e)
        MERGE (t)-[r:IN_SESSION]->(s)
        SET r.exchange_index = e.exchange_index
        RETURN count(r) AS created
        """
        with self.driver.session() as session:
            result = session.run(query, limit=limit)
            rec = result.single()
            return rec["created"] if rec else 0

    def get_temporal_chain(self, content_hash: str) -> List[Dict]:
        """Follow SUPERSEDES chain for a content_hash.

        Returns all versions from newest to oldest.
        """
        query = """
        MATCH (t:HMMTile {content_hash: $content_hash})
        WHERE NOT EXISTS { MATCH ()-[:SUPERSEDES]->(t) WHERE coalesce(t.is_snapshot, false) = false }
        OPTIONAL MATCH chain = (t)-[:SUPERSEDES*0..10]->(old)
        WITH nodes(chain) AS versions
        UNWIND versions AS v
        RETURN DISTINCT v.tile_id AS tile_id,
               v.rosetta_summary AS rosetta_summary,
               v.dominant_motifs AS dominant_motifs,
               v.enrichment_version AS enrichment_version,
               v.enriched_at AS enriched_at,
               v.platform AS platform,
               v.is_snapshot AS is_snapshot
        ORDER BY v.enriched_at DESC
        """
        with self.driver.session() as session:
            result = session.run(query, content_hash=content_hash)
            return [dict(r) for r in result]

    def get_contradictions(
        self,
        min_confidence: float = 0.5,
        limit: int = 50,
    ) -> List[Dict]:
        """Get all detected contradictions above confidence threshold."""
        query = """
        MATCH (a:HMMTile)-[r:CONTRADICTS]->(b:HMMTile)
        WHERE r.confidence >= $min_confidence
        RETURN a.tile_id AS tile_a, a.rosetta_summary AS rosetta_a,
               b.tile_id AS tile_b, b.rosetta_summary AS rosetta_b,
               r.confidence AS confidence, r.detected_at AS detected_at,
               r.resolution AS resolution, r.detected_by AS detected_by
        ORDER BY r.confidence DESC
        LIMIT $limit
        """
        with self.driver.session() as session:
            result = session.run(query, min_confidence=min_confidence, limit=limit)
            return [dict(r) for r in result]

    def reconstruct_session(self, session_id: str) -> List[Dict]:
        """Reconstruct a session's tiles in exchange order.

        Returns HMMTiles linked to a session, preferring the latest
        version (following SUPERSEDES chains).
        """
        query = """
        MATCH (s:ISMASession {session_id: $session_id})<-[r:IN_SESSION]-(t:HMMTile)
        WHERE coalesce(t.is_snapshot, false) = false
        OPTIONAL MATCH (t)-[:SUPERSEDES]->(old:HMMTile)
        RETURN t.tile_id AS tile_id,
               t.content_hash AS content_hash,
               t.rosetta_summary AS rosetta_summary,
               t.dominant_motifs AS dominant_motifs,
               t.enrichment_version AS enrichment_version,
               t.enriched_at AS enriched_at,
               r.exchange_index AS exchange_index,
               count(old) AS superseded_count
        ORDER BY r.exchange_index
        """
        with self.driver.session() as session:
            result = session.run(query, session_id=session_id)
            return [dict(r) for r in result]

    def get_tile_contradictions(self, tile_id: str) -> List[Dict]:
        """Get all contradictions for a specific tile."""
        query = """
        MATCH (t:HMMTile {tile_id: $tile_id})-[r:CONTRADICTS]-(other:HMMTile)
        RETURN other.tile_id AS other_tile_id,
               other.rosetta_summary AS other_rosetta,
               r.confidence AS confidence,
               r.detected_at AS detected_at,
               r.resolution AS resolution,
               CASE WHEN startNode(r) = t THEN 'outgoing' ELSE 'incoming' END AS direction
        ORDER BY r.confidence DESC
        """
        with self.driver.session() as session:
            result = session.run(query, tile_id=tile_id)
            return [dict(r) for r in result]

    def graph_expand(
        self,
        tile_ids: List[str],
        depth: int = 2,
        follow_supersedes: bool = True,
        relates_to_jaccard_min: float = 0.6,
    ) -> List[Dict]:
        """Expand from seed tiles through SUPERSEDES, CONTRADICTS, and filtered RELATES_TO.

        RELATES_TO edges are fetched separately at depth-1 only, filtered by jaccard
        threshold (default 0.6). This prevents the dense RELATES_TO graph (5M+ edges)
        from overwhelming results when doing multi-hop SUPERSEDES traversals.
        """
        if follow_supersedes:
            struct_types = "SUPERSEDES|CONTRADICTS"
        else:
            struct_types = "SUPERSEDES"

        # Query 1: structural multi-hop traversal (SUPERSEDES / CONTRADICTS)
        struct_query = f"""
        UNWIND $tile_ids AS seed_id
        MATCH (seed:HMMTile {{tile_id: seed_id}})
        OPTIONAL MATCH path = (seed)-[:{struct_types}*1..{depth}]-(neighbor:HMMTile)
        WHERE coalesce(neighbor.is_snapshot, false) = false
              AND neighbor.tile_id <> seed.tile_id
        RETURN DISTINCT neighbor.tile_id AS tile_id,
               neighbor.content_hash AS content_hash,
               neighbor.rosetta_summary AS rosetta_summary,
               neighbor.dominant_motifs AS dominant_motifs,
               neighbor.platform AS platform,
               neighbor.enriched_at AS enriched_at
        ORDER BY enriched_at DESC
        LIMIT 80
        """

        # Query 2: high-confidence RELATES_TO neighbors (depth-1 only, jaccard-filtered)
        relates_query = """
        UNWIND $tile_ids AS seed_id
        MATCH (seed:HMMTile {tile_id: seed_id})
        MATCH (seed)-[r:RELATES_TO]-(neighbor:HMMTile)
        WHERE r.jaccard >= $jmin
              AND coalesce(neighbor.is_snapshot, false) = false
              AND neighbor.tile_id <> seed.tile_id
        RETURN DISTINCT neighbor.tile_id AS tile_id,
               neighbor.content_hash AS content_hash,
               neighbor.rosetta_summary AS rosetta_summary,
               neighbor.dominant_motifs AS dominant_motifs,
               neighbor.platform AS platform,
               neighbor.enriched_at AS enriched_at
        ORDER BY neighbor.enriched_at DESC
        LIMIT 20
        """

        seen: set = set()
        neighbors: List[Dict] = []
        with self.driver.session() as session:
            for row in session.run(struct_query, tile_ids=tile_ids):
                d = dict(row)
                if d.get("tile_id") and d["tile_id"] not in seen:
                    seen.add(d["tile_id"])
                    neighbors.append(d)
            for row in session.run(relates_query, tile_ids=tile_ids,
                                   jmin=relates_to_jaccard_min):
                d = dict(row)
                if d.get("tile_id") and d["tile_id"] not in seen:
                    seen.add(d["tile_id"])
                    neighbors.append(d)
        return neighbors[:100]

    def graph_expand_typed(
        self,
        content_hashes: List[str],
        depth: int = 2,
        limit: int = 50,
    ) -> List[Dict]:
        """Expand from seed content_hashes through typed edges only.

        Uses supports_topk (447K edges, weighted by similarity) and
        motif_cooccurrence (71K edges) instead of untyped RELATES_TO (105M).
        Much faster and more meaningful than RELATES_TO traversal.

        Returns neighbor tiles ordered by edge weight (strongest support first).
        """
        # All edges are stored as RELATES_TO with r.type property
        query = """
        UNWIND $hashes AS seed_hash
        MATCH (seed:HMMTile {content_hash: seed_hash})
        MATCH (seed)-[r:RELATES_TO]-(neighbor:HMMTile)
        WHERE r.type IN ['supports_topk', 'motif_cooccurrence', 'challenges_topk',
                          'builds_on', 'extends', 'references']
              AND coalesce(neighbor.is_snapshot, false) = false
              AND NOT neighbor.content_hash IN $hashes
        WITH DISTINCT neighbor, max(coalesce(r.weight, 0.5)) AS best_weight
        RETURN neighbor.tile_id AS tile_id,
               neighbor.content_hash AS content_hash,
               neighbor.rosetta_summary AS rosetta_summary,
               neighbor.dominant_motifs AS dominant_motifs,
               neighbor.platform AS platform,
               neighbor.enriched_at AS enriched_at,
               best_weight AS weight
        ORDER BY best_weight DESC
        LIMIT $limit
        """
        with self.driver.session() as session:
            result = session.run(query, hashes=content_hashes, limit=limit)
            return [dict(r) for r in result]

    def wipe(self):
        """Delete all HMM nodes and relationships (for rebuild)."""
        with self.driver.session() as session:
            session.run("""
                MATCH (n) WHERE n:HMMArtifact OR n:HMMTile OR n:HMMMotif
                    OR n:HMMEvent OR n:WeaviateBridge
                DETACH DELETE n
            """)
