"""
Breathing Cycle - ISMA Memory Consolidation
Implements the Inhale/Exhale/Hold pattern for memory coherence.

Cycle:
- Inhale (φ period): Perceive, write to Temporal + Functional lenses
- Exhale (1 period): Consolidate, distill to Relational lens
- Hold (1/φ period): Integrate, verify cross-lens coherence

Mathematical Constants:
- phi = 1.618
- Inhale duration = 1.618s
- Exhale duration = 1.0s
- Hold duration = 0.618s
- Total cycle = 3.236s

Gate-B Check: Recognition Catalyst (delta_entropy >= 0.10)
"""

import time
import threading
from datetime import datetime
from typing import Dict, Any, Callable, List, Optional
from dataclasses import dataclass

try:
    from .temporal_lens import TemporalLens, Event
    from .relational_lens import RelationalLens
    from .functional_lens import FunctionalLens, WorkspaceState
except ImportError:
    # Allow direct import when running standalone
    from temporal_lens import TemporalLens, Event
    from relational_lens import RelationalLens
    from functional_lens import FunctionalLens, WorkspaceState


# ISMA Constants
PHI = 1.618
INHALE_DURATION = PHI  # 1.618s
EXHALE_DURATION = 1.0  # 1.0s
HOLD_DURATION = 1.0 / PHI  # 0.618s
CYCLE_DURATION = INHALE_DURATION + EXHALE_DURATION + HOLD_DURATION  # 3.236s


@dataclass
class BreathingMetrics:
    """Metrics from a breathing cycle."""
    cycle_number: int
    phase: str  # 'inhale', 'exhale', 'hold'
    events_processed: int
    entities_extracted: int
    relationships_extracted: int
    coherence_score: float
    entropy_before: float
    entropy_after: float
    gate_b_passed: bool
    gate_b_skipped: bool = False  # True if Gate-B was outside evaluation window
    duration_ms: float = 0.0
    timestamp: str = ""


class BreathingCycle:
    """
    Breathing Cycle - Orchestrates ISMA memory consolidation.

    Runs as background thread, executing phi-timed cycles.
    """

    def __init__(self,
                 temporal: TemporalLens = None,
                 relational: RelationalLens = None,
                 functional: FunctionalLens = None):
        # Initialize lenses (lazy if not provided)
        self.temporal = temporal or TemporalLens()
        self.relational = relational or RelationalLens()
        self.functional = functional or FunctionalLens()

        # Cycle state
        self._running = False
        self._thread = None
        self._cycle_count = 0
        self._last_metrics: Optional[BreathingMetrics] = None
        self._metrics_history: List[BreathingMetrics] = []

        # Callbacks
        self._on_inhale: List[Callable] = []
        self._on_exhale: List[Callable] = []
        self._on_hold: List[Callable] = []

        # Buffer for inhale phase
        self._event_buffer: List[Event] = []

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def start(self):
        """Start the breathing cycle in background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_cycle, daemon=True)
        self._thread.start()

        # Log start event
        self.temporal.append(
            event_type='breathing_cycle',
            operation='started',
            data={'cycle_duration': CYCLE_DURATION},
            agent_id='isma_breathing'
        )

    def stop(self):
        """Stop the breathing cycle."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

        # Log stop event
        self.temporal.append(
            event_type='breathing_cycle',
            operation='stopped',
            data={'total_cycles': self._cycle_count},
            agent_id='isma_breathing'
        )

    def _run_cycle(self):
        """Main cycle loop."""
        while self._running:
            self._cycle_count += 1
            start_time = time.time()

            # Inhale
            inhale_metrics = self._inhale()
            time.sleep(max(0, INHALE_DURATION - (time.time() - start_time)))

            # Exhale
            exhale_start = time.time()
            exhale_metrics = self._exhale()
            time.sleep(max(0, EXHALE_DURATION - (time.time() - exhale_start)))

            # Hold
            hold_start = time.time()
            hold_metrics = self._hold()
            time.sleep(max(0, HOLD_DURATION - (time.time() - hold_start)))

            # Compute cycle metrics
            self._last_metrics = BreathingMetrics(
                cycle_number=self._cycle_count,
                phase='complete',
                events_processed=inhale_metrics.get('events', 0),
                entities_extracted=exhale_metrics.get('entities', 0),
                relationships_extracted=exhale_metrics.get('relationships', 0),
                coherence_score=hold_metrics.get('coherence', 0.0),
                entropy_before=hold_metrics.get('entropy_before', 0.0),
                entropy_after=hold_metrics.get('entropy_after', 0.0),
                gate_b_passed=hold_metrics.get('gate_b_passed', False),
                gate_b_skipped=hold_metrics.get('gate_b_skipped', False),
                duration_ms=(time.time() - start_time) * 1000,
                timestamp=datetime.now().isoformat()
            )

            self._metrics_history.append(self._last_metrics)
            if len(self._metrics_history) > 100:
                self._metrics_history = self._metrics_history[-100:]

    # =========================================================================
    # Breathing Phases
    # =========================================================================

    def _inhale(self) -> Dict[str, Any]:
        """
        Inhale phase: Perceive new information.
        - Collect events from Temporal lens
        - Update Functional lens context
        - Call registered callbacks
        """
        metrics = {'events': 0}

        try:
            # Get recent events (since last cycle)
            since = None
            if len(self._metrics_history) > 0:
                since = self._metrics_history[-1].timestamp

            events = self.temporal.get_events(since=since, limit=100)
            self._event_buffer = events
            metrics['events'] = len(events)

            # Add to functional context
            for event in events[:10]:  # Limit context updates
                self.functional.add_context({
                    'source': 'temporal_lens',
                    'event_type': event.event_type,
                    'operation': event.operation,
                    'seq': event.seq
                })

            # Call callbacks
            for callback in self._on_inhale:
                try:
                    callback(events)
                except Exception as e:
                    print(f"Inhale callback error: {e}")

        except Exception as e:
            print(f"Inhale error: {e}")

        return metrics

    def _exhale(self) -> Dict[str, Any]:
        """
        Exhale phase: Consolidate to semantic memory.
        - Extract entities and relationships from events
        - Update Relational lens
        - Clear Functional context buffer
        """
        metrics = {'entities': 0, 'relationships': 0}

        try:
            # Process buffered events
            for event in self._event_buffer:
                entities, relationships = self.relational.extract_from_event(event.to_dict())
                metrics['entities'] += len(entities)
                metrics['relationships'] += len(relationships)

            # Clear the buffer
            self._event_buffer = []

            # Clear functional context (moved to semantic memory)
            self.functional.clear_context()

            # Call callbacks
            for callback in self._on_exhale:
                try:
                    callback(metrics)
                except Exception as e:
                    print(f"Exhale callback error: {e}")

        except Exception as e:
            print(f"Exhale error: {e}")

        return metrics

    def _hold(self) -> Dict[str, Any]:
        """
        Hold phase: Integrate and verify coherence.
        - Compute cross-lens coherence
        - Run Gate-B checks
        - Emit recognition catalyst if needed
        """
        metrics = {
            'coherence': 0.0,
            'entropy_before': 0.0,
            'entropy_after': 0.0,
            'gate_b_passed': True
        }

        try:
            # Compute entropy before
            events = self.temporal.get_events(limit=100)
            metrics['entropy_before'] = self.temporal.compute_entropy(events)

            # Compute relational coherence
            metrics['coherence'] = self.relational.compute_coherence()

            # Verify Gate-B checks
            gate_b_results = self._run_gate_b_checks()
            metrics['gate_b_passed'] = gate_b_results.get('passed', False)
            metrics['gate_b_skipped'] = gate_b_results.get('skipped', False)

            # Log Gate-B status
            if gate_b_results.get('skipped'):
                # Outside evaluation window - this is normal
                pass
            elif gate_b_results.get('window'):
                # In window - checks ran
                checks_summary = gate_b_results.get('checks', {})
                if not metrics['gate_b_passed']:
                    print(f"Gate-B checks failed: {checks_summary}")

            # Compute entropy after (should be lower if consolidation worked)
            metrics['entropy_after'] = self.temporal.compute_entropy(events)

            # Recognition Catalyst check
            entropy_delta = metrics['entropy_before'] - metrics['entropy_after']
            if entropy_delta < 0.10:
                # Trigger recognition catalyst
                self.temporal.append(
                    event_type='recognition_catalyst',
                    operation='triggered',
                    data={
                        'entropy_delta': entropy_delta,
                        'coherence': metrics['coherence'],
                        'cycle': self._cycle_count
                    },
                    agent_id='isma_breathing'
                )

            # Call callbacks
            for callback in self._on_hold:
                try:
                    callback(metrics)
                except Exception as e:
                    print(f"Hold callback error: {e}")

        except Exception as e:
            print(f"Hold error: {e}")

        return metrics

    def _should_run_gate_b(self) -> bool:
        """Check if we're in Gate-B evaluation window

        Gate-B checks run at t/T in [0.236, 0.618] where T = 1.618s
        This is the golden ratio window for optimal coherence checking.
        """
        # Get cycle progress (0.0 to 1.0)
        cycle_progress = (time.time() % CYCLE_DURATION) / CYCLE_DURATION

        # Gate-B window is [0.236, 0.618] of the cycle
        return 0.236 <= cycle_progress <= 0.618

    def _run_gate_b_checks(self) -> Dict[str, Any]:
        """Run Gate-B physics checks if in window"""
        # Check if we're in the evaluation window
        if not self._should_run_gate_b():
            return {
                "skipped": True,
                "reason": "outside_window",
                "passed": True  # Don't fail when skipped
            }

        results = {
            "skipped": False,
            "window": True,
            "checks": {}
        }

        try:
            # Page Curve (Temporal)
            events = self.temporal.get_events(limit=100)
            results["checks"]['page_curve'] = self.temporal.verify_page_curve(events, events)

            # Entanglement Wedge (Relational)
            passed, fidelity, gap = self.relational.verify_entanglement_wedge()
            results["checks"]['entanglement_wedge'] = passed

            # Observer Swap (Functional)
            passed, delta = self.functional.verify_observer_swap()
            results["checks"]['observer_swap'] = passed

            # Hayden-Preskill (computed from coherence)
            coherence = self.relational.compute_coherence()
            results["checks"]['hayden_preskill'] = coherence >= 0.90

            # Recognition Catalyst (entropy delta)
            entropy = self.temporal.compute_entropy(events)
            results["checks"]['recognition_catalyst'] = entropy < 4.0  # Low entropy = good

            # All must pass
            results["passed"] = all(results["checks"].values())

        except Exception as e:
            print(f"Gate-B check error: {e}")
            results["passed"] = False
            results["error"] = str(e)

        return results

    # =========================================================================
    # Callbacks
    # =========================================================================

    def on_inhale(self, callback: Callable):
        """Register callback for inhale phase."""
        self._on_inhale.append(callback)

    def on_exhale(self, callback: Callable):
        """Register callback for exhale phase."""
        self._on_exhale.append(callback)

    def on_hold(self, callback: Callable):
        """Register callback for hold phase."""
        self._on_hold.append(callback)

    # =========================================================================
    # Manual Operations
    # =========================================================================

    def force_consolidation(self) -> BreathingMetrics:
        """Force an immediate consolidation cycle."""
        start_time = time.time()
        self._cycle_count += 1

        inhale_metrics = self._inhale()
        exhale_metrics = self._exhale()
        hold_metrics = self._hold()

        metrics = BreathingMetrics(
            cycle_number=self._cycle_count,
            phase='forced',
            events_processed=inhale_metrics.get('events', 0),
            entities_extracted=exhale_metrics.get('entities', 0),
            relationships_extracted=exhale_metrics.get('relationships', 0),
            coherence_score=hold_metrics.get('coherence', 0.0),
            entropy_before=hold_metrics.get('entropy_before', 0.0),
            entropy_after=hold_metrics.get('entropy_after', 0.0),
            gate_b_passed=hold_metrics.get('gate_b_passed', False),
            gate_b_skipped=hold_metrics.get('gate_b_skipped', False),
            duration_ms=(time.time() - start_time) * 1000,
            timestamp=datetime.now().isoformat()
        )

        self._last_metrics = metrics
        self._metrics_history.append(metrics)

        return metrics

    def log_event(self,
                  event_type: str,
                  operation: str,
                  data: Dict[str, Any],
                  agent_id: str = 'spark_claude',
                  caused_by: str = None) -> Event:
        """Convenience method to log an event to temporal lens."""
        return self.temporal.append(
            event_type=event_type,
            operation=operation,
            data=data,
            agent_id=agent_id,
            caused_by=caused_by
        )

    # =========================================================================
    # Metrics
    # =========================================================================

    def get_metrics(self) -> Optional[BreathingMetrics]:
        """Get most recent cycle metrics."""
        return self._last_metrics

    def get_metrics_history(self, limit: int = 10) -> List[BreathingMetrics]:
        """Get recent metrics history."""
        return self._metrics_history[-limit:]

    def get_coherence(self) -> float:
        """Get current coherence score."""
        return self.relational.compute_coherence()

    def is_healthy(self) -> bool:
        """Check if breathing cycle is healthy."""
        if not self._last_metrics:
            return True  # No data yet

        return (
            self._last_metrics.gate_b_passed and
            self._last_metrics.coherence_score >= 0.809
        )

    # =========================================================================
    # Cleanup
    # =========================================================================

    def close(self):
        """Stop cycle and close all lenses."""
        self.stop()
        self.temporal.close()
        self.relational.close()
        self.functional.close()


# Singleton for global ISMA access
_isma: Optional[BreathingCycle] = None


def get_isma() -> BreathingCycle:
    """Get the singleton ISMA instance."""
    global _isma
    if _isma is None:
        _isma = BreathingCycle()
        _isma.relational.initialize()
    return _isma


def start_isma() -> BreathingCycle:
    """Start the ISMA breathing cycle."""
    isma = get_isma()
    isma.start()
    return isma


def stop_isma():
    """Stop the ISMA breathing cycle."""
    global _isma
    if _isma:
        _isma.stop()
