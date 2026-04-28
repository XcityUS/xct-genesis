"""DirectorQueue — durable in-memory store for signals + DM requests.

The queue does not own dispatch policy. It is a deduplicating, persistable
container the engine writes to and the API serves from.
"""

from __future__ import annotations

import uuid
from typing import Any

from worldseed.engine.director.models import (
    DirectorSignal,
    PendingDMRequest,
    SignalType,
)


class DirectorQueue:
    """In-memory durable queue for director signals and DM requests.

    Dedup contracts:
      urgent: at most one pending signal per (target_agent_id, refs.event_ref).
              Re-queueing while a previous urgent for the same agent + event
              ref is still pending is a no-op.
      checkpoint: at most one pending checkpoint at a time. A new checkpoint
                  with a different reason replaces nothing — the existing
                  pending one stays; the new one is dropped.
      dm_request: never deduped. Every action / consequence DM intent is
                  distinct, even if it arises from the same source_name.
    """

    def __init__(self) -> None:
        self._signals: dict[str, DirectorSignal] = {}
        self._dm_requests: dict[str, PendingDMRequest] = {}
        # Insertion order tracking so peek() returns FIFO.
        self._signal_order: list[str] = []
        self._dm_order: list[str] = []
        # dm_request_id → signal_id index so resolve/fail are O(1).
        self._dm_to_signal: dict[str, str] = {}
        self._pending_dm_count: int = 0

    # ── Signals ─────────────────────────────────────────────────────────

    def enqueue_signal(self, signal: DirectorSignal) -> bool:
        """Add a signal. Returns True if accepted, False if deduped away."""
        if signal.type == "urgent" and self._urgent_dup(signal):
            return False
        if signal.type == "checkpoint" and self._has_pending_checkpoint():
            return False
        self._signals[signal.id] = signal
        self._signal_order.append(signal.id)
        if signal.type == "dm_request":
            req_id = signal.refs.get("dm_request_id")
            if isinstance(req_id, str):
                self._dm_to_signal[req_id] = signal.id
        return True

    def _urgent_dup(self, signal: DirectorSignal) -> bool:
        target = signal.target_agent_id
        ref = signal.refs.get("event_ref")
        for sid in self._signal_order:
            existing = self._signals.get(sid)
            if existing is None:
                continue
            if existing.status != "pending" or existing.type != "urgent":
                continue
            if existing.target_agent_id == target and existing.refs.get("event_ref") == ref:
                return True
        return False

    def _has_pending_checkpoint(self) -> bool:
        for sid in self._signal_order:
            existing = self._signals.get(sid)
            if existing is None:
                continue
            if existing.type == "checkpoint" and existing.status == "pending":
                return True
        return False

    def peek_pending(
        self,
        limit: int | None = None,
        types: list[SignalType] | None = None,
    ) -> list[DirectorSignal]:
        """FIFO-ordered list of pending signals. Does not drain."""
        out: list[DirectorSignal] = []
        for sid in self._signal_order:
            sig = self._signals.get(sid)
            if sig is None or sig.status != "pending":
                continue
            if types is not None and sig.type not in types:
                continue
            out.append(sig)
            if limit is not None and len(out) >= limit:
                break
        return out

    def get_signal(self, signal_id: str) -> DirectorSignal | None:
        return self._signals.get(signal_id)

    def ack_signal(self, signal_id: str) -> bool:
        """Mark urgent or checkpoint as acked. Returns False if missing or wrong type.

        dm_request signals must go through resolve_dm_request, not ack.
        """
        sig = self._signals.get(signal_id)
        if sig is None or sig.status != "pending":
            return False
        if sig.type == "dm_request":
            return False
        sig.status = "acked"
        return True

    # ── DM requests ─────────────────────────────────────────────────────

    def enqueue_dm_request(self, request: PendingDMRequest) -> None:
        self._dm_requests[request.id] = request
        self._dm_order.append(request.id)
        self._pending_dm_count += 1

    def get_dm_request(self, request_id: str) -> PendingDMRequest | None:
        return self._dm_requests.get(request_id)

    def resolve_dm_request(self, request_id: str, result: dict[str, Any]) -> bool:
        req = self._dm_requests.get(request_id)
        if req is None or req.status != "pending":
            return False
        req.status = "resolved"
        req.result = result
        self._pending_dm_count -= 1
        sig = self._find_dm_signal(request_id)
        if sig is not None:
            sig.status = "resolved"
        return True

    def fail_dm_request(self, request_id: str, reason: str) -> bool:
        req = self._dm_requests.get(request_id)
        if req is None or req.status != "pending":
            return False
        req.status = "failed"
        req.result = {"failed": True, "reason": reason}
        self._pending_dm_count -= 1
        sig = self._find_dm_signal(request_id)
        if sig is not None:
            sig.status = "failed"
            sig.reason = reason
        return True

    def pending_dm_count(self) -> int:
        return self._pending_dm_count

    def _find_dm_signal(self, request_id: str) -> DirectorSignal | None:
        sig_id = self._dm_to_signal.get(request_id)
        return self._signals.get(sig_id) if sig_id else None

    # ── Persistence ─────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "signals": [self._signals[sid].to_dict() for sid in self._signal_order],
            "dm_requests": [self._dm_requests[rid].to_dict() for rid in self._dm_order],
        }

    def restore(self, data: dict[str, Any]) -> None:
        self._signals.clear()
        self._dm_requests.clear()
        self._signal_order.clear()
        self._dm_order.clear()
        self._dm_to_signal.clear()
        self._pending_dm_count = 0
        for raw in data.get("signals") or []:
            sig = DirectorSignal.from_dict(raw)
            self._signals[sig.id] = sig
            self._signal_order.append(sig.id)
            if sig.type == "dm_request":
                req_id = sig.refs.get("dm_request_id")
                if isinstance(req_id, str):
                    self._dm_to_signal[req_id] = sig.id
        for raw in data.get("dm_requests") or []:
            req = PendingDMRequest.from_dict(raw)
            self._dm_requests[req.id] = req
            self._dm_order.append(req.id)
            if req.status == "pending":
                self._pending_dm_count += 1


def new_id() -> str:
    """Short stable-format id for signals / DM requests."""
    return uuid.uuid4().hex[:16]
