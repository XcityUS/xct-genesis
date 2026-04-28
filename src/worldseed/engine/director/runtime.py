"""DirectorRuntime — engine-facing facade for the director-signal layer.

Hosts the queue, checkpoint state, and config knobs. WorldEngine owns one
instance and routes attention through it. Disabled by default — when
SceneConfig.director is omitted, every operation here is a cheap no-op.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from worldseed.engine.director.checkpoint import evaluate as evaluate_checkpoint
from worldseed.engine.director.models import (
    CheckpointPolicy,
    CheckpointState,
    DirectorSignal,
    DMSourceType,
    PendingDMRequest,
    SignalType,
)
from worldseed.engine.director.queue import DirectorQueue, new_id

if TYPE_CHECKING:
    from worldseed.engine.event_log import EventLog
    from worldseed.engine.inbox import InboxManager
    from worldseed.engine.wakeup import WakeupResult
    from worldseed.models.config_schema import DirectorConfig


class DirectorRuntime:
    """Owns the signal queue and decides when to enqueue urgent/checkpoint.

    The runtime is always constructed; `enabled` flips behavior wholesale.
    Disabled = transparent: every enqueue is dropped, observe is a no-op,
    peek returns []. This is the off-switch that keeps existing scenes
    byte-identical when `director:` is absent in YAML.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        dm_mode: str = "internal",
        max_pending_dm: int = 64,
        checkpoint_policy: CheckpointPolicy | None = None,
    ) -> None:
        self.enabled = enabled
        self.dm_mode = dm_mode
        self.max_pending_dm = max_pending_dm
        self.checkpoint_policy = checkpoint_policy or CheckpointPolicy()
        self._queue = DirectorQueue()
        self._checkpoint_state = CheckpointState()

    def is_signal_mode(self) -> bool:
        """True when DM should route through the director queue, not the provider."""
        return self.enabled and self.dm_mode == "signal"

    @classmethod
    def from_config(cls, config: DirectorConfig | None) -> DirectorRuntime:
        if config is None or not config.enabled:
            return cls(enabled=False)
        policy = CheckpointPolicy(
            every_events=config.checkpoint.every_events,
            every_minutes=config.checkpoint.every_minutes,
            every_ticks=config.checkpoint.every_ticks,
            on_event_types=list(config.checkpoint.on_event_types),
            ignore_event_scopes=list(config.checkpoint.ignore_event_scopes),
            ignore_event_types=list(config.checkpoint.ignore_event_types),
        )
        return cls(
            enabled=True,
            dm_mode=config.dm_mode,
            max_pending_dm=config.max_pending_dm,
            checkpoint_policy=policy,
        )

    # ── DM enqueue ──────────────────────────────────────────────────────

    def enqueue_action_dm_request(
        self,
        *,
        action: dict[str, Any],
        dm_config: dict[str, Any],
        ctx: dict[str, Any],
        dm_context: dict[str, Any],
        actor_agent_id: str | None,
        tick: int,
    ) -> str | None:
        """Enqueue an action DM. Returns request id or None if dropped/full."""
        return self._enqueue_dm(
            source_type="action",
            source_name=action.get("action_type", ""),
            action=action,
            dm_config=dm_config,
            ctx=ctx,
            dm_context=dm_context,
            actor_agent_id=actor_agent_id,
            tick=tick,
        )

    def enqueue_consequence_dm_request(
        self,
        *,
        consequence_name: str,
        dm_config: dict[str, Any],
        ctx: dict[str, Any],
        dm_context: dict[str, Any],
        tick: int,
    ) -> str | None:
        """Enqueue a consequence DM. Returns request id or None if dropped/full."""
        return self._enqueue_dm(
            source_type="consequence",
            source_name=consequence_name,
            action=None,
            dm_config=dm_config,
            ctx=ctx,
            dm_context=dm_context,
            actor_agent_id=None,
            tick=tick,
        )

    def _enqueue_dm(
        self,
        *,
        source_type: DMSourceType,
        source_name: str,
        action: dict[str, Any] | None,
        dm_config: dict[str, Any],
        ctx: dict[str, Any],
        dm_context: dict[str, Any],
        actor_agent_id: str | None,
        tick: int,
    ) -> str | None:
        if not self.enabled or self.dm_mode != "signal":
            return None
        if self._queue.pending_dm_count() >= self.max_pending_dm:
            return None
        request_id = new_id()
        request = PendingDMRequest(
            id=request_id,
            source_type=source_type,
            source_name=source_name,
            actor_agent_id=actor_agent_id,
            tick=tick,
            dm_config=dm_config,
            action=action,
            ctx=ctx,
            dm_context=dm_context,
        )
        self._queue.enqueue_dm_request(request)
        signal = DirectorSignal(
            id=new_id(),
            type="dm_request",
            tick=tick,
            created_at=time.time(),
            reason=f"{source_type}:{source_name}",
            target_agent_id=actor_agent_id,
            refs={"dm_request_id": request_id},
        )
        self._queue.enqueue_signal(signal)
        return request_id

    # ── Attention observation ───────────────────────────────────────────

    def observe_attention(
        self,
        *,
        tick: int,
        event_log: EventLog,
        inbox_manager: InboxManager,
        wakeup_results: list[WakeupResult],
    ) -> None:
        """Generate urgent + checkpoint signals from current world state.

        Called post state-change in WorldEngine. Mutates queue + state.
        """
        if not self.enabled:
            return

        self._observe_urgent(tick=tick, inbox_manager=inbox_manager, wakeup_results=wakeup_results)
        self._observe_checkpoint(tick=tick, event_log=event_log)

    def _observe_urgent(
        self,
        *,
        tick: int,
        inbox_manager: InboxManager,
        wakeup_results: list[WakeupResult],
    ) -> None:
        for result in wakeup_results:
            if not result.should_wake:
                continue
            inbox = inbox_manager.get_or_create(result.agent_id)
            event_ref = self._latest_push_ref(inbox)
            signal = DirectorSignal(
                id=new_id(),
                type="urgent",
                tick=tick,
                created_at=time.time(),
                reason=result.reason,
                target_agent_id=result.agent_id,
                refs={"event_ref": event_ref} if event_ref is not None else {},
            )
            self._queue.enqueue_signal(signal)

    def _latest_push_ref(self, inbox: Any) -> str | None:
        for ev in reversed(inbox.peek_events()):
            if ev.source != inbox.agent_id and ev.push:
                return f"{ev.tick}:{ev.type}:{ev.source}"
        if inbox.has_whispers():
            return "whisper"
        return None

    def _observe_checkpoint(self, *, tick: int, event_log: EventLog) -> None:
        # Cursor counts events ever appended (survives TTL cleanup). New events
        # are the suffix of the live log corresponding to that delta — capped
        # at len(_events) since older events may have aged out.
        appended_now = event_log.total_appended
        cursor = self._checkpoint_state.last_event_cursor
        delta = max(0, appended_now - cursor)
        live_events = event_log.get_events()
        new_events = live_events[-delta:] if delta else []

        if self._checkpoint_state.last_signal_time == 0.0:
            # Seed the timer on first observe so wall-clock cadence has a baseline.
            self._checkpoint_state.last_signal_time = time.monotonic()

        signal, advanced = evaluate_checkpoint(
            tick=tick,
            monotonic_now=time.monotonic(),
            new_events=new_events,
            policy=self.checkpoint_policy,
            state=self._checkpoint_state,
            new_id=new_id(),
            pending_dm_count=self._queue.pending_dm_count(),
            appended_now=appended_now,
        )
        self._checkpoint_state = advanced
        if signal is not None:
            self._queue.enqueue_signal(signal)

    # ── Queue facade (used by API + WorldEngine facade) ────────────────

    def peek_signals(
        self,
        limit: int | None = None,
        types: list[SignalType] | None = None,
    ) -> list[DirectorSignal]:
        if not self.enabled:
            return []
        return self._queue.peek_pending(limit=limit, types=types)

    def ack_signal(self, signal_id: str) -> bool:
        if not self.enabled:
            return False
        return self._queue.ack_signal(signal_id)

    def get_signal(self, signal_id: str) -> DirectorSignal | None:
        if not self.enabled:
            return None
        return self._queue.get_signal(signal_id)

    def get_dm_request(self, request_id: str) -> PendingDMRequest | None:
        if not self.enabled:
            return None
        return self._queue.get_dm_request(request_id)

    def fail_dm_request(self, request_id: str, reason: str) -> bool:
        if not self.enabled:
            return False
        return self._queue.fail_dm_request(request_id, reason)

    def mark_dm_resolved(self, request_id: str, result: dict[str, Any]) -> bool:
        """Mark a DM request as resolved. Apply effects happens elsewhere
        (in dm_resolver.apply_dm_response); this is the bookkeeping side."""
        if not self.enabled:
            return False
        return self._queue.resolve_dm_request(request_id, result)

    def pending_dm_count(self) -> int:
        return self._queue.pending_dm_count()

    # ── Persistence ────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dm_mode": self.dm_mode,
            "max_pending_dm": self.max_pending_dm,
            "queue": self._queue.to_dict(),
            "checkpoint_state": self._checkpoint_state.to_dict(),
        }

    def restore(self, data: dict[str, Any]) -> None:
        if not data:
            return
        self._queue.restore(data.get("queue") or {})
        cp = data.get("checkpoint_state")
        if cp:
            self._checkpoint_state = CheckpointState.from_dict(cp)
