"""WorldEngine — top-level facade for running a world.

Delegates to specialized components:
  StateStore      — entity CRUD
  EventLog        — event storage + TTL
  TickEngine      — tick orchestration
  AgentRegistry   — agent lifecycle + profiles + think_interval
  WakeupEvaluator — notify triggers
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worldseed import action_dispatch
from worldseed import agent_view as agent_view_helpers
from worldseed import narrator as narrator_helpers
from worldseed import transient as transient_helpers
from worldseed.agent_registry import AgentRegistry
from worldseed.engine.action_queue import ActionQueue
from worldseed.engine.director import DirectorRuntime, DirectorSignal, PendingDMRequest
from worldseed.engine.event_log import EventLog
from worldseed.engine.inbox import InboxManager, InboxWhisper
from worldseed.engine.rules_engine import ActionResult
from worldseed.engine.state_store import StateStore
from worldseed.engine.tick import TickEngine
from worldseed.engine.wakeup import WakeupEvaluator, WakeupResult
from worldseed.models.action import ActionSubmission
from worldseed.persistence import NullRecorder
from worldseed.protocol.agent import AgentPerception, build_perception
from worldseed.scene.config import load_config
from worldseed.scene.populator import populate

if TYPE_CHECKING:
    from worldseed.dm.providers.base import DMProvider
    from worldseed.models.config_schema import AgentConfig, SceneConfig
    from worldseed.persistence import RunRecorder


class WorldEngine:
    """Facade: load config, register agents, submit actions, step ticks."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        dm_provider: DMProvider | None = None,
        *,
        config: SceneConfig | None = None,
        recorder: RunRecorder | NullRecorder | None = None,
        language: str = "",
    ) -> None:
        if config is not None:
            self._config = config
        elif config_path is not None:
            self._config = load_config(config_path)
        else:
            msg = "Either config_path or config must be provided"
            raise ValueError(msg)

        self.recorder: RunRecorder | NullRecorder = recorder or NullRecorder()
        self.state = StateStore()
        self.event_log = EventLog()
        self._queue = ActionQueue()
        self._inbox_manager = InboxManager()
        self.registry = AgentRegistry(self._config, self.state)

        self._wakeup = WakeupEvaluator()
        self._director = DirectorRuntime.from_config(self._config.director)

        self._tick_engine = TickEngine(
            self._config,
            self.state,
            self.event_log,
            self._queue,
            inbox_manager=self._inbox_manager,
            dm_provider=dm_provider,
            recorder=self.recorder,
            registry=self.registry,
            director_runtime=self._director,
        )
        self.language = language
        if language:
            self._tick_engine.set_language(language)

        if self._config.narrator:
            self._setup_narrator()

        populate(self._config, self.state)

    # ── Narrator (delegated to worldseed.narrator) ──────────────────

    def _setup_narrator(self) -> None:
        """Register the narrator system agent. Chapters submitted via worldseed_narrate."""
        ncfg = self._config.narrator
        if ncfg is None:
            return

        instructions = narrator_helpers.build_instructions(
            scene_description=self._config.scene.description,
            perception=self._config.perception,
            ncfg=ncfg,
            language=self.language,
        )

        if not self.registry.is_claimed("narrator"):
            self.register_agent(
                agent_id="narrator",
                properties={"chapter_count": 0, "_system": True, "_last_narrate_tick": -1},
                character={"role": "narrator", "instructions": instructions},
                omniscient=True,
                system=True,
                wake_on_push=ncfg.wake_on_push,
            )
            self.registry.update_think_interval("narrator", ncfg.interval)

    def set_narrator_style(self, style: str | None = None, prompt: str | None = None) -> None:
        """Reconfigure narrator style (or custom prompt) on a running world."""
        if prompt:
            style_instruction = prompt
        elif style:
            style_instruction = narrator_helpers.NARRATOR_STYLES.get(style, "")
        else:
            return
        profile = self.registry.get_profile("narrator")
        if profile is None:
            return
        narrator_helpers.replace_style_block(profile, style_instruction)

    def set_language(self, lang: str) -> None:
        """Update language for DM prompts and narrator."""
        self.language = lang
        self._tick_engine.set_language(lang)
        profile = self.registry.get_profile("narrator")
        if profile is not None:
            narrator_helpers.replace_language_line(profile, lang)

    def record_narration(self, params: dict[str, Any]) -> int | str:
        """Record a narrator chapter directly — bypasses action pipeline."""
        return narrator_helpers.apply_narration(
            state=self.state,
            event_log=self.event_log,
            inbox_manager=self._inbox_manager,
            recorder=self.recorder,
            tick=self.tick,
            params=params,
        )

    @property
    def config(self) -> SceneConfig:
        """Scene configuration."""
        return self._config

    def load_stripped_config(self) -> dict[str, Any]:
        """Serialize in-memory config and strip secrets/internals for agents.

        Uses model_dump(exclude_none=True) to serialize the in-memory config,
        then strips engine-internal sections, hidden properties, and metadata flags.
        """
        raw = copy.deepcopy(self._config.model_dump(exclude_none=True))

        hidden = set(self._config.perception.hidden_properties)

        # Keep only agent-visible sections: scene, entities, actions.
        # Everything else is engine internals.
        agent_visible = {"scene", "entities", "actions"}
        for key in list(raw.keys()):
            if key not in agent_visible:
                raw.pop(key)

        # Strip engine-internal scene fields
        scene = raw.get("scene", {})
        if isinstance(scene, dict):
            scene.pop("dm_knowledge", None)
            scene.pop("default_spawn", None)
            scene.pop("max_ticks", None)
            scene.pop("timeout_min", None)
            scene.pop("max_dm_calls", None)
            scene.pop("use", None)

        for entity in raw.get("entities", []):
            props = entity.get("properties", entity)
            for h in hidden:
                props.pop(h, None)

        # Strip engine-only boolean flags from action definitions
        # (push, highlight are engine metadata, not useful to agents)
        _engine_flags = {"push", "highlight"}
        for action_data in raw.get("actions", {}).values():
            if not isinstance(action_data, dict):
                continue
            for event in action_data.get("events", []):
                if isinstance(event, dict):
                    for f in _engine_flags:
                        event.pop(f, None)
            for effect in action_data.get("effects", []):
                if isinstance(effect, dict):
                    for f in _engine_flags:
                        effect.pop(f, None)
            for f in _engine_flags:
                action_data.pop(f, None)

        return raw

    def action_catalog(self) -> dict[str, dict[str, Any]]:
        """Generate action catalog for agents.

        Returns {action_name: {description, params: [{name, type, description}]}}
        for ALL public actions. No phase filtering — agents see the full list
        of actions they might use across the entire game. Runtime action_options
        from perceive controls what's available NOW.
        """
        catalog: dict[str, dict[str, Any]] = {}
        for name, action_cfg in self._config.actions.items():
            params = []
            for p in action_cfg.params or []:
                params.append(
                    {
                        "name": p.name,
                        "type": p.type,
                        **({"description": p.description} if p.description else {}),
                    }
                )
            catalog[name] = {
                "description": action_cfg.description or "",
                "params": params,
            }
        return catalog

    def actions_available_to(self, agent_id: str) -> set[str]:
        """Return the set of action names available to this agent (by available_to filter).

        System agents (narrator etc.) only see actions that explicitly include
        them via available_to — they never inherit the generic action pool.
        """
        from worldseed.dsl.preconditions import evaluate as eval_pre

        profile = self.registry.get_profile(agent_id)
        is_system = profile is not None and profile.system

        ctx = {"agent_id": agent_id, "action_params": {}, "tick": self.tick}
        result: set[str] = set()
        for name, action_cfg in self._config.actions.items():
            if action_cfg.available_to is None:
                if not is_system:
                    result.add(name)
            elif all(eval_pre(p, self.state, ctx) for p in action_cfg.available_to):
                result.add(name)
        return result

    # ------------------------------------------------------------------
    # Agent registration (delegates to registry)
    # ------------------------------------------------------------------

    def register_agent(
        self,
        agent_id: str,
        properties: dict[str, Any] | None = None,
        character: dict[str, Any] | None = None,
        *,
        omniscient: bool = False,
        system: bool = False,
        wake_on_push: bool = True,
    ) -> None:
        """Register an agent. Single chokepoint for all register paths.

        Idempotent: re-registering an already-claimed agent is a no-op.
        Always writes a "register" stream record at the current tick.
        """
        if self.registry.is_claimed(agent_id):
            return
        self.registry.register(
            agent_id,
            properties,
            character,
            omniscient=omniscient,
            system=system,
            wake_on_push=wake_on_push,
        )
        self.recorder.record("register", self.tick, agent_id=agent_id)

    def register_from_config(self) -> None:
        """Fully register all preset agents (entity + profile + claimed).

        Used by tests and sanity_runner. Production uses prepopulate_agents().
        """
        for agent_cfg in self._config.agents:
            if self.registry.is_claimed(agent_cfg.id):
                continue
            props = self.registry.merge_preset_properties(agent_cfg)
            self.register_agent(
                agent_id=agent_cfg.id,
                properties=props,
                character=dict(agent_cfg.character),
                omniscient=agent_cfg.omniscient,
                system=agent_cfg.system,
                wake_on_push=agent_cfg.wake_on_push,
            )

    def prepopulate_agents(self) -> None:
        """Create agent entities + profiles for UI/map without marking claimed.

        Agents show up on map and intro page but tick won't start until
        they register via plugin (agents_ready + maybe_auto_start_ticks).
        """
        self.registry.prepopulate_agents()

    def get_agent_profile(self, agent_id: str) -> AgentConfig | None:
        """Look up an agent's profile."""
        return self.registry.get_profile(agent_id)

    def get_characters(self) -> list[dict[str, Any]]:
        """List preset agents with claimed status."""
        return self.registry.get_characters()

    def update_character(self, agent_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
        """Update an agent's character card. Returns the updated character."""
        return self.registry.update_character(agent_id, overrides)

    def get_registered_agents(self) -> list[str]:
        """List registered agent IDs."""
        return self.registry.get_registered_agents()

    def get_system_agents(self) -> list[str]:
        """List IDs of system agents (hidden from normal agents/frontend)."""
        return self.registry.get_system_agents()

    def get_think_interval(self, agent_id: str) -> int:
        """Get agent's think interval."""
        return self.registry.get_think_interval(agent_id)

    def get_wake_on_push(self, agent_id: str) -> bool:
        """Check if agent should be woken by push events."""
        profile = self.registry.get_profile(agent_id)
        return profile.wake_on_push if profile else True

    def set_think_interval(self, agent_id: str, interval: int) -> None:
        """Set agent's think interval."""
        self.registry.update_think_interval(agent_id, interval)

    # ------------------------------------------------------------------
    # Actions + ticks
    # ------------------------------------------------------------------

    def validate_params(
        self,
        action_type: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Validate params against action config. Returns error dict or None."""
        action_cfg = self._config.actions.get(action_type)
        if action_cfg is None:
            return {
                "code": "unknown_action",
                "message": f"Unknown action: '{action_type}'",
                "available_actions": list(self._config.actions.keys()),
            }
        expected: dict[str, Any] = {}
        missing: list[str] = []
        for p in action_cfg.params:
            expected[p.name] = {"type": p.type, "required": p.required}
            if p.required and p.name not in params:
                missing.append(p.name)
        if missing:
            return {
                "code": "invalid_params",
                "message": (f"Missing required parameter(s) {missing} for action '{action_type}'"),
                "action": action_type,
                "expected": expected,
            }
        return None

    def submit(
        self,
        agent_id: str,
        action_type: str,
        params: dict[str, Any] | None = None,
    ) -> str | ActionResult | None:
        """Submit an action.

        Mechanical actions (no dm config) execute immediately and return ActionResult.
        DM actions are queued and return None on success, error string on failure.
        Raises ValueError if action is unknown or required params missing.
        """
        resolved = params or {}
        error = self.validate_params(action_type, resolved)
        if error is not None:
            msg = error["message"]
            raise ValueError(msg)

        action_cfg = self._config.actions.get(action_type)
        submission = ActionSubmission(
            agent_id=agent_id,
            action_type=action_type,
            params=resolved,
            tick_submitted=self.tick,
        )

        # Mechanical action: execute immediately, no queue
        if action_cfg is not None and action_cfg.dm is None:
            result = self._tick_engine._rules.process_action(submission, self.tick)
            action_dispatch.apply_mechanical_result(self, action_cfg, submission, result)
            return result

        # DM action: queue for next tick's step_async
        return self._queue.submit(submission)

    def step(self) -> list[ActionResult]:
        """Process one tick (sync — dm field skipped)."""
        results = self._tick_engine.step()
        self._observe_attention()
        return results

    async def step_async(self) -> list[ActionResult]:
        """Process one tick with async DM support."""
        results = await self._tick_engine.step_async()
        self._observe_attention()
        return results

    @property
    def tick(self) -> int:
        """Current tick number."""
        return self._tick_engine.tick

    @property
    def dm_call_count(self) -> int:
        """Total DM calls made since engine start."""
        return self._tick_engine.dm_call_count

    # ------------------------------------------------------------------
    # Perception + inbox
    # ------------------------------------------------------------------

    def agent_world_view(self, agent_id: str) -> dict[str, Any]:
        """Real-time world view for an agent (dashboard inspector)."""
        perceiver = self._tick_engine.perceiver
        if perceiver is None:
            return {
                "self_state": {},
                "nearby_entities": {},
                "nearby_agents": {},
                "events": [],
            }
        view = perceiver.build_agent_view(agent_id, self.tick)
        # Remap to external field names
        return {
            "self_state": view["self_state"],
            "nearby_entities": view["visible_entities"],
            "nearby_agents": view["visible_agents"],
            "events": view["events"],
        }

    def perceive(self, agent_id: str) -> AgentPerception:
        """What an agent can see right now. Single source of truth."""
        inbox = self._inbox_manager.get_or_create(agent_id)

        # If perceiver hasn't delivered yet, do a live deliver first
        # so the agent sees real visibility data (not empty)
        perceiver = self._tick_engine.perceiver
        if inbox.last_perceive_tick < 0 and perceiver is not None:
            perceiver.deliver(self.tick)

        data = inbox.read()
        options = self._build_action_options(agent_id)
        return build_perception(data, options)

    def _build_action_options(self, agent_id: str) -> dict[str, dict[str, Any]]:
        """Compact action options with resolved enum values for one agent."""
        return agent_view_helpers.build_action_options(self, agent_id)

    def read_inbox(self, agent_id: str) -> dict[str, Any]:
        """Read raw inbox data. Prefer perceive() for typed output."""
        inbox = self._inbox_manager.get_or_create(agent_id)
        return inbox.read()

    def peek_inbox(self, agent_id: str) -> dict[str, Any]:
        """Peek at an agent's inbox without draining."""
        inbox = self._inbox_manager.get_or_create(agent_id)
        return inbox.peek()

    def drain_inbox(self, agent_id: str) -> None:
        """Drain events + DMs from inbox (called after wake delivers data)."""
        inbox = self._inbox_manager.get_or_create(agent_id)
        inbox.read()  # drain events + DMs, keep state snapshot

    def peek_perception(self, agent_id: str) -> dict[str, Any]:
        """Build perception dict without draining inbox (for wake messages).

        Includes available_actions with dynamic enum (full, every wake)
        so agents always know valid action targets for their current state.
        """
        from worldseed.protocol.agent import _filter_description

        inbox = self._inbox_manager.get_or_create(agent_id)
        state = inbox._current_state
        raw_entities = dict(state.visible_entities) if state else {}
        schemas = self._build_action_options(agent_id)
        return {
            "self_state": dict(state.self_state) if state else {},
            "nearby_entities": _filter_description(raw_entities),
            "nearby_agents": dict(state.visible_agents) if state else {},
            "events": [e.to_dict() for e in inbox.peek_events()],
            "whispers": [m.to_dict() for m in inbox._whispers],
            "action_options": schemas,
            "tick": self.tick,
        }

    def send_whisper(
        self,
        agent_id: str,
        source: str,
        detail: str,
        msg_type: str = "whisper",
    ) -> None:
        """Send a whisper into an agent's inbox."""
        inbox = self._inbox_manager.get_or_create(agent_id)
        inbox.append_whisper(
            InboxWhisper(
                tick=self.tick,
                source=source,
                detail=detail,
                type=msg_type,
            )
        )

    @property
    def has_dm(self) -> bool:
        """Whether a DM provider is configured."""
        return self._tick_engine._dm_provider is not None

    def queue_entity_set(self, entity_id: str, prop: str, value: Any) -> None:
        """Queue a property change for tick boundary application."""
        self._tick_engine.pending_ops.enqueue_entity_set(entity_id, prop, value, self.tick)

    def queue_entity_remove(self, entity_id: str) -> None:
        """Queue an entity removal for tick boundary application."""
        self._tick_engine.pending_ops.enqueue_entity_remove(entity_id, self.tick)

    def queue_gm_resolve(
        self,
        text: str,
        target_entity_id: str | None = None,
    ) -> str:
        """Queue a GM natural-language command for DM resolution.

        Returns request_id. The command executes at the next tick boundary.
        """
        return self._tick_engine.pending_ops.enqueue_gm_resolve(
            text=text,
            tick=self.tick,
            target_entity_id=target_entity_id,
        )

    def get_wakeup_results(self) -> list[WakeupResult]:
        """Evaluate wakeup for all agents."""
        return self._wakeup.evaluate_all(self._inbox_manager)

    @property
    def inbox_manager(self) -> InboxManager:
        """Per-agent inbox manager. Public so out-of-process resolvers can deliver whispers."""
        return self._inbox_manager

    def refresh_perception_and_observe(self) -> None:
        """Re-deliver perception snapshots and run director observation.

        Used by paths that mutate state outside the normal tick (e.g. external
        DM resolve) so observers see the new state on their next perceive.
        """
        perceiver = self._tick_engine.perceiver
        if perceiver is not None:
            perceiver.deliver(self.tick)
        self._observe_attention()

    # ------------------------------------------------------------------
    # Director-signal facade
    # ------------------------------------------------------------------

    def director_enabled(self) -> bool:
        """True when SceneConfig.director.enabled = true."""
        return self._director.enabled

    def peek_director_signals(
        self,
        limit: int | None = None,
        types: list[str] | None = None,
    ) -> list[DirectorSignal]:
        """Pending director signals, FIFO. Does not drain."""
        return self._director.peek_signals(
            limit=limit,
            types=types,  # type: ignore[arg-type]
        )

    def ack_director_signal(self, signal_id: str) -> bool:
        """Mark an urgent or checkpoint signal as acked."""
        return self._director.ack_signal(signal_id)

    def get_director_dm_request(self, request_id: str) -> PendingDMRequest | None:
        return self._director.get_dm_request(request_id)

    def director_runtime(self) -> DirectorRuntime:
        """Direct handle for the API layer; do not bypass for normal use."""
        return self._director

    def resolve_director_dm_request(
        self,
        request_id: str,
        narrative: str,
        effects_raw: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """Apply an external DM judgment for a pending director DM request."""
        from worldseed import director_resolver

        return director_resolver.resolve(self, request_id, narrative, effects_raw)

    def _observe_attention(self) -> None:
        """Hook director runtime to evaluate urgent + checkpoint conditions.

        Called after any state-mutating engine path. No-op when director
        is disabled. Does not drain inboxes or notify agents.
        """
        if not self._director.enabled:
            return
        self._director.observe_attention(
            tick=self.tick,
            event_log=self.event_log,
            inbox_manager=self._inbox_manager,
            wakeup_results=self.get_wakeup_results(),
        )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def save_state(self) -> None:
        """Save current world state to disk (for pause/resume)."""
        entities = [e.to_full_dict() for e in self.state.all_entities()]
        # Include resolved characters so edits survive server restart
        characters = {
            aid: copy.deepcopy(profile.character)
            for aid, profile in self.registry._profiles.items()
            if profile.character
        }
        self.recorder.save_state(entities, self.tick, characters=characters)
        self.recorder.save_counters(dm_call_count=self._tick_engine.dm_call_count)
        self.recorder.save_transient(self._collect_transient())

    def load_state(
        self,
        entities: list[dict[str, Any]],
        tick: int,
        *,
        characters: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Restore world state from saved data."""
        from worldseed.models.entity import Entity

        # Clear existing state
        for eid in [e.id for e in self.state.all_entities()]:
            self.state.remove(eid)

        # Restore entities
        for e_dict in entities:
            d = dict(e_dict)  # don't mutate input
            eid = d.pop("id")
            etype = d.pop("type")
            constraints = d.pop("constraints", {})
            entity = Entity(id=eid, type=etype, _data=d, _constraints=constraints)
            self.state.add(entity)

        # Restore tick and counters
        counters = self.recorder.load_counters() or {}
        self._tick_engine.restore_state(
            tick=tick,
            dm_call_count=counters.get("dm_call_count", 0),
        )

        # Mark agents as claimed in registry (entity already in StateStore)
        from worldseed.models.config_schema import AgentConfig as _AC

        # Build lookup from config agents to restore character + flags
        config_agents = {a.id: a for a in self._config.agents}

        for entity in self.state.query_by_type("agent"):
            if not self.registry.is_claimed(entity.id):
                cfg_agent = config_agents.get(entity.id)
                char = dict(cfg_agent.character) if cfg_agent else {}
                omniscient = cfg_agent.omniscient if cfg_agent else False
                system = cfg_agent.system if cfg_agent else False
                self.registry._claimed.add(entity.id)
                self.registry._profiles[entity.id] = _AC(
                    id=entity.id,
                    character=char,
                    omniscient=omniscient,
                    system=system,
                )
                self.registry._think_intervals.setdefault(entity.id, 5)

        # Restore resolved characters from state.json (includes intro edits)
        if characters:
            for aid, char_data in characters.items():
                profile = self.registry._profiles.get(aid)
                if profile is not None:
                    profile.character = char_data

        # Re-setup narrator if configured (restores narrate action + flags
        # for narrator created by _setup_narrator, not in config.agents)
        if self._config.narrator and not self.registry.is_claimed("narrator"):
            self._setup_narrator()

        # Restore transient state (inbox, action queue, events, intervals)
        transient = self.recorder.load_transient()
        if transient:
            self._restore_transient(transient)

    # ── Transient state serialization (delegated to worldseed.transient) ──

    def _collect_transient(self) -> dict[str, Any]:
        """Snapshot pause/resume state into a JSON-serializable dict."""
        return transient_helpers.collect(self)

    def _restore_transient(self, data: dict[str, Any]) -> None:
        """Apply a transient snapshot back onto this engine."""
        transient_helpers.restore(self, data)
