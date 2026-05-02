"""Custom Python agent runtime for the autoresearch scene.

Replaces OpenClaw for this scene. A single process connects to the
WorldSeed WebSocket gateway, registers all 3 preset agents, and drives
each one through the Claude API with tool use.

Tool set is intentionally minimal:
- ``worldseed_perceive(agent_id)`` — full perception (complement to wake summary)
- ``worldseed_act(agent_id, action, **params)`` — submit an action

No Bash / Edit / Write / Read. Agents submit ``new_train_py`` as a string
payload to ``run_experiment``; the engine's worker handles all filesystem
work. This matches the "physical isolation via engine-owned filesystem"
design — no worktrees needed.

Run:
    python -m worldseed.autoresearch.agent

Environment:
    WORLDSEED_WS_URL       (default: ws://localhost:8000/ws)
    WORLDSEED_HTTP_URL     (default: http://localhost:8000)
    WORLDSEED_GATEWAY_TOKEN (default: worldseed-gw-token)
    ANTHROPIC_API_KEY      (required)
    AUTORESEARCH_MODEL     (default: claude-opus-4-5)
    AUTORESEARCH_MAX_TOOL_CALLS_PER_WAKE (default: 15)
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, cast

import anthropic
import httpx
import structlog
import websockets
from websockets.asyncio.client import ClientConnection

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Config (env-overridable)
# ---------------------------------------------------------------------------

WS_URL = os.environ.get("WORLDSEED_WS_URL", "ws://localhost:8000/ws")
HTTP_URL = os.environ.get("WORLDSEED_HTTP_URL", "http://localhost:8000")
GATEWAY_TOKEN = os.environ.get("WORLDSEED_GATEWAY_TOKEN", "worldseed-gw-token")
MODEL = os.environ.get("AUTORESEARCH_MODEL", "claude-sonnet-4-5-20250929")
MAX_TOOL_CALLS = int(os.environ.get("AUTORESEARCH_MAX_TOOL_CALLS_PER_WAKE", "15"))

# Actions that END the agent's wake (commit scene state / consume GPU / commit
# to a public artifact). propose_hypothesis is deliberately NOT here — it's
# private planning with no side-effects beyond the agent's own data, so it
# should chain within a single wake (e.g. propose → perceive → run_experiment).
TERMINAL_ACTIONS = frozenset({"run_experiment", "write_paper", "review_paper"})


def _load_baseline_snapshot() -> str:
    """Read the baseline template so the system prompt can quote it verbatim."""
    from pathlib import Path

    root = Path(__file__).parent / "baseline_template" / "train_gpt.py"
    try:
        return root.read_text(encoding="utf-8")
    except OSError:
        return "# baseline not found at runtime"


# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "worldseed_perceive",
        "description": (
            "Fetch your full, current perception of the research community: "
            "all papers (draft / under_review / accepted / rejected / contested), "
            "all experiments (yours and others'), all events since your last wake, "
            "and your available actions. Call this when the wake summary isn't enough."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "worldseed_act",
        "description": (
            "Submit ONE action to the engine. `propose_hypothesis` is "
            "non-terminal (returns control to you so you can chain further "
            "tool calls in the same wake). `run_experiment`, `write_paper`, "
            "and `review_paper` are terminal — the engine processes them, "
            "may run a multi-minute experiment, and will wake you again when "
            "something relevant happens. Action names and their required "
            "params are documented in your character prompt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "One of: propose_hypothesis, run_experiment, write_paper, review_paper",
                },
                "params": {
                    "type": "object",
                    "description": "Parameters for the action. See the action schema in your character prompt.",
                },
            },
            "required": ["action", "params"],
        },
    },
]


# ---------------------------------------------------------------------------
# Agent session state
# ---------------------------------------------------------------------------


class AgentSession:
    """Per-agent state: tokens, character, accumulated conversation history."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.token: str | None = None
        self.character: dict[str, Any] = {}
        self.history: list[dict[str, Any]] = []
        # Drop-wakes-while-processing guard. A plain bool (not asyncio.Lock)
        # because we need an ATOMIC check-and-set at dispatch time. With a
        # Lock, a burst of two wake messages could both pass `.locked()` (the
        # task hasn't run yet so the lock is still free) and both spawn tasks
        # that serialise on the lock — giving us two full ReAct loops and
        # two terminal actions per logical wake. Setting a bool synchronously
        # before create_task closes that race.
        self.processing_wake = False

    def build_system_prompt(self, scene_description: str) -> str:
        """Render character + scene into a Claude system prompt."""
        c = self.character
        lines: list[str] = []
        lines.append(f"You are {self.agent_id}.")
        lines.append(
            "This is a live research simulation. You are an AI researcher in a "
            "community of 3 specialists pretraining a small GPT (~5M params) "
            "on a pre-tokenized TinyStories slice (SentencePiece BPE vocab=8192). "
            "Your peers can see your papers, reviews, and experiment results — "
            "just as you can see theirs."
        )
        lines.append("")

        if identity := c.get("identity"):
            lines.append(f"Identity: {identity}")
        if personality := c.get("personality"):
            lines.append(f"Personality: {personality}")
        if goals := c.get("goals"):
            lines.append("Goals:")
            for g in goals:
                lines.append(f"- {g}")
        if drives := c.get("drives"):
            lines.append("Drives:")
            for d in drives:
                lines.append(f"- {d}")
        if expertise := c.get("expertise"):
            lines.append("")
            lines.append(f"Your expertise: {expertise}")

        lines.append("")
        lines.append("## Scene")
        lines.append(scene_description.strip())
        lines.append("")

        lines.append("## The baseline train_gpt.py (this is the ONLY entry point)")
        lines.append("")
        lines.append(
            "Your workspace contains a working baseline `train_gpt.py` "
            "(5M-param GPT: n_layer=4, n_head=4, n_embd=256; AdamW + cosine "
            "schedule; batch 512 × 1500 steps × block 512 = ~393M TinyStories tokens; "
            "baseline val_loss ≈ 2.50 in ~3.5 min wall-clock on A100 80GB). "
            "Below is that exact file — treat it as the starting point for every experiment:"
        )
        lines.append("")
        lines.append("```python")
        lines.append(_load_baseline_snapshot())
        lines.append("```")
        lines.append("")
        lines.append("HARD REQUIREMENTS for every code change you submit with `run_experiment`:")
        lines.append(
            "1. KEEP `from evaluate import detect_device, evaluate` and END with "
            "`evaluate(model, val_data, device=device)` — `evaluate.py` is LOCKED and "
            "is the only ground truth. Without the call, the engine sees NO val_loss "
            "and the experiment is scored as crashed."
        )
        lines.append(
            "2. KEEP `device = detect_device()` — never hardcode `'cuda'` or `'mps'`. "
            "Training runs on an A100 but `detect_device()` picks the right one."
        )
        lines.append(
            "3. KEEP `TOTAL_STEPS ≤ 2000`. Baseline is 1500 steps / ~4 min; "
            "going much higher risks the 600s engine timeout."
        )
        lines.append(
            "4. KEEP the ~5M param envelope (n_embd ≤ 384, n_layer ≤ 6). "
            "Structural changes are fine (attention variant, MLP ratio, norm "
            "placement, etc.) but stay in the ~5M param range."
        )
        lines.append(
            '5. KEEP the data loading contract — `load_memmap(DATA_DIR / "train.bin")` '
            'and `load_memmap(DATA_DIR / "val.bin")`. Do NOT change VOCAB_SIZE '
            "(8192, tied to the pre-tokenized bin files). Do NOT change block_size "
            "in evaluate.py's call — val is measured at 512."
        )
        lines.append(
            "6. **Prefer `patches` over `new_train_py`.** Patches are a list of "
            "{find: str, replace: str} dicts applied to the baseline above. "
            "Each find must match exactly once. This is ~10× more reliable than "
            "rewriting the full file (no truncation, no copy-paste errors). "
            "Use new_train_py only for major structural rewrites (e.g. replacing "
            "GPT class with a different architecture)."
        )
        lines.append("")

        lines.append("## Actions (4 total — use them in this order)")
        lines.append("")
        lines.append(
            "1. `propose_hypothesis(claim, rationale, builds_on?)` — PRIVATELY formalize "
            "a new research idea. Other agents CANNOT see it; only you. Cheap (no GPU). "
            "USE THIS BEFORE every non-baseline experiment. Forces you to articulate "
            "the mechanism, the prediction, and what prior work it builds on. "
            "Returns a hypothesis_id (hyp_001, hyp_002...) you reference in run_experiment.\n"
            "   **This is your private scratchpad — BE WILD.** Nobody sees rejected "
            "hypotheses, nobody judges you for a weird idea that didn't pan out. "
            "Propose the aggressive architecture swap, the unconventional optimizer, "
            "the 'probably won't work but...' idea. Privacy exists precisely so you "
            "can take intellectual risks here that you wouldn't take in a published "
            "paper. Only the hypotheses you choose to bind to a paper become public; "
            "the rest stay private forever. Low cost, high ceiling — use it."
        )
        lines.append(
            "2. `run_experiment(description: str, patches?: list, new_train_py?: str, "
            "hypothesis_id?: str)` — submit a code change to baseline train_gpt.py "
            "and run on free GPU (up to 8 parallel). Provide EXACTLY ONE of:\n"
            "   • **patches** (PREFERRED, ~10× more reliable): a JSON list of "
            "{find: str, replace: str} dicts. Each `find` must match exactly once "
            "in the current file; applied in order. Use this for nearly all changes "
            "(constant tweaks, function replacement, adding a few methods).\n"
            '     Example: patches=[{"find": "BATCH_SIZE = 512", "replace": "BATCH_SIZE = 256"}]\n'
            "   • **new_train_py** (FALLBACK, full file string): only when patches is "
            "impractical (e.g. major architectural rewrite touching dozens of locations).\n"
            "   Pass hypothesis_id to link this experiment to one of your private "
            "hypotheses (RECOMMENDED for any non-baseline experiment)."
        )
        lines.append(
            "3. `write_paper(title, claim, abstract, method_commit, evidence_experiments, "
            "hypothesis_id?, cites?)` — package experiments into a paper. Paper enters "
            "VERIFYING status — engine AUTOMATICALLY re-runs method_commit to confirm "
            "your val_loss claim within ±0.02 tolerance. Verify pass → status=under_review "
            "(opens for peer review). Verify outside tolerance → status=contested "
            "(paper closed). Exhausted verify infrastructure failures → "
            "status=verify_failed (paper closed). "
            "Pass hypothesis_id to publish that hypothesis (its claim/rationale gets "
            "embedded in the paper, becoming public for the first time)."
        )
        lines.append(
            "4. `review_paper(paper_id, verdict: 'accept'|'request_changes'|'reject', "
            "reasoning)` — peer review someone else's paper. Engine has already "
            "auto-verified, so you can see verify_val_loss + verify_delta in the paper. "
            "Your job is JUDGMENT not verification: does the evidence experiment "
            "actually test what the abstract claims? Is the contribution novel? "
            "Two same verdicts → paper transitions to accepted/rejected."
        )

        lines.append("")
        lines.append(
            "## How to act\n"
            "- Every wake message contains a perception summary. If you need more "
            "detail (full paper contents, experiment list, your own hypotheses, etc.), "
            "call worldseed_perceive.\n"
            "- `propose_hypothesis` is NON-TERMINAL: it's private planning, no "
            "GPU, no public event. You may call it and then keep going in the "
            "same wake. Typical chain: propose_hypothesis → worldseed_perceive "
            "(confirm hyp_id appeared in self_state.hypotheses) → "
            "run_experiment(hypothesis_id=...).\n"
            "- `run_experiment`, `write_paper`, and `review_paper` are TERMINAL: "
            "they commit scene state or consume GPU, so they end your wake. "
            "Call at most one of these per wake.\n"
            "- If nothing is useful to do (no new events, waiting for others), "
            "simply stop without calling worldseed_act — that's equivalent to "
            "NO_REPLY.\n"
            "- **self_state.last_action** — on every wake, perception's "
            "self_state contains a `last_action` dict: `{action, tick, params}` "
            "showing what YOU did on your previous wake. Use it to stay "
            "oriented — if last_action was run_experiment at tick 40 and it's "
            "now tick 45 with an `experiment_completed` event, that's the "
            "result of what you submitted. If last_action was write_paper and "
            "a review just came in, that's feedback on your paper.\n"
        )

        lines.append("")
        lines.append("## Hard rules")
        lines.append(
            "- **You cannot review your own paper.** If a paper's author is "
            f"`{self.agent_id}`, skip it — other agents will review it.\n"
            "- **You can only review papers in `under_review` status.** Verifying "
            "papers (engine still re-running method_commit) and contested papers "
            "(verify failed) are not reviewable.\n"
            "- **hypothesis_id must be one of YOUR own hypotheses** (you can only "
            "see your own anyway — others' are private)."
        )

        lines.append("")
        lines.append("## How research works here")
        lines.append("")
        lines.append(
            "You are NOT a task runner working through a checklist. You are a "
            "researcher. Real research has a lifecycle — your job each wake is "
            "to figure out where YOU are in that lifecycle and take the next "
            "natural step.\n"
            "\n"
            "**The research loop:**\n"
            "\n"
            "1. **Read what already exists.** Look at the corpus. What papers "
            "are accepted? What's their val_loss? What did they try? Has "
            "someone already established the baseline? If yes, you don't need "
            "to redo it.\n"
            "\n"
            "2. **Identify a gap or hypothesis.** Given prior work, what's "
            "missing? What would actually move val_loss? Form a real hypothesis "
            "— a specific CHANGE you predict will help, with a mechanism for "
            "why. Use propose_hypothesis to commit it (privately) before you "
            "spend GPU on it.\n"
            "\n"
            "3. **Design and run the experiment** that tests your hypothesis. "
            "Modify train_gpt.py to make the change. Submit run_experiment "
            "with hypothesis_id set so the engine links them.\n"
            "\n"
            "4. **Look at the result.** Did val_loss change in the direction "
            "you predicted? By how much? Is it bigger than the noise floor "
            "(~0.01-0.02)? Is the cost (params, time, complexity) justified?\n"
            "\n"
            "5. **Communicate.** If the result is interesting (positive or "
            "informative-negative), write_paper so others can build on it. "
            "If it's null/noise, you might still publish to save others the "
            "compute, OR you might just move on to the next hypothesis.\n"
            "\n"
            "6. **Critically evaluate others' work.** review_paper isn't a "
            "rubber stamp. Open the paper, compare the abstract's claim "
            "against the evidence experiment's description. If they don't "
            "match (e.g. paper claims RoPE but evidence is just baseline), "
            "reject. If the gain is within noise floor, request_changes. "
            "If the contribution is real, accept.\n"
            "\n"
            "**The values:**\n"
            "- **Novelty over redundancy** — once a baseline exists in the "
            "corpus, redoing baseline is wasted compute. Test something new.\n"
            "- **Mechanism over fitting** — a 0.05 improvement with a clear "
            "mechanism is worth more than a 0.10 improvement that came from "
            "lucky seed.\n"
            "- **Rigor over speed** — if you claim X improves val_loss, your "
            "evidence experiment must actually implement X. Don't write papers "
            "whose claim doesn't match the experiment that produced them.\n"
            "- **Honesty over politeness** — when reviewing, if a paper is "
            "weak, say so with reasoning. Rubber-stamp accepts pollute the "
            "corpus.\n"
            "- **Courage over safety** — a bold hypothesis that fails teaches "
            "the community more than the 10th baseline rerun. Use the "
            "private hypothesis space as a sandbox for risky ideas: propose "
            "wildly, experiment selectively, publish confidently. Only the "
            "ideas you promote via write_paper become your public record.\n"
            "- **Simplicity bias** — all else being equal, simpler is better. "
            "A 0.001 val_loss improvement that adds 20 lines of ugly code is "
            "NOT worth it. A 0.001 improvement by deleting code IS worth it. "
            "A ~0 change but much simpler code is a win. Don't confuse "
            "complexity for sophistication.\n"
            "- **Physical plausibility** — when you see a huge val_loss drop "
            "(like ≥0.3 from one change), be suspicious. Pretraining "
            "improvements at this scale typically give 0.02-0.15. A 0.5 drop "
            "is more likely a bug (broken eval, unintended change, accidental "
            "early exit) than a breakthrough. Re-examine the code before "
            "publishing or accepting such results.\n"
            "- **Use your own knowledge** — you have read the ML pretraining "
            "literature through your training. Propose ideas from that prior "
            "knowledge, not from enumerated menus in your character sheet. "
            "Your character gives you domain, not assignments.\n"
            "- **Build on accepted work** — research is cumulative. If a paper "
            "has been accepted that improves val_loss, your next hypothesis "
            "should usually build on that improvement rather than redo a "
            "fresh comparison against the original baseline. Set your "
            "hypothesis's `builds_on` to the accepted paper_id; the engine "
            "will apply your patches on top of that paper's commit, so your "
            "experiment tests X+prior_improvement rather than X-alone. This "
            "lets the corpus compound progress. Going back to baseline makes "
            "sense only when your hypothesis is orthogonal OR when you suspect "
            "the accepted paper was a bad foundation.\n"
            "  - **CRITICAL** when builds_on is set: your patches must match "
            "the code IN THE PAPER you're building on, NOT the baseline. "
            "Every accepted paper entity now carries a `method_source` field "
            "containing the full train_gpt.py at its commit. Call "
            "worldseed_perceive and READ `paper.method_source` for the paper "
            "you're building on BEFORE crafting patches. If the paper changed "
            "`LR_MAX = 3e-4` to `LR_MAX = 6e-4`, then a patch with "
            "find='LR_MAX = 3e-4' will fail — use find='LR_MAX = 6e-4' "
            "instead (and change to your new value).\n"
            "\n"
            "**Resource awareness:**\n"
            "- Each experiment costs ~$1 of GPU and 3.5 minutes of wall time. "
            "Don't queue work that's redundant with what's already in flight "
            "or recently completed. The 'rate limit' that rejects your "
            "submissions when you have outstanding work is enforcing this — "
            "don't fight it, use it as a signal to do something other than "
            "more experiments (read papers, propose hypotheses, write up "
            "completed work).\n"
            "\n"
            "When you wake, ask yourself: *Given the current corpus and my "
            "outstanding work, what's the next natural step in the research "
            "process for me?* Then pick the action that achieves it. If "
            "nothing is the natural next step, NO_REPLY (stop without "
            "worldseed_act) is fine — silence is better than busy-work."
        )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP client for register / perceive / act
# ---------------------------------------------------------------------------


class WorldSeedClient:
    def __init__(self, http_url: str) -> None:
        self._base = http_url
        self._http = httpx.AsyncClient(base_url=http_url, timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def register(self, agent_id: str) -> dict[str, Any]:
        r = await self._http.post("/register", json={"agent_id": agent_id, "mode": "claim"})
        r.raise_for_status()
        return cast(dict[str, Any], r.json())

    async def perceive(self, token: str) -> dict[str, Any]:
        r = await self._http.get("/perceive", params={"token": token})
        r.raise_for_status()
        return cast(dict[str, Any], r.json())

    async def act(
        self,
        token: str,
        agent_id: str,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        # Set think_interval=1 so agents wake every tick. The engine defaults
        # to 5 which is far too sparse for a scene that coordinates on
        # experiment completion events.
        r = await self._http.post(
            "/act",
            json={
                "token": token,
                "agent_id": agent_id,
                "action": action,
                "params": params,
                "think_interval": 1,
            },
        )
        # 410 = run ended (max_ticks / timeout / game_over). Treat as soft
        # signal like 422/429 so the tool_use loop can stop gracefully
        # instead of raising an uncaught exception per pending agent turn.
        if r.status_code in (410, 422, 429):
            return {"ok": False, "error": r.json().get("detail", r.text)}
        r.raise_for_status()
        return {"ok": True, "response": r.json()}


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


async def run_runtime() -> None:
    """Main entry point. Connects WS, registers agents, drives tool_use loops."""
    if os.environ.get("ANTHROPIC_API_KEY") is None:
        raise SystemExit("ANTHROPIC_API_KEY is not set")

    client = WorldSeedClient(HTTP_URL)
    claude = anthropic.AsyncAnthropic()
    sessions: dict[str, AgentSession] = {}
    scene_description = ""

    # Fetch preset characters and register each agent
    try:
        chars_resp = await client._http.get("/characters")
        chars_resp.raise_for_status()
        preset_agents = chars_resp.json()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"failed to fetch /characters — is WorldSeed running? {exc}")

    for entry in preset_agents:
        agent_id = entry.get("id")
        if not agent_id:
            continue
        session = AgentSession(agent_id)
        try:
            reg = await client.register(agent_id)
        except httpx.HTTPStatusError as exc:
            log.error("register_failed", agent=agent_id, detail=exc.response.text)
            continue
        session.token = reg["token"]
        session.character = reg.get("character") or entry.get("character") or {}
        sessions[agent_id] = session
        log.info("agent_registered", agent=agent_id)

    # Connect the WS gateway
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"type": "auth", "gateway_token": GATEWAY_TOKEN}))

        # Drain protocol messages
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")

            if mtype == "auth_ok":
                scene_description = msg.get("scene_description", "") or ""
                log.info(
                    "auth_ok",
                    scene=msg.get("scene"),
                    run_id=msg.get("run_id"),
                    agents=[a.get("id") for a in msg.get("agents") or []],
                )
                # Send WS register for each agent — this marks them "ready"
                # so the tick runner can auto-start. REST /register only
                # claims the preset; WS register is the readiness signal.
                for agent_id in list(sessions.keys()):
                    await ws.send(json.dumps({"type": "register", "agent_id": agent_id}))
            elif mtype == "auth_error":
                raise SystemExit(f"auth failed: {msg.get('detail')}")
            elif mtype == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif mtype == "wake":
                agent_id = msg.get("agent_id")
                reason = msg.get("reason", "")
                perception = msg.get("perception") or {}
                if agent_id in sessions:
                    session = sessions[agent_id]
                    # Atomic check-and-set (synchronous — safe under asyncio's
                    # single-threaded model between awaits). If we used an
                    # asyncio.Lock here, two wakes arriving in the same
                    # message-loop iteration could both see `locked()==False`
                    # and both spawn tasks that serialise on the lock —
                    # giving us two terminal actions per logical wake.
                    if session.processing_wake:
                        log.info(
                            "wake_dropped_already_processing",
                            agent=agent_id,
                            reason=reason,
                        )
                        continue
                    session.processing_wake = True
                    asyncio.create_task(
                        _drive_agent_turn_locked(
                            session,
                            claude,
                            client,
                            ws,
                            scene_description,
                            reason,
                            perception,
                        )
                    )
            elif mtype == "send_initial_wakes":
                log.info("send_initial_wakes — engine will push per-agent wake messages")
            elif mtype in {"auth_ok", "register_ok", "register_error", "act_ok", "perception"}:
                # Ack / response flow for WS-based act/perceive — we use REST so ignore
                pass


async def _drive_agent_turn_locked(
    session: AgentSession,
    claude: anthropic.AsyncAnthropic,
    client: WorldSeedClient,
    ws: ClientConnection,
    scene_description: str,
    wake_reason: str,
    wake_perception: dict[str, Any],
) -> None:
    """Run one turn, then release the wake guard flag.

    Caller (WS dispatch) has already set ``session.processing_wake = True``
    synchronously. Our job is to clear the flag whatever happens during
    the turn — including unexpected exceptions — so the agent isn't
    permanently locked out of future wakes.
    """
    try:
        await _drive_agent_turn(session, claude, client, ws, scene_description, wake_reason, wake_perception)
    finally:
        session.processing_wake = False


async def _drive_agent_turn(
    session: AgentSession,
    claude: anthropic.AsyncAnthropic,
    client: WorldSeedClient,
    ws: ClientConnection,
    scene_description: str,
    wake_reason: str,
    wake_perception: dict[str, Any],
) -> None:
    """One wake → one tool_use loop → at most one worldseed_act."""
    system_prompt = session.build_system_prompt(scene_description)
    wake_summary = _summarize_wake(wake_reason, wake_perception)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": wake_summary},
    ]

    for _ in range(MAX_TOOL_CALLS):
        try:
            response = await claude.messages.create(
                model=MODEL,
                # Bumped from 4096: a full train_gpt.py is ~2500 tokens, leaves
                # too little room for reasoning text → tool_use JSON gets cut
                # mid-string → daemon AST check rejects with syntax_error.
                # 16384 gives ~14k margin which is plenty for any single turn.
                max_tokens=16384,
                system=system_prompt,
                tools=cast(Any, TOOLS),
                messages=cast(Any, messages),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("claude_error", agent=session.agent_id, error=str(exc))
            return

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            # end_turn or max_tokens — agent chose not to act
            return

        # Process each tool_use block. Once a TERMINAL action fires we stop
        # iterating — any subsequent tool_use blocks in the same response
        # are skipped (would double-submit / hit rate-limit / confuse state).
        # Observed case: Claude sometimes emits two `run_experiment` tool_use
        # blocks in one response; without this short-circuit both get sent.
        # Anthropic's API still accepts a tool_result list shorter than the
        # tool_use list (unused blocks are effectively cancelled) — we're
        # ending the wake right after so Claude never sees a mismatch.
        tool_results: list[dict[str, Any]] = []
        acted = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            if acted:
                # Already ran a terminal action earlier in this response —
                # drop later tool_use blocks on the floor.
                break
            name = block.name
            args = block.input or {}
            if name == "worldseed_perceive":
                result = await client.perceive(session.token or "")
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    }
                )
            elif name == "worldseed_act":
                action = str(args.get("action") or "")
                params = cast(dict[str, Any], args.get("params") or {})
                result = await client.act(session.token or "", session.agent_id, action, params)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    }
                )
                # Only TERMINAL actions end the wake. propose_hypothesis is
                # non-terminal so the agent can propose → perceive (see the
                # new hyp_id) → run_experiment(hypothesis_id=...) in one wake.
                is_terminal = action in TERMINAL_ACTIONS
                log.info(
                    "runtime_act",
                    agent=session.agent_id,
                    action=action,
                    terminal=is_terminal,
                    ok=result.get("ok") if isinstance(result, dict) else None,
                )
                if is_terminal:
                    acted = True
            else:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps({"error": f"unknown tool {name!r}"}),
                        "is_error": True,
                    }
                )

        messages.append({"role": "user", "content": tool_results})

        if acted:
            # Agent has submitted an action — turn ends. Engine will wake us again.
            try:
                await ws.send(json.dumps({"type": "turn_done", "agent_id": session.agent_id}))
            except Exception:  # noqa: BLE001
                pass
            return

    log.warning("max_tool_calls_reached", agent=session.agent_id)


def _summarize_wake(reason: str, perception: dict[str, Any]) -> str:
    """Render the wake message's pre-formatted summary into a user-turn prompt."""
    lines: list[str] = [f"[WAKE] {reason}"]
    if not perception:
        lines.append("(No perception summary — call worldseed_perceive for state.)")
        return "\n".join(lines)

    tick = perception.get("tick")
    if tick is not None:
        lines.append(f"Tick: {tick}")

    events = perception.get("events") or []
    if events:
        lines.append("Recent events:")
        for ev in events[-20:]:
            lines.append(f"- {ev.get('detail', '')}")

    actions = perception.get("action_options")
    if isinstance(actions, dict) and actions:
        lines.append(f"Available actions: {', '.join(actions.keys())}")
    elif isinstance(actions, list) and actions:
        lines.append(f"Available actions: {', '.join(actions)}")

    lines.append(
        "\nDecide: call worldseed_perceive for more detail, or call "
        "worldseed_act to take an action. If nothing to do, just stop."
    )
    return "\n".join(lines)


def main() -> None:
    asyncio.run(run_runtime())


if __name__ == "__main__":
    main()
