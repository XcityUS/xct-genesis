"""Director-signal data models.

These dataclasses are pure data: serializable, free of engine side effects.
The engine populates them; an external main agent reads them via API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SignalType = Literal["dm_request", "urgent", "checkpoint"]
SignalStatus = Literal["pending", "acked", "resolved", "failed"]
DMRequestStatus = Literal["pending", "resolved", "failed"]
DMSourceType = Literal["action", "consequence", "gm"]


@dataclass
class DirectorSignal:
    """An attention signal surfaced via the director queue.

    Three kinds:
      dm_request — a PendingDMRequest awaits external resolution.
      urgent     — an agent just became wake-eligible (push event or whisper).
      checkpoint — engine fired a cadence checkpoint; main may want to look.

    refs is a thin pointer (event ids, dm request id, agent id) so the
    payload stays small. Subscribers fetch full data via dedicated GETs.
    """

    id: str
    type: SignalType
    tick: int
    created_at: float
    status: SignalStatus = "pending"
    reason: str = ""
    target_agent_id: str | None = None
    refs: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "tick": self.tick,
            "created_at": self.created_at,
            "status": self.status,
            "reason": self.reason,
            "target_agent_id": self.target_agent_id,
            "refs": dict(self.refs),
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DirectorSignal:
        return cls(
            id=data["id"],
            type=data["type"],
            tick=data["tick"],
            created_at=data["created_at"],
            status=data.get("status", "pending"),
            reason=data.get("reason", ""),
            target_agent_id=data.get("target_agent_id"),
            refs=dict(data.get("refs") or {}),
            payload=dict(data.get("payload") or {}),
        )


@dataclass
class PendingDMRequest:
    """A DM judgment request waiting to be resolved by an external caller.

    The fields mirror RulesEngine's `dm_info` shape (action / consequence)
    so handing one off and resolving it later does not require a separate
    schema. dm_context is the serialized DMContext as a dict — the caller
    inspects it via GET /api/director/dm/{id}.
    """

    id: str
    source_type: DMSourceType
    source_name: str
    actor_agent_id: str | None
    tick: int
    dm_config: dict[str, Any]
    action: dict[str, Any] | None
    ctx: dict[str, Any]
    dm_context: dict[str, Any]
    status: DMRequestStatus = "pending"
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "source_name": self.source_name,
            "actor_agent_id": self.actor_agent_id,
            "tick": self.tick,
            "dm_config": dict(self.dm_config),
            "action": dict(self.action) if self.action else None,
            "ctx": dict(self.ctx),
            "dm_context": dict(self.dm_context),
            "status": self.status,
            "result": dict(self.result) if self.result else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingDMRequest:
        return cls(
            id=data["id"],
            source_type=data["source_type"],
            source_name=data["source_name"],
            actor_agent_id=data.get("actor_agent_id"),
            tick=data["tick"],
            dm_config=dict(data.get("dm_config") or {}),
            action=dict(data["action"]) if data.get("action") else None,
            ctx=dict(data.get("ctx") or {}),
            dm_context=dict(data.get("dm_context") or {}),
            status=data.get("status", "pending"),
            result=dict(data["result"]) if data.get("result") else None,
        )


@dataclass
class CheckpointPolicy:
    """Cadence triggers for checkpoint signals.

    Any of every_events / every_minutes / every_ticks crossing its threshold
    fires one checkpoint. on_event_types is an advisory list — events of
    those types always fire a checkpoint (and reset the counter).

    All thresholds optional so a scene can pick exactly which dimensions
    matter. None on a dimension disables it.
    """

    every_events: int | None = 8
    every_minutes: float | None = 5.0
    every_ticks: int | None = None
    on_event_types: list[str] = field(default_factory=list)
    ignore_event_scopes: list[str] = field(default_factory=lambda: ["admin"])
    ignore_event_types: list[str] = field(default_factory=lambda: ["action_rejected"])

    def is_enabled(self) -> bool:
        return any(v is not None for v in (self.every_events, self.every_minutes, self.every_ticks)) or bool(
            self.on_event_types
        )


@dataclass
class CheckpointState:
    """Mutable counters tracked across ticks for checkpoint cadence."""

    last_signal_tick: int = -1
    last_signal_time: float = 0.0
    last_event_cursor: int = 0  # number of counted events seen so far
    events_since_checkpoint: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_signal_tick": self.last_signal_tick,
            "last_signal_time": self.last_signal_time,
            "last_event_cursor": self.last_event_cursor,
            "events_since_checkpoint": self.events_since_checkpoint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckpointState:
        return cls(
            last_signal_tick=int(data.get("last_signal_tick", -1)),
            last_signal_time=float(data.get("last_signal_time", 0.0)),
            last_event_cursor=int(data.get("last_event_cursor", 0)),
            events_since_checkpoint=int(data.get("events_since_checkpoint", 0)),
        )
