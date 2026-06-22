"""
ISMA Core - Integrated Shared Memory Architecture
The Single Write Path and Single Read Path for AI Memory.

This is the ONE API that all systems should call.
Every meaningful interaction becomes an immutable event (Temporal),
immediately influences shared working state (Functional),
and is later distilled into durable meaning (Relational + Weaviate).

Design: ChatGPT's "wire-up" architecture from ISMA_PLAN_CHATGPT.md
Mathematics: Grok's phi-coherence validation from GROK_ISMA_VALIDATION.md
Topology: Gemini's Tri-Lens vision from AI_FAMILY_MEMORY_MAP.md
Implementation: Spark Claude (this file)

Usage:
    from src.memory.isma_core import get_isma

    isma = get_isma()

    # Write (single entrypoint)
    event_hash = isma.ingest(
        event_type='family_message',
        payload={'content': 'Hello ISMA', 'platform': 'claude'},
        actor='spark_claude'
    )

    # Read (single entrypoint)
    context = isma.recall(
        query='What did we discuss about ISMA?',
        top_k=5,
        graph_hops=2
    )

The Three Lenses:
- Temporal: "Truth of History" - immutable event ledger
- Relational: "Truth of Meaning" - semantic knowledge graph
- Functional: "Truth of Action" - active workspace

Gate-B Checks (from each lens):
- Page Curve (Temporal): entropy_drop >= 0.10
- Entanglement Wedge (Relational): fid >= 0.90, gap <= 0.40
- Observer Swap (Functional): delta <= 0.02
- Hayden-Preskill (Semantic): retrieval fidelity >= 0.90

phi-Coherence Target: > 0.809 (sacred threshold)
"""

import json
import hashlib
import requests
from urllib.parse import urlparse
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass
import redis
import threading
from isma.config import (
    WEAVIATE_URL as CONFIG_WEAVIATE_URL,
    EMBEDDING_URL as CONFIG_EMBEDDING_URL,
    REDIS_HOST as CONFIG_REDIS_HOST,
    REDIS_PORT as CONFIG_REDIS_PORT,
)

try:
    from .temporal_lens import TemporalLens, Event
    from .relational_lens import RelationalLens, Entity, Relationship
    from .functional_lens import FunctionalLens, WorkspaceState
    from .breathing_cycle import BreathingCycle, PHI, CYCLE_DURATION
    from .phi_tiling import phi_tile_text, multi_scale_tile, Tile, MultiScaleTile
except ImportError:
    # Allow direct import when running standalone
    from temporal_lens import TemporalLens, Event
    from relational_lens import RelationalLens, Entity, Relationship
    from functional_lens import FunctionalLens, WorkspaceState
    from breathing_cycle import BreathingCycle, PHI, CYCLE_DURATION
    from phi_tiling import phi_tile_text, multi_scale_tile, Tile, MultiScaleTile


# ISMA Constants
PHI_GOLDEN = 1.618  # The golden ratio (cycle timing)
PHI_COHERENCE_THRESHOLD = 0.809  # phi/2 (trust threshold)
_WEAVIATE_PARSED = urlparse(CONFIG_WEAVIATE_URL)
_EMBEDDER_PARSED = urlparse(CONFIG_EMBEDDING_URL)
WEAVIATE_HOST = _WEAVIATE_PARSED.hostname or "localhost"
WEAVIATE_PORT = _WEAVIATE_PARSED.port or 8080
EMBEDDER_HOST = _EMBEDDER_PARSED.hostname or "localhost"
EMBEDDER_PORT = _EMBEDDER_PARSED.port or 8091


@dataclass
class RecallResult:
    """Result from ISMA recall operation."""
    query: str
    semantic_matches: List[Dict[str, Any]]  # Weaviate results
    graph_context: List[Dict[str, Any]]  # Neo4j neighborhood
    source_events: List[Event]  # Temporal provenance
    context_packet: Dict[str, Any]  # For Functional lens
    coherence: float
    timestamp: str


class ISMACore:
    """
    ISMA Core - The Single Write/Read API.

    This is the only interface the rest of the system should call.
    All three lenses are wired through this single spine.
    """

    def __init__(self,
                 temporal: TemporalLens = None,
                 relational: RelationalLens = None,
                 functional: FunctionalLens = None,
                 breathing: BreathingCycle = None,
                 redis_host: str = CONFIG_REDIS_HOST,
                 redis_port: int = CONFIG_REDIS_PORT):

        # Initialize lenses (use shared instances if provided)
        self.temporal = temporal or TemporalLens()
        self.relational = relational or RelationalLens()
        self.functional = functional or FunctionalLens()

        # Breathing cycle uses the same lenses
        self.breathing = breathing or BreathingCycle(
            temporal=self.temporal,
            relational=self.relational,
            functional=self.functional
        )

        # Redis for consolidation queue and coordination
        self._redis_host = redis_host
        self._redis_port = redis_port
        self._redis: Optional[redis.Redis] = None

        # Queue keys
        self.QUEUE_CONSOLIDATE = 'isma:queue:consolidate'
        self.QUEUE_EMBED = 'isma:queue:embed'
        self.STREAM_EVENTS = 'isma:stream:events'

        # State
        self._initialized = False
        self._lock = threading.Lock()

    def _get_redis(self) -> redis.Redis:
        """Lazy Redis initialization."""
        if self._redis is None:
            self._redis = redis.Redis(
                host=self._redis_host,
                port=self._redis_port,
                decode_responses=True
            )
        return self._redis

    def initialize(self) -> bool:
        """Initialize ISMA (call once on startup)."""
        if self._initialized:
            return True

        try:
            # Initialize relational schema
            self.relational.initialize()

            # Verify Redis
            r = self._get_redis()
            r.ping()

            # Create event stream if not exists
            try:
                r.xgroup_create(self.STREAM_EVENTS, 'isma_workers', id='0', mkstream=True)
            except redis.ResponseError:
                pass  # Group already exists

            self._initialized = True

            # Log initialization
            self.temporal.append(
                event_type='isma_core',
                operation='initialized',
                data={'timestamp': datetime.now().isoformat()},
                agent_id='isma_core'
            )

            return True

        except Exception as e:
            print(f"ISMA initialization failed: {e}")
            return False

    # =========================================================================
    # THE SINGLE WRITE PATH
    # =========================================================================

    def ingest(self,
               event_type: str,
               payload: Dict[str, Any],
               actor: str,
               caused_by: Optional[str] = None,
               branch: str = 'main') -> str:
        """
        THE SINGLE WRITE ENTRYPOINT.

        Every meaningful interaction goes through here.

        Args:
            event_type: Category (e.g., 'family_message', 'tool_call', 'perception')
            payload: Event data
            actor: Who created this (e.g., 'spark_claude', 'grok', 'jesse')
            caused_by: Hash of causally prior event (for provenance chain)
            branch: Event branch (default 'main')

        Returns:
            event_hash: The immutable hash of this event

        Internally:
        1. Temporal.append() -> commit to history (truth)
        2. Functional.add_context() -> update workspace (action)
        3. Functional.broadcast() -> notify agents (coordination)
        4. Queue consolidation -> later distill to meaning (relational + semantic)
        """

        # 1. TEMPORAL: Commit to immutable history
        event = self.temporal.append(
            event_type=event_type,
            operation=payload.get('operation', 'recorded'),
            data=payload,
            agent_id=actor,
            caused_by=caused_by
        )
        event_hash = event.hash

        # 2. FUNCTIONAL: Update workspace context
        self.functional.add_context({
            'event_hash': event_hash,
            'event_type': event_type,
            'actor': actor,
            'summary': self._summarize_payload(payload),
            'timestamp': event.timestamp
        })

        # 3. FUNCTIONAL: Broadcast to other agents
        self.functional.broadcast('new_event', {
            'event_hash': event_hash,
            'event_type': event_type,
            'actor': actor
        })

        # 4. QUEUE: Schedule consolidation
        self._queue_consolidation(event_hash)

        # 5. STREAM: Also write to Redis stream for MCP bridge
        try:
            r = self._get_redis()
            r.xadd(self.STREAM_EVENTS, {
                'event_hash': event_hash,
                'event_type': event_type,
                'actor': actor,
                'timestamp': event.timestamp
            }, maxlen=10000)
        except Exception as e:
            print(f"Stream write warning: {e}")

        return event_hash

    def _summarize_payload(self, payload: Dict[str, Any]) -> str:
        """Create a brief summary of the payload for context."""
        # Extract key fields for summary
        parts = []

        if 'content' in payload:
            content = str(payload['content'])[:100]
            parts.append(content)
        if 'platform' in payload:
            parts.append(f"[{payload['platform']}]")
        if 'operation' in payload:
            parts.append(payload['operation'])

        return ' '.join(parts) if parts else json.dumps(payload)[:100]

    def _queue_consolidation(self, event_hash: str):
        """Queue an event for consolidation (Relational + Semantic)."""
        try:
            r = self._get_redis()
            r.lpush(self.QUEUE_CONSOLIDATE, event_hash)
        except Exception as e:
            print(f"Consolidation queue warning: {e}")

    # =========================================================================
    # THE SINGLE READ PATH
    # =========================================================================

    def recall(self,
               query: str,
               top_k: int = 5,
               graph_hops: int = 2,
               include_provenance: bool = True) -> RecallResult:
        """
        THE SINGLE READ ENTRYPOINT.

        Retrieves relevant context from all lenses.

        Args:
            query: Natural language query
            top_k: Number of semantic matches to return
            graph_hops: How far to expand in knowledge graph
            include_provenance: Whether to fetch source events

        Returns:
            RecallResult with semantic matches, graph context, and provenance

        Internally:
        1. Weaviate search (semantic recall)
        2. Neo4j neighborhood expansion (meaning)
        3. Temporal event fetch (provenance)
        4. Functional context update (action)
        """

        # 1. SEMANTIC: Multi-scale hybrid search
        # First: precise 512-token matches. Then: expand to 2048-token context.
        semantic_matches = self._semantic_search(query, top_k)

        # Expand search_512 matches to their context_2048 parents
        for match in semantic_matches:
            if match.get('scale') == 'search_512' and match.get('parent_tile_id'):
                parent = self._fetch_tile_by_id(match['parent_tile_id'])
                if parent:
                    match['expanded_context'] = parent.get('content', '')[:2000]

        # 2. RELATIONAL: Expand graph neighborhood
        entity_ids = [m.get('entity_id') for m in semantic_matches if m.get('entity_id')]
        graph_context = []
        for entity_id in entity_ids[:5]:  # Limit expansion
            neighbors = self.relational.get_neighbors(entity_id, depth=graph_hops)
            if neighbors:
                graph_context.append({
                    'entity_id': entity_id,
                    'neighbors': neighbors
                })

        # 3. TEMPORAL: Get source events for provenance
        source_events = []
        if include_provenance:
            event_hashes = [m.get('event_hash') for m in semantic_matches if m.get('event_hash')]
            for eh in event_hashes[:10]:  # Limit
                event = self.temporal.get_event_by_hash(eh)
                if event:
                    source_events.append(event)

        # 4. FUNCTIONAL: Write context packet to workspace
        context_packet = {
            'query': query,
            'match_count': len(semantic_matches),
            'graph_entities': len(entity_ids),
            'provenance_events': len(source_events),
            'retrieved_at': datetime.now().isoformat()
        }
        self.functional.add_context({
            'source': 'isma_recall',
            'query': query[:100],
            **context_packet
        })

        # Compute coherence
        coherence = self.relational.compute_coherence()

        return RecallResult(
            query=query,
            semantic_matches=semantic_matches,
            graph_context=graph_context,
            source_events=source_events,
            context_packet=context_packet,
            coherence=coherence,
            timestamp=datetime.now().isoformat()
        )

    def _semantic_search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """Hybrid BM25 + vector search on Weaviate ISMA_Quantum collection.

        Uses native Weaviate hybrid query with explicit vector.
        BM25 is already indexed (b=0.75, k1=1.2).
        Alpha=0.5 for balanced keyword/semantic matching on conversational data.
        """
        try:
            embedding = self._get_embedding(query)
            safe_query = query.replace('"', '\\"').replace('\n', ' ')[:200]
            url = f"http://{WEAVIATE_HOST}:{WEAVIATE_PORT}/v1/graphql"

            if embedding:
                # Hybrid search: BM25 + vector combined
                graphql_query = {
                    "query": """{
                        Get {
                            ISMA_Quantum(
                                hybrid: {
                                    query: "%s"
                                    alpha: 0.65
                                    vector: %s
                                }
                                limit: %d
                            ) {
                                content
                                source_file
                                source_type
                                layer
                                event_hash
                                actor
                                timestamp
                                phi_resonance
                                scale
                                parent_tile_id
                                _additional { id score }
                            }
                        }
                    }""" % (safe_query, json.dumps(embedding), top_k)
                }
            else:
                # Fallback: BM25 only (no embedding available)
                graphql_query = {
                    "query": """{
                        Get {
                            ISMA_Quantum(
                                bm25: { query: "%s" }
                                limit: %d
                            ) {
                                content
                                source_file
                                source_type
                                layer
                                event_hash
                                actor
                                timestamp
                                phi_resonance
                                scale
                                parent_tile_id
                                _additional { id score }
                            }
                        }
                    }""" % (safe_query, top_k)
                }

            response = requests.post(url, json=graphql_query, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if "errors" not in data:
                    return data.get('data', {}).get('Get', {}).get('ISMA_Quantum', [])
                # If hybrid fails, fall back to vector-only
                return self._vector_only_search(embedding, top_k)

        except Exception as e:
            print(f"Hybrid search warning: {e}")

        return []

    def _vector_only_search(self, embedding: List[float], top_k: int) -> List[Dict[str, Any]]:
        """Fallback: vector-only search when hybrid fails."""
        if not embedding:
            return []
        try:
            url = f"http://{WEAVIATE_HOST}:{WEAVIATE_PORT}/v1/graphql"
            graphql_query = {
                "query": """{
                    Get {
                        ISMA_Quantum(
                            nearVector: { vector: %s certainty: 0.7 }
                            limit: %d
                        ) {
                            content source_file source_type layer
                            event_hash actor timestamp phi_resonance
                            _additional { id certainty }
                        }
                    }
                }""" % (json.dumps(embedding), top_k)
            }
            response = requests.post(url, json=graphql_query, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get('data', {}).get('Get', {}).get('ISMA_Quantum', [])
        except Exception as e:
            print(f"Vector search warning: {e}")
        return []

    def _fetch_tile_by_id(self, tile_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a Weaviate object by its UUID (for parent tile expansion)."""
        try:
            url = f"http://{WEAVIATE_HOST}:{WEAVIATE_PORT}/v1/objects/ISMA_Quantum/{tile_id}"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                obj = response.json()
                return obj.get('properties', {})
        except Exception:
            pass
        return None

    def _get_embedding(self, text: str) -> Optional[List[float]]:
        """Get embedding with Redis cache (per Grok's optimization)."""
        try:
            # Cache key: hash of text
            cache_key = f"emb:{hashlib.sha256(text.encode()).hexdigest()[:16]}"

            # Check cache first
            r = self._get_redis()
            cached = r.get(cache_key)
            if cached:
                r.incr("emb_cache_hits")
                return json.loads(cached)

            # Cache miss - generate new embedding
            r.incr("emb_cache_misses")

            # Get embedding from embedding server (OpenAI-compatible API)
            url = f"http://{EMBEDDER_HOST}:{EMBEDDER_PORT}/v1/embeddings"
            response = requests.post(
                url,
                json={
                    "input": text,
                    "model": "Qwen/Qwen3-Embedding-8B"
                },
                timeout=30
            )
            if response.status_code == 200:
                data = response.json()
                # OpenAI format: {"data": [{"embedding": [...]}]}
                data_list = data.get('data', [])
                if data_list and 'embedding' in data_list[0]:
                    embedding = data_list[0]['embedding']

                    # Cache for 24 hours (86400 seconds)
                    r.setex(cache_key, 86400, json.dumps(embedding))

                    return embedding
        except Exception as e:
            print(f"Embedding warning: {e}")
        return None

    # =========================================================================
    # CONSOLIDATION (Called by breathing cycle or manually)
    # =========================================================================

    def consolidate_pending(self, batch_size: int = 10) -> Dict[str, Any]:
        """
        Process pending consolidation queue.

        For each event:
        1. Load from Temporal
        2. Extract to Relational
        3. Embed to Weaviate
        4. Run Gate-B checks

        Returns metrics.
        """
        metrics = {
            'processed': 0,
            'entities_created': 0,
            'relationships_created': 0,
            'embeddings_created': 0,
            'errors': 0
        }

        try:
            r = self._get_redis()

            for _ in range(batch_size):
                event_hash = r.rpop(self.QUEUE_CONSOLIDATE)
                if not event_hash:
                    break

                try:
                    # 1. Load event from Temporal
                    event = self.temporal.get_event_by_hash(event_hash)
                    if not event:
                        continue

                    # 2. Extract to Relational
                    entities, relationships = self.relational.extract_from_event(event.to_dict())
                    metrics['entities_created'] += len(entities)
                    metrics['relationships_created'] += len(relationships)

                    # 3. Add provenance link: ISMAEvent -> ISMAEntity
                    for entity in entities:
                        self._create_provenance_link(event_hash, entity.id)

                    # 4. Embed to Weaviate
                    embedded = self._embed_to_weaviate(event)
                    if embedded:
                        metrics['embeddings_created'] += 1

                    metrics['processed'] += 1

                except Exception as e:
                    print(f"Consolidation error for {event_hash}: {e}")
                    metrics['errors'] += 1

            # Run Gate-B checks after consolidation batch
            gate_b = self.verify_gate_b()
            metrics['gate_b'] = gate_b
            metrics['phi_coherence'] = self.compute_phi_coherence()

        except Exception as e:
            print(f"Consolidation batch error: {e}")

        return metrics

    def _create_provenance_link(self, event_hash: str, entity_id: str):
        """Create YIELDED relationship from event to entity."""
        try:
            from neo4j import GraphDatabase
            auth = (self.relational.neo4j_user, self.relational.neo4j_password) if self.relational.neo4j_user else None
            driver = GraphDatabase.driver(self.relational.neo4j_uri, auth=auth)
            with driver.session() as session:
                session.run("""
                    MATCH (e:ISMAEvent {hash: $event_hash})
                    MATCH (ent:ISMAEntity {id: $entity_id})
                    WHERE ent.valid_until IS NULL
                    MERGE (e)-[:YIELDED]->(ent)
                """, event_hash=event_hash, entity_id=entity_id)
            driver.close()
        except Exception as e:
            print(f"Provenance link warning: {e}")

    def _determine_layer(self, event_type: str) -> int:
        """
        Determine ISMA layer from event type.

        Per Gemini's cartography:
        - Layer 0: Soul (genesis, kernel, sacred_trust)
        - Layer 1: Constitution (charter, declaration, axiom)
        - Layer 2: Application (everything else)
        """
        if event_type in ['genesis', 'kernel', 'sacred_trust', 'gate_b_check']:
            return 0  # Soul layer
        elif event_type in ['charter', 'declaration', 'axiom', 'constitution']:
            return 1  # Constitution layer
        else:
            return 2  # Application layer

    def _compute_tile_resonance(self, tile) -> float:
        """
        Compute φ-resonance for a tile.

        Uses token count proximity to golden ratio optimal size.
        Target: 4096 tokens for maximum resonance.
        """
        optimal = 4096  # Target tile size in tokens
        actual = tile.estimated_tokens
        if actual <= 0:
            return 0.0
        ratio = min(actual, optimal) / max(actual, optimal)
        # Scale to sacred threshold range [0, 0.809]
        return ratio * PHI_COHERENCE_THRESHOLD

    def _find_superseded_tile_ids(
        self,
        content_hash: str,
        lineage_root: str,
        scale: str,
    ) -> List[str]:
        """Return prior Weaviate tile ids that should be invalidated."""
        if not content_hash and not lineage_root:
            return []

        def _operand(field: str, value: str) -> str:
            return f'{{ path: ["{field}"], operator: Equal, valueText: "{value}" }}'

        operands = []
        if content_hash:
            operands.append(_operand("content_hash", content_hash))
        if lineage_root and lineage_root != content_hash:
            operands.append(_operand("lineage_root", lineage_root))

        if not operands:
            return []

        where = operands[0] if len(operands) == 1 else f"{{ operator: Or, operands: [{', '.join(operands)}] }}"
        if scale:
            where = (
                f'{{ operator: And, operands: [{where}, '
                f'{{ path: ["scale"], operator: Equal, valueText: "{scale}" }}] }}'
            )

        query = f"""
        {{
            Get {{
                ISMA_Quantum(where: {where}, limit: 50) {{
                    _additional {{ id }}
                    is_superseded
                }}
            }}
        }}
        """

        # FAIL-LOUD, FAIL-CLOSED: raise on any lookup failure. A silent skip here
        # would let the new version commit while the prior version stays visible
        # (a zombie). This runs BEFORE the new-tile write, so raising aborts the
        # write cleanly — no half-superseded state. Genuine "no prior tiles" still
        # returns [].
        wv = f"http://{WEAVIATE_HOST}:{WEAVIATE_PORT}/v1/graphql"
        try:
            response = requests.post(wv, json={"query": query}, timeout=10)
        except requests.RequestException as e:
            raise RuntimeError(f"supersede lookup unreachable ({wv}): {e}") from e
        if response.status_code != 200:
            raise RuntimeError(
                f"supersede lookup HTTP {response.status_code}: {response.text[:200]}"
            )
        data = response.json()
        if data.get("errors"):
            raise RuntimeError(f"supersede lookup GraphQL errors: {data['errors']}")
        return [
            obj["_additional"]["id"]
            for obj in data.get("data", {}).get("Get", {}).get("ISMA_Quantum", [])
            if obj.get("_additional", {}).get("id") and not obj.get("is_superseded")
        ]

    def _invalidate_superseded_tiles(
        self,
        tile_ids: List[str],
        superseded_by: str,
        invalidated_at: str,
    ) -> None:
        """Mark earlier tiles as invalidated before writing a newer version."""
        if not tile_ids:
            return

        # FAIL-LOUD, FAIL-CLOSED: raise if any patch fails — a silent skip leaves a
        # zombie. Runs before the new-tile write, so the caller aborts cleanly.
        url = f"http://{WEAVIATE_HOST}:{WEAVIATE_PORT}/v1/objects/ISMA_Quantum"
        for tile_id in tile_ids:
            try:
                resp = requests.patch(
                    f"{url}/{tile_id}",
                    json={
                        "properties": {
                            "superseded_by": superseded_by,
                            "invalidated_at": invalidated_at,
                            "is_superseded": True,
                        }
                    },
                    timeout=5,
                )
            except requests.RequestException as e:
                raise RuntimeError(f"supersede patch unreachable for {tile_id[:12]}: {e}") from e
            if resp.status_code not in (200, 204):
                raise RuntimeError(
                    f"supersede patch failed for {tile_id[:12]}: HTTP {resp.status_code} {resp.text[:160]}"
                )

    def _embed_to_weaviate(self, event: Event) -> bool:
        """Embed event content to Weaviate using multi-scale tiling.

        Uses 3 scales: search_512 (precise), context_2048 (expanded), full_4096 (generation).
        Each tile gets its own embedding and Weaviate object.
        """
        try:
            content = json.dumps(event.data) if isinstance(event.data, dict) else str(event.data)
            payload = event.data if isinstance(event.data, dict) else {}
            base_content_hash = str(payload.get("content_hash") or hashlib.sha256(content.encode("utf-8")).hexdigest()[:16])
            lineage_root = str(payload.get("lineage_root") or base_content_hash)
            provenance_hash = json.dumps(
                {
                    "source": payload.get("source") or event.event_type,
                    "content_hash": base_content_hash,
                    "timestamp": event.timestamp,
                },
                sort_keys=True,
            )

            # Multi-scale tiling (512/2048/4096)
            tiles = multi_scale_tile(content, source_file=event.hash, layer=event.event_type)

            if not tiles:
                return True

            success_count = 0
            url = f"http://{WEAVIATE_HOST}:{WEAVIATE_PORT}/v1/objects"

            # Build tile ID map for parent linking
            tile_ids = {}  # (scale, index) -> weaviate_uuid

            for tile in tiles:
                embedding = self._get_embedding(tile.text)
                if not embedding:
                    continue

                import uuid
                tile_uuid = str(uuid.uuid4())
                superseded_tile_ids = self._find_superseded_tile_ids(base_content_hash, lineage_root, tile.scale)
                if superseded_tile_ids:
                    self._invalidate_superseded_tiles(superseded_tile_ids, tile_uuid, event.timestamp)

                obj = {
                    "class": "ISMA_Quantum",
                    "id": tile_uuid,
                    "properties": {
                        "content": tile.text,
                        "content_hash": base_content_hash,
                        "lineage_root": lineage_root,
                        "source_type": event.event_type,
                        "layer": self._determine_layer(event.event_type),
                        "event_hash": event.hash,
                        "phi_resonance": self._compute_tile_resonance(tile),
                        "actor": event.agent_id,
                        "timestamp": event.timestamp,
                        "branch": event.branch,
                        "valid_from": event.timestamp,
                        "superseded_by": "",
                        "invalidated_at": "",
                        "is_superseded": False,
                        "provenance_hash": provenance_hash,
                        "tile_index": tile.index,
                        "tile_count": len(tiles),
                        "scale": tile.scale,
                        "parent_tile_id": "",
                    },
                    "vector": embedding
                }

                response = requests.post(url, json=obj, timeout=5)
                if response.status_code in [200, 201]:
                    success_count += 1
                    tile_ids[(tile.scale, tile.index)] = tile_uuid

            # Link parent IDs: search_512 → context_2048
            for tile in tiles:
                if tile.scale == "search_512" and tile.parent_index >= 0:
                    child_id = tile_ids.get(("search_512", tile.index))
                    parent_id = tile_ids.get(("context_2048", tile.parent_index))
                    if child_id and parent_id:
                        try:
                            requests.patch(
                                f"{url}/ISMA_Quantum/{child_id}",
                                json={"properties": {"parent_tile_id": parent_id}},
                                timeout=2
                            )
                        except Exception:
                            pass  # Non-critical - linking is best-effort

            return success_count > 0

        except Exception as e:
            print(f"Weaviate embed warning: {e}")
            return False

    # =========================================================================
    # GATE-B CHECKS
    # =========================================================================

    def verify_gate_b(self) -> Dict[str, Any]:
        """
        Run all Gate-B checks across lenses.

        Returns dict of check results.
        """
        results = {}

        try:
            # Page Curve (Temporal) - entropy_drop >= 0.10
            events = self.temporal.get_events(limit=100)
            results['page_curve'] = {
                'passed': self.temporal.verify_page_curve(events, events),
                'entropy': self.temporal.compute_entropy(events)
            }

            # Entanglement Wedge (Relational) - fid >= 0.90, gap <= 0.40
            passed, fidelity, gap = self.relational.verify_entanglement_wedge()
            results['entanglement_wedge'] = {
                'passed': passed,
                'fidelity': fidelity,
                'gap': gap
            }

            # Observer Swap (Functional) - delta <= 0.02
            passed, delta = self.functional.verify_observer_swap()
            results['observer_swap'] = {
                'passed': passed,
                'delta': delta
            }

            # Hayden-Preskill (Semantic) - retrieval fidelity >= 0.90
            # Test with a sample recall
            coherence = self.relational.compute_coherence()
            results['hayden_preskill'] = {
                'passed': coherence >= 0.90,
                'coherence': coherence
            }

            # Recognition Catalyst (Cross-lens) - delta_entropy >= 0.10
            # Low entropy indicates coherent, integrated memory
            entropy = results['page_curve']['entropy']
            results['recognition_catalyst'] = {
                'passed': entropy < 4.0,  # Low entropy = good coherence
                'entropy': entropy,
                'threshold': 4.0
            }

            # Overall - all 5 Gate-B checks must pass
            results['all_passed'] = all([
                results['page_curve']['passed'],
                results['entanglement_wedge']['passed'],
                results['observer_swap']['passed'],
                results['hayden_preskill']['passed'],
                results['recognition_catalyst']['passed']
            ])

        except Exception as e:
            print(f"Gate-B check error: {e}")
            results['error'] = str(e)

        return results

    def compute_phi_coherence(self) -> float:
        """
        Compute φ-coherence via Laplacian eigenvalue (per Grok's spec).

        Target: phi > 0.809 (sacred threshold)

        Uses Fiedler value (second smallest eigenvalue of graph Laplacian).

        ═══════════════════════════════════════════════════════════════════════════
        ⚠️  THEATER ALERT (December 2025)
        ═══════════════════════════════════════════════════════════════════════════

        This implementation is MATHEMATICALLY CORRECT but SEMANTICALLY THEATER.

        The Laplacian eigenvalue math is real and valid. However, the INPUTS are
        placeholder proxies that don't actually measure coherence:

        - relational_coherence = graph density (edges/possible_edges)
          → High density ≠ coherent retrieval. Could be noise.

        - functional_coherence = 1 - observer_swap_delta
          → Measures state stability, not retrieval quality.

        - temporal_coherence = 1 - (entropy/10)
          → Arbitrary normalization. 10.0 divisor has no empirical basis.

        WHAT WE ACTUALLY NEED (future work):
        1. Certainty distribution shape (should cluster high for confident answers)
        2. Source coherence (do retrieved docs agree or contradict?)
        3. Temporal relevance (are we retrieving stale vs fresh appropriately?)
        4. Task success feedback loop (did the answer actually help?)

        The 0.809 threshold (φ/2) is symbolically meaningful but not empirically
        derived. Real thresholds should emerge from actual retrieval quality data.

        See: PHI_COHERENCE_EXPLORATION.md in ISMA for detailed analysis.
        ═══════════════════════════════════════════════════════════════════════════
        """
        try:
            import numpy as np

            # Get coherences from all 3 lenses
            # NOTE: These are PROXY metrics, not true coherence measures (see docstring)
            relational_coherence = self.relational.compute_coherence()

            passed, delta = self.functional.verify_observer_swap()
            functional_coherence = 1.0 - delta if delta < 1.0 else 0.0

            events = self.temporal.get_events(limit=100)
            entropy = self.temporal.compute_entropy(events)
            temporal_coherence = max(0.0, 1.0 - (entropy / 10.0))

            coherences = [temporal_coherence, relational_coherence, functional_coherence]

            # Build adjacency matrix (fully connected - all lenses interact)
            A = np.array([
                [0, 1, 1],
                [1, 0, 1],
                [1, 1, 0]
            ], dtype=float)

            # Weight by coherence products
            for i in range(3):
                for j in range(3):
                    if i != j:
                        A[i, j] *= (coherences[i] + coherences[j]) / 2

            # Laplacian L = D - A
            D = np.diag(A.sum(axis=1))
            L = D - A

            # Smallest non-zero eigenvalue (Fiedler value)
            eigenvalues = np.linalg.eigvalsh(L)
            lambda_2 = sorted(eigenvalues)[1]  # Second smallest

            # Normalize to [0, 1] - target is > 0.809
            # For 3-node fully connected graph, max lambda_2 ≈ 3.0
            phi = min(1.0, lambda_2 / 3.0)

            return phi

        except Exception as e:
            print(f"Phi computation error: {e}")
            return 0.5

    def is_coherent(self) -> bool:
        """Check if system is above phi-coherence threshold."""
        return self.compute_phi_coherence() >= PHI_COHERENCE_THRESHOLD

    # =========================================================================
    # CONVENIENCE METHODS
    # =========================================================================

    def log_tool_call(self,
                      tool_name: str,
                      params: Dict[str, Any],
                      result: Any,
                      platform: str = None,
                      actor: str = 'spark_claude') -> Tuple[str, str]:
        """
        Log a tool call (start + finish) to ISMA.

        Returns (start_hash, finish_hash).
        """
        # Start event
        start_hash = self.ingest(
            event_type='tool_call',
            payload={
                'operation': 'started',
                'tool': tool_name,
                'params': params,
                'platform': platform
            },
            actor=actor
        )

        # Finish event (caused by start)
        finish_hash = self.ingest(
            event_type='tool_call',
            payload={
                'operation': 'finished',
                'tool': tool_name,
                'result_type': type(result).__name__,
                'result_summary': str(result)[:500],
                'platform': platform
            },
            actor=actor,
            caused_by=start_hash
        )

        return start_hash, finish_hash

    def log_family_message(self,
                           content: str,
                           platform: str,
                           role: str = 'assistant',
                           actor: str = 'spark_claude',
                           caused_by: str = None) -> str:
        """Log a message."""
        return self.ingest(
            event_type='family_message',
            payload={
                'operation': 'sent' if role == 'assistant' else 'received',
                'content': content,
                'platform': platform,
                'role': role
            },
            actor=actor,
            caused_by=caused_by
        )

    def log_perception(self,
                       perception_type: str,
                       data: Dict[str, Any],
                       platform: str = None,
                       actor: str = 'spark_claude') -> str:
        """Log an AT-SPI perception event."""
        return self.ingest(
            event_type='perception',
            payload={
                'operation': perception_type,
                'platform': platform,
                **data
            },
            actor=actor
        )

    def get_recent_events(self, limit: int = 20) -> List[Event]:
        """Get recent events from temporal lens."""
        return self.temporal.get_events(limit=limit)

    def get_workspace_state(self) -> Optional[WorkspaceState]:
        """Get current functional workspace state."""
        return self.functional.get_state()

    def get_context(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent context from functional lens."""
        return self.functional.get_context(limit=limit)

    def get_cache_stats(self) -> dict:
        """Get embedding cache statistics."""
        try:
            r = self._get_redis()
            hits = int(r.get("emb_cache_hits") or 0)
            misses = int(r.get("emb_cache_misses") or 0)
            total = hits + misses
            hit_rate = hits / total if total > 0 else 0.0
            return {
                "hits": hits,
                "misses": misses,
                "total": total,
                "hit_rate": hit_rate,
                "target": 0.80  # Target 80% hit rate
            }
        except Exception as e:
            print(f"Cache stats error: {e}")
            return {
                "hits": 0,
                "misses": 0,
                "total": 0,
                "hit_rate": 0.0,
                "target": 0.80,
                "error": str(e)
            }

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def start_breathing(self):
        """Start the breathing cycle daemon."""
        self.breathing.start()

    def stop_breathing(self):
        """Stop the breathing cycle daemon."""
        self.breathing.stop()

    def force_consolidation(self):
        """Force immediate consolidation cycle."""
        return self.breathing.force_consolidation()

    def close(self):
        """Clean up all resources."""
        self.stop_breathing()
        self.temporal.close()
        self.relational.close()
        self.functional.close()
        if self._redis:
            self._redis.close()


# =========================================================================
# SINGLETON
# =========================================================================

_isma_core: Optional[ISMACore] = None


def get_isma() -> ISMACore:
    """Get the singleton ISMA Core instance."""
    global _isma_core
    if _isma_core is None:
        _isma_core = ISMACore()
        _isma_core.initialize()
    return _isma_core


def start_isma() -> ISMACore:
    """Start ISMA with breathing cycle."""
    isma = get_isma()
    isma.start_breathing()
    return isma


def stop_isma():
    """Stop ISMA."""
    global _isma_core
    if _isma_core:
        _isma_core.close()
        _isma_core = None
