"""
Functional Lens - "Truth of Action"
Active workspace for ISMA (Global Workspace pattern).

Implements:
- Working memory (current context, goals, plans)
- Redis-backed state for fast access
- Blackboard pattern for agent coordination
- MCP tool integration

Gate-B Check: Observer Swap (delta <= 0.02)
"""

import json
import hashlib
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, asdict
import redis
from isma.config import REDIS_HOST as CONFIG_REDIS_HOST, REDIS_PORT as CONFIG_REDIS_PORT


@dataclass
class WorkspaceState:
    """Current workspace state."""
    goal: str
    plan_status: Dict[str, str]
    active_agents: List[str]
    context_buffer: List[Dict[str, Any]]
    last_updated: str
    hash: str = ''

    def __post_init__(self):
        if not self.hash:
            self.hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """Compute hash for state comparison."""
        content = json.dumps({
            'goal': self.goal,
            'plan_status': self.plan_status,
            'active_agents': self.active_agents,
            'context_buffer': self.context_buffer[:5]  # Only hash recent context
        }, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


class FunctionalLens:
    """
    Functional Lens - Active Workspace.

    Uses Redis for fast state access.
    Implements Blackboard pattern.
    Integrates with MCP tools.
    """

    def __init__(self,
                 redis_host: str = CONFIG_REDIS_HOST,
                 redis_port: int = CONFIG_REDIS_PORT,
                 redis_db: int = 1):  # Separate DB for ISMA
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_db = redis_db
        self._client = None

        # Workspace keys
        self.KEY_STATE = 'isma:workspace:state'
        self.KEY_GOAL = 'isma:workspace:goal'
        self.KEY_PLAN = 'isma:workspace:plan'
        self.KEY_AGENTS = 'isma:workspace:agents'
        self.KEY_CONTEXT = 'isma:workspace:context'
        self.KEY_BROADCAST = 'isma:broadcast'
        self.KEY_STATE_HISTORY = 'isma:workspace:history'

    def _get_client(self) -> redis.Redis:
        """Lazy Redis client initialization."""
        if self._client is None:
            self._client = redis.Redis(
                host=self.redis_host,
                port=self.redis_port,
                db=self.redis_db,
                decode_responses=True
            )
        return self._client

    # =========================================================================
    # Workspace State
    # =========================================================================

    def get_state(self) -> Optional[WorkspaceState]:
        """Get current workspace state."""
        try:
            client = self._get_client()
            state_json = client.get(self.KEY_STATE)
            if state_json:
                data = json.loads(state_json)
                return WorkspaceState(**data)
        except Exception as e:
            print(f"State get failed: {e}")
        return None

    def set_state(self, state: WorkspaceState) -> bool:
        """Set workspace state (with history tracking)."""
        try:
            client = self._get_client()

            # Save current state to history before overwriting
            current = client.get(self.KEY_STATE)
            if current:
                client.lpush(self.KEY_STATE_HISTORY, current)
                client.ltrim(self.KEY_STATE_HISTORY, 0, 99)  # Keep last 100

            # Set new state
            state.last_updated = datetime.now().isoformat()
            state.hash = state._compute_hash()
            client.set(self.KEY_STATE, json.dumps(asdict(state)))
            return True
        except Exception as e:
            print(f"State set failed: {e}")
        return False

    def update_goal(self, goal: str) -> bool:
        """Update the current goal."""
        try:
            client = self._get_client()
            state = self.get_state() or WorkspaceState(
                goal='',
                plan_status={},
                active_agents=[],
                context_buffer=[],
                last_updated=datetime.now().isoformat()
            )
            state.goal = goal
            return self.set_state(state)
        except Exception as e:
            print(f"Goal update failed: {e}")
        return False

    def update_plan_status(self, step: str, status: str) -> bool:
        """Update plan step status."""
        try:
            state = self.get_state() or WorkspaceState(
                goal='',
                plan_status={},
                active_agents=[],
                context_buffer=[],
                last_updated=datetime.now().isoformat()
            )
            state.plan_status[step] = status
            return self.set_state(state)
        except Exception as e:
            print(f"Plan update failed: {e}")
        return False

    # =========================================================================
    # Agent Coordination (Blackboard Pattern)
    # =========================================================================

    def register_agent(self, agent_id: str, role: str = None) -> bool:
        """Register an agent in the workspace."""
        try:
            client = self._get_client()
            agent_data = json.dumps({
                'agent_id': agent_id,
                'role': role,
                'registered_at': datetime.now().isoformat(),
                'last_active': datetime.now().isoformat()
            })
            client.hset(self.KEY_AGENTS, agent_id, agent_data)
            return True
        except Exception as e:
            print(f"Agent register failed: {e}")
        return False

    def update_agent_activity(self, agent_id: str) -> bool:
        """Update agent's last activity timestamp."""
        try:
            client = self._get_client()
            agent_json = client.hget(self.KEY_AGENTS, agent_id)
            if agent_json:
                data = json.loads(agent_json)
                data['last_active'] = datetime.now().isoformat()
                client.hset(self.KEY_AGENTS, agent_id, json.dumps(data))
                return True
        except Exception as e:
            print(f"Agent activity update failed: {e}")
        return False

    def get_active_agents(self, max_inactive_seconds: int = 60) -> List[Dict[str, Any]]:
        """Get list of active agents."""
        try:
            client = self._get_client()
            all_agents = client.hgetall(self.KEY_AGENTS)

            from datetime import datetime, timedelta
            cutoff = datetime.now() - timedelta(seconds=max_inactive_seconds)

            active = []
            for agent_id, agent_json in all_agents.items():
                data = json.loads(agent_json)
                last_active = datetime.fromisoformat(data['last_active'])
                if last_active > cutoff:
                    active.append(data)

            return active
        except Exception as e:
            print(f"Active agents query failed: {e}")
        return []

    def unregister_agent(self, agent_id: str) -> bool:
        """Unregister an agent."""
        try:
            client = self._get_client()
            client.hdel(self.KEY_AGENTS, agent_id)
            return True
        except Exception as e:
            print(f"Agent unregister failed: {e}")
        return False

    # =========================================================================
    # Context Buffer
    # =========================================================================

    def add_context(self, context: Dict[str, Any]) -> bool:
        """Add context to the buffer (for orchestrator attention)."""
        try:
            client = self._get_client()
            context['added_at'] = datetime.now().isoformat()
            client.lpush(self.KEY_CONTEXT, json.dumps(context))
            client.ltrim(self.KEY_CONTEXT, 0, 49)  # Keep last 50
            return True
        except Exception as e:
            print(f"Context add failed: {e}")
        return False

    def get_context(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent context from buffer."""
        try:
            client = self._get_client()
            items = client.lrange(self.KEY_CONTEXT, 0, limit - 1)
            return [json.loads(item) for item in items]
        except Exception as e:
            print(f"Context get failed: {e}")
        return []

    def clear_context(self) -> bool:
        """Clear the context buffer (after consolidation)."""
        try:
            client = self._get_client()
            client.delete(self.KEY_CONTEXT)
            return True
        except Exception as e:
            print(f"Context clear failed: {e}")
        return False

    # =========================================================================
    # Broadcast (Pub/Sub for agent coordination)
    # =========================================================================

    def broadcast(self, message_type: str, data: Dict[str, Any]) -> bool:
        """Broadcast a message to all agents."""
        try:
            client = self._get_client()
            message = json.dumps({
                'type': message_type,
                'data': data,
                'timestamp': datetime.now().isoformat()
            })
            client.publish(self.KEY_BROADCAST, message)
            return True
        except Exception as e:
            print(f"Broadcast failed: {e}")
        return False

    def subscribe(self, callback):
        """Subscribe to workspace broadcasts."""
        try:
            client = self._get_client()
            pubsub = client.pubsub()
            pubsub.subscribe(self.KEY_BROADCAST)

            for message in pubsub.listen():
                if message['type'] == 'message':
                    data = json.loads(message['data'])
                    callback(data)
        except Exception as e:
            print(f"Subscribe failed: {e}")

    # =========================================================================
    # Gate-B Check: Observer Swap
    # =========================================================================

    def compute_state_fidelity(self, state1: WorkspaceState, state2: WorkspaceState) -> float:
        """
        Compute fidelity between two workspace states.
        Used for Observer Swap check.

        Returns:
            Fidelity score (0-1)
        """
        if not state1 or not state2:
            return 0.0

        # Compare goals
        goal_match = 1.0 if state1.goal == state2.goal else 0.0

        # Compare plan status
        all_steps = set(state1.plan_status.keys()) | set(state2.plan_status.keys())
        if all_steps:
            matching = sum(1 for s in all_steps
                         if state1.plan_status.get(s) == state2.plan_status.get(s))
            plan_match = matching / len(all_steps)
        else:
            plan_match = 1.0

        # Compare active agents
        agents1 = set(state1.active_agents)
        agents2 = set(state2.active_agents)
        if agents1 | agents2:
            agent_match = len(agents1 & agents2) / len(agents1 | agents2)
        else:
            agent_match = 1.0

        # Weighted average
        fidelity = (0.4 * goal_match) + (0.4 * plan_match) + (0.2 * agent_match)
        return fidelity

    def verify_observer_swap(self, other_state: WorkspaceState = None) -> Tuple[bool, float]:
        """
        Verify Observer Swap Gate-B check.
        Checks if state is stable under observation swap.

        Args:
            other_state: State from another observer to compare

        Returns:
            Tuple of (passed, delta)
        """
        current = self.get_state()

        if other_state:
            fidelity = self.compute_state_fidelity(current, other_state)
        else:
            # Compare with most recent historical state
            try:
                client = self._get_client()
                history = client.lrange(self.KEY_STATE_HISTORY, 0, 0)
                if history:
                    prev = WorkspaceState(**json.loads(history[0]))
                    fidelity = self.compute_state_fidelity(current, prev)
                else:
                    fidelity = 1.0  # No history, assume stable
            except (redis.RedisError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise RuntimeError("failed to load workspace state history") from exc

        delta = 1.0 - fidelity if fidelity else 1.0
        passed = delta <= 0.02

        return passed, delta

    # =========================================================================
    # Cleanup
    # =========================================================================

    def clear_workspace(self) -> bool:
        """Clear the entire workspace."""
        try:
            client = self._get_client()
            client.delete(self.KEY_STATE, self.KEY_GOAL, self.KEY_PLAN,
                         self.KEY_AGENTS, self.KEY_CONTEXT)
            return True
        except Exception as e:
            print(f"Workspace clear failed: {e}")
        return False

    def close(self):
        """Close Redis connection."""
        if self._client:
            self._client.close()
            self._client = None
