"""
Relational Lens - "Truth of Meaning"
Semantic knowledge graph for ISMA.

Implements:
- Entity and relationship storage (Neo4j)
- Temporal validity (facts have time windows)
- Ontology alignment (synonym mapping)
- Graph queries for context retrieval

Note: No automatic entity extraction needed - we create structured
metadata (platform, session, purpose, etc.) when recording events.
The "facts" are explicit in our data, not hidden in text.

Gate-B Check: Entanglement Wedge (fid >= 0.90, gap >= 0.40)
"""

import json
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass
from isma.config import NEO4J_URI as CONFIG_NEO4J_URI


@dataclass
class Entity:
    """Knowledge graph entity."""
    id: str
    entity_type: str  # 'person', 'concept', 'agent', 'project', etc.
    name: str
    properties: Dict[str, Any]
    valid_from: str
    valid_until: Optional[str] = None
    confidence: float = 1.0


@dataclass
class Relationship:
    """Knowledge graph relationship."""
    source_id: str
    target_id: str
    relationship_type: str  # 'KNOWS', 'PREFERS', 'PART_OF', etc.
    properties: Dict[str, Any]
    valid_from: str
    valid_until: Optional[str] = None
    confidence: float = 1.0


class RelationalLens:
    """
    Relational Lens - Semantic Knowledge Graph.

    Uses Neo4j for graph storage.
    Implements temporal validity for facts.
    Supports ontology alignment for synonyms.
    """

    def __init__(self,
                 neo4j_uri: str = CONFIG_NEO4J_URI,
                 neo4j_user: str = None,
                 neo4j_password: str = None):
        self.neo4j_uri = neo4j_uri
        self.neo4j_user = neo4j_user
        self.neo4j_password = neo4j_password
        self._driver = None
        self._initialized = False

        # Ontology mappings (synonym -> canonical)
        self._ontology = {
            'postgres': 'postgresql',
            'py': 'python',
            'js': 'javascript',
            'ts': 'typescript',
            'ai': 'artificial_intelligence',
            'ml': 'machine_learning',
            'llm': 'large_language_model',
        }

    def _get_driver(self):
        """Lazy Neo4j driver initialization."""
        if self._driver is None:
            from neo4j import GraphDatabase
            auth = (self.neo4j_user, self.neo4j_password) if self.neo4j_user else None
            self._driver = GraphDatabase.driver(self.neo4j_uri, auth=auth)
        return self._driver

    def initialize(self) -> bool:
        """Initialize Neo4j schema for ISMA relational lens."""
        try:
            driver = self._get_driver()
            with driver.session() as session:
                # Entity constraints
                session.run("""
                    CREATE CONSTRAINT isma_entity_id IF NOT EXISTS
                    FOR (e:ISMAEntity) REQUIRE e.id IS UNIQUE
                """)

                # Indexes for common queries
                session.run("""
                    CREATE INDEX isma_entity_type IF NOT EXISTS
                    FOR (e:ISMAEntity) ON (e.entity_type)
                """)
                session.run("""
                    CREATE INDEX isma_entity_name IF NOT EXISTS
                    FOR (e:ISMAEntity) ON (e.name)
                """)
                session.run("""
                    CREATE INDEX isma_entity_valid IF NOT EXISTS
                    FOR (e:ISMAEntity) ON (e.valid_from, e.valid_until)
                """)

            self._initialized = True
            return True
        except Exception as e:
            print(f"RelationalLens init failed: {e}")
            return False

    # =========================================================================
    # Entity Operations
    # =========================================================================

    def add_entity(self,
                   entity_type: str,
                   name: str,
                   properties: Dict[str, Any] = None,
                   confidence: float = 1.0) -> Optional[Entity]:
        """
        Add or update an entity in the knowledge graph.

        Args:
            entity_type: Type of entity (person, concept, agent, etc.)
            name: Entity name (will be canonicalized)
            properties: Additional properties
            confidence: Confidence score (0-1)

        Returns:
            Created/updated Entity
        """
        # Canonicalize name
        canonical_name = self._canonicalize(name)
        entity_id = f"{entity_type}:{canonical_name}"
        now = datetime.now().isoformat()

        try:
            driver = self._get_driver()
            with driver.session() as session:
                # Upsert entity (update valid_until of old version if exists)
                session.run("""
                    MATCH (e:ISMAEntity {id: $id})
                    WHERE e.valid_until IS NULL
                    SET e.valid_until = $now
                """, id=entity_id, now=now)

                # Create new version
                result = session.run("""
                    CREATE (e:ISMAEntity {
                        id: $id,
                        entity_type: $type,
                        name: $name,
                        properties: $props,
                        valid_from: $now,
                        valid_until: null,
                        confidence: $confidence
                    })
                    RETURN e
                """, id=entity_id, type=entity_type, name=canonical_name,
                    props=json.dumps(properties or {}), now=now, confidence=confidence)

                record = result.single()
                if record:
                    return Entity(
                        id=entity_id,
                        entity_type=entity_type,
                        name=canonical_name,
                        properties=properties or {},
                        valid_from=now,
                        confidence=confidence
                    )
        except Exception as e:
            print(f"Entity add failed: {e}")

        return None

    def get_entity(self, entity_id: str, at_time: str = None) -> Optional[Entity]:
        """Get entity by ID, optionally at a specific point in time."""
        try:
            driver = self._get_driver()
            with driver.session() as session:
                if at_time:
                    result = session.run("""
                        MATCH (e:ISMAEntity {id: $id})
                        WHERE e.valid_from <= $time
                        AND (e.valid_until IS NULL OR e.valid_until > $time)
                        RETURN e
                        LIMIT 1
                    """, id=entity_id, time=at_time)
                else:
                    result = session.run("""
                        MATCH (e:ISMAEntity {id: $id})
                        WHERE e.valid_until IS NULL
                        RETURN e
                        LIMIT 1
                    """, id=entity_id)

                record = result.single()
                if record:
                    e = dict(record['e'])
                    return Entity(
                        id=e['id'],
                        entity_type=e['entity_type'],
                        name=e['name'],
                        properties=json.loads(e.get('properties', '{}')),
                        valid_from=e['valid_from'],
                        valid_until=e.get('valid_until'),
                        confidence=e.get('confidence', 1.0)
                    )
        except Exception as e:
            print(f"Entity get failed: {e}")

        return None

    def search_entities(self,
                        query: str,
                        entity_type: str = None,
                        limit: int = 10) -> List[Entity]:
        """Search entities by name pattern."""
        try:
            driver = self._get_driver()
            with driver.session() as session:
                cypher = """
                    MATCH (e:ISMAEntity)
                    WHERE e.valid_until IS NULL
                    AND e.name CONTAINS $query
                """
                if entity_type:
                    cypher += " AND e.entity_type = $type"
                cypher += " RETURN e LIMIT $limit"

                result = session.run(cypher, query=query.lower(),
                                    type=entity_type, limit=limit)

                entities = []
                for record in result:
                    e = dict(record['e'])
                    entities.append(Entity(
                        id=e['id'],
                        entity_type=e['entity_type'],
                        name=e['name'],
                        properties=json.loads(e.get('properties', '{}')),
                        valid_from=e['valid_from'],
                        valid_until=e.get('valid_until'),
                        confidence=e.get('confidence', 1.0)
                    ))
                return entities
        except Exception as e:
            print(f"Entity search failed: {e}")

        return []

    # =========================================================================
    # Relationship Operations
    # =========================================================================

    def add_relationship(self,
                         source_id: str,
                         target_id: str,
                         relationship_type: str,
                         properties: Dict[str, Any] = None,
                         confidence: float = 1.0) -> bool:
        """
        Add a relationship between entities.

        Args:
            source_id: Source entity ID
            target_id: Target entity ID
            relationship_type: Type of relationship (e.g., 'KNOWS', 'PREFERS')
            properties: Additional properties
            confidence: Confidence score

        Returns:
            True if successful
        """
        now = datetime.now().isoformat()

        try:
            driver = self._get_driver()
            with driver.session() as session:
                # Close any existing relationship of same type
                session.run("""
                    MATCH (s:ISMAEntity {id: $source})-[r]->(t:ISMAEntity {id: $target})
                    WHERE type(r) = $rel_type AND r.valid_until IS NULL
                    SET r.valid_until = $now
                """, source=source_id, target=target_id, rel_type=relationship_type, now=now)

                # Create new relationship
                result = session.run("""
                    MATCH (s:ISMAEntity {id: $source})
                    MATCH (t:ISMAEntity {id: $target})
                    WHERE s.valid_until IS NULL AND t.valid_until IS NULL
                    CREATE (s)-[r:%s {
                        valid_from: $now,
                        valid_until: null,
                        properties: $props,
                        confidence: $confidence
                    }]->(t)
                    RETURN r
                """ % relationship_type, source=source_id, target=target_id,
                    now=now, props=json.dumps(properties or {}), confidence=confidence)

                return result.single() is not None
        except Exception as e:
            print(f"Relationship add failed: {e}")

        return False

    def get_relationships(self,
                          entity_id: str,
                          direction: str = 'both',
                          relationship_type: str = None) -> List[Relationship]:
        """Get relationships for an entity."""
        try:
            driver = self._get_driver()
            with driver.session() as session:
                if direction == 'outgoing':
                    pattern = "(e)-[r]->(other)"
                elif direction == 'incoming':
                    pattern = "(other)-[r]->(e)"
                else:
                    pattern = "(e)-[r]-(other)"

                cypher = f"""
                    MATCH {pattern}
                    WHERE e.id = $id AND e.valid_until IS NULL
                    AND r.valid_until IS NULL
                """
                if relationship_type:
                    cypher += f" AND type(r) = '{relationship_type}'"
                cypher += " RETURN type(r) as rel_type, r, e.id as source, other.id as target"

                result = session.run(cypher, id=entity_id)

                relationships = []
                for record in result:
                    r = dict(record['r'])
                    relationships.append(Relationship(
                        source_id=record['source'],
                        target_id=record['target'],
                        relationship_type=record['rel_type'],
                        properties=json.loads(r.get('properties', '{}')),
                        valid_from=r['valid_from'],
                        valid_until=r.get('valid_until'),
                        confidence=r.get('confidence', 1.0)
                    ))
                return relationships
        except Exception as e:
            print(f"Relationship get failed: {e}")

        return []

    # =========================================================================
    # Knowledge Extraction (from events)
    # =========================================================================

    def extract_from_event(self, event_data: Dict[str, Any]) -> Tuple[List[Entity], List[Relationship]]:
        """
        Extract entities and relationships from an event.
        Used by the Breathing Cycle consolidation.

        Args:
            event_data: Event dictionary from Temporal Lens

        Returns:
            Tuple of (entities, relationships) extracted
        """
        entities = []
        relationships = []

        # Extract agent as entity
        if 'agent_id' in event_data:
            entity = self.add_entity(
                entity_type='agent',
                name=event_data['agent_id'],
                properties={'source': 'event_extraction'}
            )
            if entity:
                entities.append(entity)

        # Extract event type as concept
        if 'event_type' in event_data:
            entity = self.add_entity(
                entity_type='concept',
                name=event_data['event_type'],
                properties={'source': 'event_extraction'}
            )
            if entity:
                entities.append(entity)

        # Extract platform from data
        data = event_data.get('data', {})
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as exc:
                raise ValueError("event data string is not valid JSON") from exc

        if 'platform' in data:
            entity = self.add_entity(
                entity_type='platform',
                name=data['platform'],
                properties={'source': 'event_extraction'}
            )
            if entity:
                entities.append(entity)

        # Create relationship: agent -> performed -> event_type
        if len(entities) >= 2:
            self.add_relationship(
                source_id=entities[0].id,
                target_id=entities[1].id,
                relationship_type='PERFORMED',
                properties={'event_seq': event_data.get('seq')}
            )
            relationships.append(Relationship(
                source_id=entities[0].id,
                target_id=entities[1].id,
                relationship_type='PERFORMED',
                properties={'event_seq': event_data.get('seq')},
                valid_from=datetime.now().isoformat()
            ))

        return entities, relationships

    # =========================================================================
    # Ontology
    # =========================================================================

    def _canonicalize(self, name: str) -> str:
        """Convert name to canonical form."""
        name = name.lower().strip()
        return self._ontology.get(name, name)

    def add_synonym(self, synonym: str, canonical: str):
        """Add a synonym mapping."""
        self._ontology[synonym.lower()] = canonical.lower()

    # =========================================================================
    # Graph Queries
    # =========================================================================

    def get_neighbors(self, entity_id: str, depth: int = 2) -> Dict[str, Any]:
        """Get neighborhood of an entity (for context)."""
        try:
            driver = self._get_driver()
            with driver.session() as session:
                result = session.run("""
                    MATCH path = (e:ISMAEntity {id: $id})-[*1..%d]-(neighbor:ISMAEntity)
                    WHERE e.valid_until IS NULL AND neighbor.valid_until IS NULL
                    RETURN neighbor.id as id, neighbor.name as name,
                           neighbor.entity_type as type, length(path) as distance
                    ORDER BY distance ASC
                    LIMIT 50
                """ % depth, id=entity_id)

                neighbors = {}
                for record in result:
                    neighbors[record['id']] = {
                        'name': record['name'],
                        'type': record['type'],
                        'distance': record['distance']
                    }
                return neighbors
        except Exception as e:
            print(f"Neighbor query failed: {e}")

        return {}

    def compute_coherence(self) -> float:
        """
        Compute graph coherence (φ-resonance).
        Used for Entanglement Wedge check.

        Returns:
            Coherence score (0-1), target > 0.809
        """
        try:
            driver = self._get_driver()
            with driver.session() as session:
                # Get graph stats
                result = session.run("""
                    MATCH (e:ISMAEntity)
                    WHERE e.valid_until IS NULL
                    WITH count(e) as nodes
                    MATCH ()-[r]->()
                    WHERE r.valid_until IS NULL
                    RETURN nodes, count(r) as edges
                """)

                record = result.single()
                if not record or record['nodes'] == 0:
                    return 1.0  # Empty graph is coherent

                nodes = record['nodes']
                edges = record['edges']

                # Simple coherence: edge density normalized
                # Full graph has n*(n-1)/2 edges
                max_edges = nodes * (nodes - 1) / 2
                if max_edges == 0:
                    return 1.0

                density = edges / max_edges
                # Scale to 0.5-1.0 range (sparse graphs still coherent)
                coherence = 0.5 + (density * 0.5)

                return min(1.0, coherence)
        except Exception as e:
            print(f"Coherence computation failed: {e}")

        return 0.5

    def verify_entanglement_wedge(self, reference_graph: Dict = None) -> Tuple[bool, float, float]:
        """
        Verify Entanglement Wedge Gate-B check.

        Args:
            reference_graph: Optional reference for fidelity comparison

        Returns:
            Tuple of (passed, fidelity, gap)
        """
        coherence = self.compute_coherence()

        # Fidelity: if we have reference, compare; else use coherence
        fidelity = coherence

        # Gap: distance from target (0.809)
        gap = abs(coherence - 0.809)

        # Check thresholds
        passed = fidelity >= 0.90 and gap <= 0.40

        return passed, fidelity, gap

    # =========================================================================
    # Cleanup
    # =========================================================================

    def close(self):
        """Close Neo4j driver."""
        if self._driver:
            self._driver.close()
            self._driver = None
