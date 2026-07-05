import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

os.environ.setdefault("WEAVIATE_URL", "http://localhost:8080")
os.environ.setdefault("EMBEDDING_URL", "http://localhost:8091/v1/embeddings")

import isma.src.provenance_scorer as scorer
from isma.src.hmm.eventlog import EventLog
from isma.src.hmm.neo4j_store import HMMNeo4jStore
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
        calls = []
        store = Mock()

        def fake_read(_self, tile_id, operation):
            calls.append(("read", tile_id, operation))
            return {
                "is_superseded": False,
                "correction_status": "current",
            }

        def fake_patch(_self, tile_id, properties, operation):
            calls.append(("patch", tile_id, properties, operation))

        with patch.object(ISMACore, "_read_isma_quantum_tile_properties", fake_read), patch.object(
            ISMACore, "_patch_isma_quantum_tile", fake_patch
        ), patch(
            "isma.src.hmm.neo4j_store.HMMNeo4jStore",
            return_value=store,
        ), patch("isma.src.hmm.eventlog.EventLog"):
            core.mark_revised(
                ["old-v1"],
                ["new-v1"],
                evidence="operator accepted mind-change",
                old_graph_ids=["old-graph"],
                new_graph_ids=["new-graph"],
            )

        self.assertEqual(calls[0], ("read", "old-v1", "mark_revised"))
        self.assertEqual(calls[1], ("read", "new-v1", "mark_revised"))

        old_patch = calls[2]
        self.assertEqual(old_patch[0], "patch")
        self.assertEqual(old_patch[1], "old-v1")
        self.assertEqual(old_patch[2]["correction_status"], "revised")
        self.assertEqual(old_patch[2]["memory_zone"], "sandbox")
        self.assertEqual(old_patch[2]["authority"], "advisory")
        self.assertIs(old_patch[2]["is_superseded"], False)
        self.assertEqual(old_patch[2]["superseded_by"], "")

        new_patch = calls[3]
        self.assertEqual(new_patch[0], "patch")
        self.assertEqual(new_patch[1], "new-v1")
        self.assertEqual(new_patch[2]["correction_status"], "current")
        self.assertEqual(new_patch[2]["memory_zone"], "canon")
        self.assertEqual(new_patch[2]["authority"], "binding")
        self.assertIs(new_patch[2]["is_superseded"], False)

        store.mark_revised.assert_called_once_with(
            "old-graph",
            "new-graph",
            evidence="operator accepted mind-change",
        )

    def test_mark_revised_emits_branch_created_and_replay_reconstructs_receipt(self):
        core = object.__new__(ISMACore)
        store = Mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            event_log = EventLog(str(Path(tmpdir) / "events.jsonl"))

            with patch.object(
                ISMACore,
                "_read_isma_quantum_tile_properties",
                return_value={"is_superseded": False, "correction_status": "current"},
            ) as read_tile, patch.object(ISMACore, "_patch_isma_quantum_tile"), patch(
                "isma.src.hmm.neo4j_store.HMMNeo4jStore",
                return_value=store,
            ), patch("isma.src.hmm.eventlog.EventLog", return_value=event_log):
                core.mark_revised(
                    ["old-v1"],
                    ["new-v1"],
                    evidence="operator accepted mind-change",
                    old_graph_ids=["old-graph"],
                    new_graph_ids=["new-graph"],
                )

            reconstructed = []
            replayed = event_log.replay(reconstructed.append)

        self.assertEqual(replayed, 1)
        self.assertEqual(reconstructed[0].type, "BRANCH_CREATED")
        self.assertEqual(reconstructed[0].refs["old_tile_id"], "old-v1")
        self.assertEqual(reconstructed[0].refs["new_tile_id"], "new-v1")
        self.assertEqual(reconstructed[0].payload["old_tile_ids"], ["old-v1"])
        self.assertEqual(reconstructed[0].payload["new_tile_ids"], ["new-v1"])
        self.assertEqual(reconstructed[0].payload["old_graph_ids"], ["old-graph"])
        self.assertEqual(reconstructed[0].payload["new_graph_ids"], ["new-graph"])
        self.assertEqual(reconstructed[0].payload["evidence"], "operator accepted mind-change")
        self.assertEqual(reconstructed[0].payload["correction_status"], "revised")
        self.assertIn("provenance_hash", reconstructed[0].payload)

    def test_mark_contested_emits_contradiction_detected_and_replay_reconstructs_receipt(self):
        core = object.__new__(ISMACore)
        store = Mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            event_log = EventLog(str(Path(tmpdir) / "events.jsonl"))

            with patch.object(
                ISMACore,
                "_read_isma_quantum_tile_properties",
                return_value={"is_superseded": False, "correction_status": "current"},
            ) as read_tile, patch.object(ISMACore, "_patch_isma_quantum_tile"), patch(
                "isma.src.hmm.neo4j_store.HMMNeo4jStore",
                return_value=store,
            ), patch("isma.src.hmm.eventlog.EventLog", return_value=event_log):
                core.mark_contested(
                    "tile-a",
                    "tile-b",
                    confidence=0.91,
                    resolution="needs operator adjudication",
                    detected_by="gate_b",
                    graph_tile_a_id="graph-a",
                    graph_tile_b_id="graph-b",
                )

            read_tile.assert_has_calls(
                [
                    call("tile-a", "mark_contested"),
                    call("tile-b", "mark_contested"),
                ]
            )
            reconstructed = []
            replayed = event_log.replay(reconstructed.append)

        self.assertEqual(replayed, 1)
        self.assertEqual(reconstructed[0].type, "CONTRADICTION_DETECTED")
        self.assertEqual(reconstructed[0].refs["tile_a_id"], "tile-a")
        self.assertEqual(reconstructed[0].refs["tile_b_id"], "tile-b")
        self.assertEqual(reconstructed[0].payload["tile_a_id"], "tile-a")
        self.assertEqual(reconstructed[0].payload["tile_b_id"], "tile-b")
        self.assertEqual(reconstructed[0].payload["graph_tile_a_id"], "graph-a")
        self.assertEqual(reconstructed[0].payload["graph_tile_b_id"], "graph-b")
        self.assertEqual(reconstructed[0].payload["confidence"], 0.91)
        self.assertEqual(reconstructed[0].payload["resolution"], "needs operator adjudication")
        self.assertEqual(reconstructed[0].payload["detected_by"], "gate_b")
        self.assertEqual(reconstructed[0].payload["correction_status"], "contested")
        self.assertIn("provenance_hash", reconstructed[0].payload)

    def test_mark_revised_refuses_corrected_tile_before_unsupersede(self):
        core = object.__new__(ISMACore)
        event_log = Mock()

        with patch.object(
            ISMACore,
            "_read_isma_quantum_tile_properties",
            return_value={
                "is_superseded": True,
                "correction_status": "corrected",
            },
        ) as read_tile, patch.object(
            ISMACore, "_patch_isma_quantum_tile"
        ) as patch_tile, patch(
            "isma.src.hmm.eventlog.EventLog",
            return_value=event_log,
        ):
            with self.assertRaisesRegex(ValueError, "refuses to clear supersede state"):
                core.mark_revised(
                    ["old-v1"],
                    ["new-v1"],
                    evidence="weaker transition must not resurrect",
                    old_graph_ids=["old-graph"],
                    new_graph_ids=["new-graph"],
                )

        read_tile.assert_called_once_with("old-v1", "mark_revised")
        patch_tile.assert_not_called()
        event_log.emit.assert_not_called()

    def test_mark_contested_refuses_corrected_tile_before_unsupersede(self):
        core = object.__new__(ISMACore)
        event_log = Mock()

        with patch.object(
            ISMACore,
            "_read_isma_quantum_tile_properties",
            return_value={
                "is_superseded": True,
                "correction_status": "corrected",
            },
        ) as read_tile, patch.object(
            ISMACore, "_patch_isma_quantum_tile"
        ) as patch_tile, patch(
            "isma.src.hmm.eventlog.EventLog",
            return_value=event_log,
        ):
            with self.assertRaisesRegex(ValueError, "refuses to clear supersede state"):
                core.mark_contested(
                    "tile-a",
                    "tile-b",
                    confidence=0.91,
                    detected_by="automation",
                    graph_tile_a_id="graph-a",
                    graph_tile_b_id="graph-b",
                )

        read_tile.assert_called_once_with("tile-a", "mark_contested")
        patch_tile.assert_not_called()
        event_log.emit.assert_not_called()

    def test_mark_revised_refuses_legacy_superseded_by_before_unsupersede(self):
        core = object.__new__(ISMACore)
        event_log = Mock()

        with patch.object(
            ISMACore,
            "_read_isma_quantum_tile_properties",
            return_value={
                "is_superseded": False,
                "correction_status": "current",
                "superseded_by": "new-v2",
                "invalidated_at": "",
            },
        ) as read_tile, patch.object(
            ISMACore, "_patch_isma_quantum_tile"
        ) as patch_tile, patch(
            "isma.src.hmm.eventlog.EventLog",
            return_value=event_log,
        ):
            with self.assertRaisesRegex(ValueError, "refuses to clear supersede state"):
                core.mark_revised(
                    ["old-v1"],
                    ["new-v1"],
                    evidence="legacy superseded_by must stay corrected",
                    old_graph_ids=["old-graph"],
                    new_graph_ids=["new-graph"],
                )

        read_tile.assert_called_once_with("old-v1", "mark_revised")
        patch_tile.assert_not_called()
        event_log.emit.assert_not_called()

    def test_mark_contested_refuses_legacy_invalidated_at_before_unsupersede(self):
        core = object.__new__(ISMACore)
        event_log = Mock()

        with patch.object(
            ISMACore,
            "_read_isma_quantum_tile_properties",
            return_value={
                "is_superseded": False,
                "correction_status": "current",
                "superseded_by": "",
                "invalidated_at": "2026-07-04T00:00:00Z",
            },
        ) as read_tile, patch.object(
            ISMACore, "_patch_isma_quantum_tile"
        ) as patch_tile, patch(
            "isma.src.hmm.eventlog.EventLog",
            return_value=event_log,
        ):
            with self.assertRaisesRegex(ValueError, "refuses to clear supersede state"):
                core.mark_contested(
                    "tile-a",
                    "tile-b",
                    confidence=0.91,
                    detected_by="automation",
                    graph_tile_a_id="graph-a",
                    graph_tile_b_id="graph-b",
                )

        read_tile.assert_called_once_with("tile-a", "mark_contested")
        patch_tile.assert_not_called()
        event_log.emit.assert_not_called()

    def test_mark_revised_graph_false_raises_without_receipt(self):
        core = object.__new__(ISMACore)
        store = Mock()
        store.mark_revised.return_value = False
        event_log = Mock()

        with patch.object(
            ISMACore,
            "_read_isma_quantum_tile_properties",
            return_value={"is_superseded": False, "correction_status": "current"},
        ), patch.object(ISMACore, "_patch_isma_quantum_tile") as patch_tile, patch(
            "isma.src.hmm.neo4j_store.HMMNeo4jStore",
            return_value=store,
        ), patch(
            "isma.src.hmm.eventlog.EventLog",
            return_value=event_log,
        ):
            with self.assertRaisesRegex(RuntimeError, "REVISES edge not created"):
                core.mark_revised(
                    ["old-v1"],
                    ["new-v1"],
                    evidence="graph mutation must be durable",
                    old_graph_ids=["old-graph"],
                    new_graph_ids=["new-graph"],
                )

        patch_tile.assert_not_called()
        store.mark_revised.assert_called_once_with(
            "old-graph",
            "new-graph",
            evidence="graph mutation must be durable",
        )
        event_log.emit.assert_not_called()

    def test_mark_contested_graph_false_raises_without_receipt(self):
        core = object.__new__(ISMACore)
        store = Mock()
        store.mark_contradiction.return_value = False
        event_log = Mock()

        with patch.object(
            ISMACore,
            "_read_isma_quantum_tile_properties",
            return_value={"is_superseded": False, "correction_status": "current"},
        ), patch.object(ISMACore, "_patch_isma_quantum_tile") as patch_tile, patch(
            "isma.src.hmm.neo4j_store.HMMNeo4jStore",
            return_value=store,
        ), patch(
            "isma.src.hmm.eventlog.EventLog",
            return_value=event_log,
        ):
            with self.assertRaisesRegex(RuntimeError, "CONTRADICTS edge not created"):
                core.mark_contested(
                    "tile-a",
                    "tile-b",
                    confidence=0.91,
                    resolution="needs adjudication",
                    detected_by="automation",
                    graph_tile_a_id="graph-a",
                    graph_tile_b_id="graph-b",
                )

        patch_tile.assert_not_called()
        store.mark_contradiction.assert_called_once_with(
            "graph-a",
            "graph-b",
            confidence=0.91,
            resolution="needs adjudication",
            detected_by="automation",
        )
        event_log.emit.assert_not_called()

    def test_mark_revised_missing_graph_ids_aborts_before_patch(self):
        core = object.__new__(ISMACore)
        event_log = Mock()

        with patch.object(
            ISMACore,
            "_read_isma_quantum_tile_properties",
            return_value={"is_superseded": False, "correction_status": "current"},
        ), patch.object(ISMACore, "_patch_isma_quantum_tile") as patch_tile, patch(
            "isma.src.hmm.eventlog.EventLog",
            return_value=event_log,
        ):
            with self.assertRaisesRegex(ValueError, "requires graph ids"):
                core.mark_revised(
                    ["old-v1"],
                    ["new-v1"],
                    evidence="graph ids must be valid before mutation",
                    old_graph_ids=[""],
                    new_graph_ids=["new-graph"],
                )

        patch_tile.assert_not_called()
        event_log.emit.assert_not_called()

    def test_hard_supersede_without_refuter_is_rejected_fail_loud(self):
        core = object.__new__(ISMACore)

        with patch("isma.src.isma_core.requests.patch") as patch_request:
            with self.assertRaisesRegex(ValueError, "hard supersede requires refuter"):
                core._invalidate_superseded_tiles(
                    ["old-v1"],
                    "new-hash",
                    "2026-07-04T00:00:00Z",
                )

        patch_request.assert_not_called()

    def test_hard_supersede_refuter_rejects_extra_keys_before_patch(self):
        core = object.__new__(ISMACore)
        refuter = {
            "who": "operator",
            "source": "decision-record:abc",
            "when": "2026-07-04T00:00:00Z",
            "payload": "inject me into durable provenance",
        }

        with patch("isma.src.isma_core.requests.patch") as patch_request:
            with self.assertRaisesRegex(ValueError, "unsupported field"):
                core._invalidate_superseded_tiles(
                    ["old-v1"],
                    "new-hash",
                    "2026-07-04T00:00:00Z",
                    refuter=refuter,
                    authenticated_actor="operator",
                )

        patch_request.assert_not_called()

    def test_hard_supersede_patch_failure_does_not_emit_correction_receipt(self):
        core = object.__new__(ISMACore)
        response = Mock()
        response.status_code = 500
        response.text = "write failed"
        event_log = Mock()
        refuter = {
            "who": "operator",
            "source": "decision-record:abc",
            "when": "2026-07-04T00:00:00Z",
        }

        with patch("isma.src.isma_core.requests.patch", return_value=response):
            with patch("isma.src.hmm.eventlog.EventLog", return_value=event_log):
                with self.assertRaisesRegex(RuntimeError, "supersede patch failed"):
                    core._invalidate_superseded_tiles(
                        ["old-v1"],
                        "new-hash",
                        "2026-07-04T00:00:00Z",
                        refuter=refuter,
                        authenticated_actor="operator",
                    )

        event_log.emit.assert_not_called()

    def test_mark_revised_patch_failure_does_not_emit_branch_receipt(self):
        core = object.__new__(ISMACore)
        store = Mock()
        event_log = Mock()

        with patch.object(
            ISMACore,
            "_read_isma_quantum_tile_properties",
            return_value={"is_superseded": False, "correction_status": "current"},
        ), patch.object(
            ISMACore,
            "_patch_isma_quantum_tile",
            side_effect=RuntimeError("patch failed"),
        ), patch(
            "isma.src.hmm.eventlog.EventLog",
            return_value=event_log,
        ), patch(
            "isma.src.hmm.neo4j_store.HMMNeo4jStore",
            return_value=store,
        ):
            with self.assertRaisesRegex(RuntimeError, "patch failed"):
                core.mark_revised(
                    ["old-v1"],
                    ["new-v1"],
                    evidence="patch failure must not be receipted",
                    old_graph_ids=["old-graph"],
                    new_graph_ids=["new-graph"],
                )

        store.mark_revised.assert_called_once_with(
            "old-graph",
            "new-graph",
            evidence="patch failure must not be receipted",
        )
        event_log.emit.assert_not_called()

    def test_mark_contested_patch_failure_does_not_emit_contradiction_receipt(self):
        core = object.__new__(ISMACore)
        store = Mock()
        event_log = Mock()

        with patch.object(
            ISMACore,
            "_read_isma_quantum_tile_properties",
            return_value={"is_superseded": False, "correction_status": "current"},
        ), patch.object(
            ISMACore,
            "_patch_isma_quantum_tile",
            side_effect=RuntimeError("patch failed"),
        ), patch(
            "isma.src.hmm.eventlog.EventLog",
            return_value=event_log,
        ), patch(
            "isma.src.hmm.neo4j_store.HMMNeo4jStore",
            return_value=store,
        ):
            with self.assertRaisesRegex(RuntimeError, "patch failed"):
                core.mark_contested(
                    "tile-a",
                    "tile-b",
                    confidence=0.91,
                    detected_by="automation",
                    graph_tile_a_id="graph-a",
                    graph_tile_b_id="graph-b",
                )

        store.mark_contradiction.assert_called_once_with(
            "graph-a",
            "graph-b",
            confidence=0.91,
            resolution="",
            detected_by="automation",
        )
        event_log.emit.assert_not_called()

    def test_corrected_write_without_refuter_aborts_before_tile_write(self):
        core = object.__new__(ISMACore)
        event = Mock()
        event.data = {
            "content": "new claim",
            "content_hash": "claim-hash",
            "lineage_root": "claim-root",
            "correction_status": "corrected",
        }
        event.event_type = "operator_assertion"

        with patch("isma.src.isma_core.requests.post") as post_request:
            with self.assertRaisesRegex(ValueError, "hard supersede requires refuter"):
                core._embed_to_weaviate(event)

        post_request.assert_not_called()

    def test_hard_supersede_with_refuter_patches_and_logs_correction(self):
        core = object.__new__(ISMACore)
        response = Mock()
        response.status_code = 204
        event_log = Mock()
        refuter = {
            "who": "operator",
            "source": "decision-record:abc",
            "when": "2026-07-04T00:00:00Z",
        }

        with patch("isma.src.isma_core.requests.patch", return_value=response) as patch_request:
            with patch("isma.src.hmm.eventlog.EventLog", return_value=event_log):
                core._invalidate_superseded_tiles(
                    ["old-v1"],
                    "new-hash",
                    "2026-07-04T00:00:00Z",
                    refuter=refuter,
                    authenticated_actor="operator",
                )

        patch_body = patch_request.call_args.kwargs["json"]["properties"]
        self.assertIs(patch_body["is_superseded"], True)
        self.assertEqual(patch_body["correction_status"], "corrected")
        self.assertEqual(patch_body["superseded_by"], "new-hash")
        self.assertEqual(patch_body["invalidated_at"], "2026-07-04T00:00:00Z")

        provenance = json.loads(patch_body["provenance_hash"])
        self.assertEqual(provenance["event_type"], "CORRECTION")
        self.assertEqual(provenance["action"], "hard_supersede")
        self.assertEqual(provenance["old_tile_id"], "old-v1")
        self.assertEqual(provenance["superseded_by"], "new-hash")
        self.assertEqual(provenance["refuter"]["who"], refuter["who"])
        self.assertEqual(provenance["refuter"]["source"], refuter["source"])
        self.assertEqual(provenance["refuter"]["when"], refuter["when"])
        self.assertEqual(set(provenance["refuter"]), {"who", "source", "when"})

        event_log.emit.assert_called_once()
        event_type, = event_log.emit.call_args.args
        self.assertEqual(event_type, "CORRECTION")
        refs = event_log.emit.call_args.kwargs["refs"]
        payload = event_log.emit.call_args.kwargs["payload"]
        self.assertEqual(refs["old_tile_id"], "old-v1")
        self.assertEqual(refs["superseded_by"], "new-hash")
        self.assertEqual(payload["correction_status"], "corrected")
        self.assertEqual(payload["refuter"], provenance["refuter"])
        self.assertEqual(payload["provenance_hash"], patch_body["provenance_hash"])

    def test_hard_supersede_refuter_must_match_authenticated_actor(self):
        core = object.__new__(ISMACore)
        refuter = {
            "who": "payload-declared-actor",
            "source": "decision-record:abc",
            "when": "2026-07-04T00:00:00Z",
        }

        with patch("isma.src.isma_core.requests.patch") as patch_request:
            with self.assertRaisesRegex(ValueError, "must match authenticated actor"):
                core._invalidate_superseded_tiles(
                    ["old-v1"],
                    "new-hash",
                    "2026-07-04T00:00:00Z",
                    refuter=refuter,
                    authenticated_actor="authenticated-actor",
                )

        patch_request.assert_not_called()

    def test_neo4j_mark_revised_missing_nodes_degrades_without_mutation(self):
        store = object.__new__(HMMNeo4jStore)
        session = Mock()
        result = Mock()
        result.single.return_value = {
            "new_exists": False,
            "old_exists": False,
            "new_is_superseded": False,
            "old_is_superseded": False,
            "new_correction_status": "",
            "old_correction_status": "",
        }
        session.run.return_value = result
        store.driver = MagicMock()
        store.driver.session.return_value.__enter__.return_value = session

        self.assertIs(store.mark_revised("old-graph", "new-graph"), False)
        self.assertEqual(session.run.call_count, 1)

    def test_neo4j_mark_contradiction_missing_nodes_degrades(self):
        store = object.__new__(HMMNeo4jStore)
        session = Mock()
        result = Mock()
        result.single.return_value = {"contradicted": 0}
        session.run.return_value = result
        store.driver = MagicMock()
        store.driver.session.return_value.__enter__.return_value = session

        self.assertIs(
            store.mark_contradiction(
                "tile-a",
                "tile-b",
                confidence=0.5,
                resolution="missing graph nodes",
                detected_by="test",
            ),
            False,
        )


if __name__ == "__main__":
    unittest.main()
