import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("WEAVIATE_URL", "http://localhost:8080")
os.environ.setdefault("EMBEDDING_URL", "http://localhost:8091/v1/embeddings")

import isma.src.provenance_scorer as scorer
from isma.src.isma_core import ISMACore
from isma.src.retrieval import TileResult


class CorrectionStatusExtensionTest(unittest.TestCase):
    def test_revised_prior_stays_rankable_below_current_successor(self):
        old_graph_weight = scorer.W_GRAPH
        scorer.W_GRAPH = 0.0
        try:
            prior = TileResult(
                content="old position",
                score=0.8,
                tile_id="old-v1",
                scale="search_512",
                source_type="document",
                source_file="decision.md",
                content_hash="old-hash",
                truth_tier="verified",
                authority="advisory",
                memory_zone="sandbox",
                correction_status="revised",
                lineage_root="decision",
            )
            successor = TileResult(
                content="new position",
                score=0.8,
                tile_id="new-v1",
                scale="search_512",
                source_type="document",
                source_file="decision.md",
                content_hash="new-hash",
                truth_tier="verified",
                authority="binding",
                memory_zone="canon",
                correction_status="current",
                lineage_root="decision",
            )

            ranked = scorer.apply_provenance_scoring(
                [prior, successor],
                query_type="exact",
            )
        finally:
            scorer.W_GRAPH = old_graph_weight

        self.assertEqual([t.content_hash for t in ranked], ["new-hash", "old-hash"])
        self.assertGreater(ranked[1].score, 0.01)
        self.assertEqual(scorer.correction_obedience(prior), 0.65)

    def test_contested_obedience_is_symmetric_downweight(self):
        tile_a = TileResult(
            content="a",
            score=0.5,
            tile_id="a",
            scale="search_512",
            source_type="document",
            source_file="a.md",
            content_hash="a",
            correction_status="contested",
        )
        tile_b = TileResult(
            content="b",
            score=0.5,
            tile_id="b",
            scale="search_512",
            source_type="document",
            source_file="b.md",
            content_hash="b",
            correction_status="contested",
        )

        self.assertEqual(scorer.correction_obedience(tile_a), 0.6)
        self.assertEqual(scorer.correction_obedience(tile_b), 0.6)

    def test_mark_revised_materializes_v1_without_superseding(self):
        core = object.__new__(ISMACore)
        patches = []
        store = Mock()

        def fake_patch(_self, tile_id, properties, operation):
            patches.append((tile_id, properties, operation))

        with patch.object(ISMACore, "_patch_isma_quantum_tile", fake_patch), patch(
            "isma.src.hmm.neo4j_store.HMMNeo4jStore",
            return_value=store,
        ):
            core.mark_revised(
                ["old-v1"],
                ["new-v1"],
                evidence="operator accepted mind-change",
                old_graph_ids=["old-graph"],
                new_graph_ids=["new-graph"],
            )

        self.assertEqual(patches[0][0], "old-v1")
        self.assertEqual(patches[0][1]["correction_status"], "revised")
        self.assertEqual(patches[0][1]["memory_zone"], "sandbox")
        self.assertEqual(patches[0][1]["authority"], "advisory")
        self.assertIs(patches[0][1]["is_superseded"], False)
        self.assertEqual(patches[0][1]["superseded_by"], "")

        self.assertEqual(patches[1][0], "new-v1")
        self.assertEqual(patches[1][1]["correction_status"], "current")
        self.assertEqual(patches[1][1]["memory_zone"], "canon")
        self.assertEqual(patches[1][1]["authority"], "binding")
        self.assertIs(patches[1][1]["is_superseded"], False)

        store.mark_revised.assert_called_once_with(
            "old-graph",
            "new-graph",
            evidence="operator accepted mind-change",
        )


if __name__ == "__main__":
    unittest.main()
