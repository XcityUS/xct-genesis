"""Narrator helpers — style presets, instruction-string assembly, narration apply.

Extracted from WorldEngine so world.py stays a thin facade. The narrator system
agent is a regular agent with character.instructions; these helpers shape that
string and the side effects of `record_narration`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from worldseed.engine.inbox import InboxWhisper
from worldseed.models.event import Event

if TYPE_CHECKING:
    from worldseed.engine.event_log import EventLog
    from worldseed.engine.inbox import InboxManager
    from worldseed.engine.state_store import StateStore
    from worldseed.models.config_schema import (
        AgentConfig,
        NarratorConfig,
        PerceptionConfig,
    )
    from worldseed.persistence import NullRecorder, RunRecorder


# Style presets — language-neutral writing instructions appended to the
# narrator's SOUL.md. A scene picks a preset by name (NarratorConfig.style) or
# overrides with a free-text NarratorConfig.prompt.
NARRATOR_STYLES: dict[str, str] = {
    "storyteller": (
        "title: A chapter title that names the core tension or turning point. "
        "Use a balanced pair of phrases separated by a slash or dash if natural.\n"
        "body: Third-person serial narrator. 2-4 short paragraphs. "
        "Build tension — setup, escalation, cliffhanger. "
        "End mid-action or with an unanswered question. "
        "Every sentence carries new information. Cut filler.\n"
        "asides: Things the reader can see but characters cannot. "
        "1-2 sentences each. State the hidden truth plainly.\n"
        "whisper_options: A pointed hint derived from the aside. "
        "Short, actionable, specific to the target agent."
    ),
    "poet": (
        "title: A single image that holds the chapter's tension. "
        "No punctuation. 3-5 words maximum.\n"
        "body: One image per line. 4-8 lines total. "
        "No explanation — juxtaposition does the work. "
        "White space between stanzas. Concrete nouns, no adjectives. "
        "Let the gap between images carry the meaning.\n"
        "asides: A paired image — the visible and the hidden, side by side. "
        "One line each.\n"
        "whisper_options: A quiet note slid under the door. "
        "One image, not an instruction."
    ),
    "intel": (
        "title: Wire-service headline. Verb-led, no articles, present tense. "
        "Maximum 12 words.\n"
        "body: Bullet-point briefing. One dash per fact. "
        "Lead with highest-impact item. No adjectives, no commentary, "
        "no atmosphere. Just what happened, who did it, what changed. "
        "4-8 bullet points.\n"
        "asides: Analyst's contradiction log. "
        "State both sides in one flat sentence each. No editorial.\n"
        "whisper_options: Operational recommendation. "
        "One short imperative sentence."
    ),
    "noir": (
        "title: Short, atmospheric. A location, an object, or a mood. "
        "Under 6 words.\n"
        "body: Hard-boiled narrator voice. Short sentences. "
        "Present tense where it adds tension. Weather and lighting as mood. "
        "Everyone is hiding something — the narrator sees through it "
        "but doesn't judge, just observes with weary precision. "
        "2-3 paragraphs. Dry, clipped, no sentimentality.\n"
        "asides: State what nobody else noticed. "
        "Flat delivery, devastating implication. One sentence each.\n"
        "whisper_options: A terse warning. "
        "The kind of thing someone mutters without making eye contact."
    ),
    "gossip": (
        "title: Starts with a rumor hook — 'Did you hear...', "
        "'Word is...', 'Apparently...'. Conversational, breathless.\n"
        "body: Second-hand narration. Mix confirmed facts with speculation, "
        "hedging, and 'I heard from someone who...'. "
        "The narrator is piecing it together from fragments and may get "
        "details wrong. Breathless, digressive, occasionally self-correcting. "
        "2-4 paragraphs.\n"
        "asides: 'The part nobody is talking about:' or similar. "
        "Gossip-column energy but the content is real.\n"
        "whisper_options: 'You didn't hear this from me, but...' "
        "followed by a specific, actionable tip."
    ),
    "conspiracy": (
        "title: A connection statement — 'X happened right after Y' "
        "or 'The timeline doesn't add up'. Declarative, urgent.\n"
        "body: Pattern-finding narrator. Every event is evidence. "
        "Draw explicit connections between events that others missed. "
        "Use phrases like 'Notice the timing', 'This is not a coincidence'. "
        "Present tense for urgency. 2-4 paragraphs. "
        "The narrator is building a case — structured, logical, "
        "but seeing patterns everywhere.\n"
        "asides: A connection between two seemingly unrelated events. "
        "Each aside links two specific facts.\n"
        "whisper_options: A pointed question that forces the target "
        "to reconsider what they know."
    ),
    "bureaucrat": (
        "title: Formal incident report header — 'Incident Report: [subject]' "
        "or 'Memo Re: [subject]'. Dry, institutional.\n"
        "body: Official documentation voice. Field labels, reference numbers, "
        "passive voice. The narrator genuinely believes the paperwork matters "
        "more than the events. Emotional situations described in procedural "
        "language — the gap between the form and reality IS the voice. "
        "2-4 paragraphs structured as report sections.\n"
        "asides: Filed as footnotes or addenda. The bureaucracy acknowledges "
        "the problem exists but has no form for it.\n"
        "whisper_options: A procedural recommendation that accidentally "
        "contains real advice."
    ),
    "gameshow": (
        "title: A round announcement — 'Round N: [dramatic question]' "
        "or 'And behind door number N...'. Showmanship.\n"
        "body: The world is a competition and the narrator is the host. "
        "Agents are contestants. Every choice is a wager, every outcome "
        "has a score. Dramatic pauses, consolation prizes for failures. "
        "The host clearly has favorites. Cheerfully cruel about bad outcomes. "
        "Present tense, high energy. 2-4 paragraphs.\n"
        "asides: 'What our contestants don't know...' delivered with "
        "theatrical relish.\n"
        "whisper_options: A game-show hint — 'Psst, contestant [agent]: "
        "you might want to check [specific thing] before the next round.'"
    ),
    "trickster": (
        "title: A punchline or reversal — names the funniest or most absurd "
        "thing that happened. Conversational, slightly gleeful.\n"
        "body: The narrator is inside the chaos and loving it. "
        "Not cynical or above it — genuinely amused by reversals, "
        "collapsed plans, and unintended consequences. Quick, energetic prose. "
        "Celebrates when the powerful trip. May address the reader directly. "
        "Accurate but presented to maximize the comedy. 2-4 paragraphs.\n"
        "asides: States the hidden truth with visible delight. "
        "Not mean-spirited — finds the absurdity genuinely wonderful.\n"
        "whisper_options: A gleeful tip delivered with a wink."
    ),
}


def build_instructions(
    *,
    scene_description: str,
    perception: PerceptionConfig,
    ncfg: NarratorConfig,
    language: str,
) -> str:
    """Assemble the full SOUL.md instructions for the narrator agent."""
    style_instruction = ncfg.prompt or NARRATOR_STYLES.get(ncfg.style, "")

    visibility_text = ""
    if perception.visibility:
        rules = [r.model_dump(exclude_none=True) for r in perception.visibility]
        visibility_text = (
            "\n\nVISIBILITY RULES — agents can only see entities matching these conditions:\n"
            + "\n".join(f"  - {r}" for r in rules)
            + "\nYou (narrator) see everything. Agents do NOT."
        )

    hidden_text = ""
    if perception.hidden_properties:
        hidden_text = "\n\nHIDDEN PROPERTIES — only you can see these, agents cannot:\n" + ", ".join(
            perception.hidden_properties
        )

    instructions = (
        "You observe everything in this world and write structured chapter "
        "summaries. Write as if the reader is watching a story unfold — "
        "never refer to yourself, never use words like 'narrator' or "
        "'narration' in your output.\n\n"
        "WORKFLOW: On each wake you receive events since your last chapter. "
        "Read them, then call worldseed_narrate with your chapter. "
        "Do NOT call worldseed_perceive or worldseed_act — use only "
        "worldseed_narrate. NEVER output text — no commentary, no "
        "explanations. Text output wastes tokens.\n\n"
        f"Scene: {scene_description}"
        f"{visibility_text}"
        f"{hidden_text}\n\n"
        "Each chapter covers only NEW events since your last chapter. "
        "Never repeat previous content.\n\n"
        "OUTPUT FIELDS (pass to worldseed_narrate):\n"
        "- title: A chapter title that captures the core tension.\n"
        "- tldr: One sentence that captures what happened this chapter.\n"
        "- body: The narrative text. MAX 2-4 short paragraphs. "
        "Be dense — every sentence must carry new information.\n"
        "- asides: 0-3 asides to the reader. Things brewing under the "
        "surface that the reader can see but the characters can't. "
        "Keep each one 1-2 sentences. Separate with blank lines.\n"
        "- whisper_options: One whisper per aside, matching by position. "
        "Format: 'exact_agent_id: short note'. One per line."
    )
    if style_instruction:
        instructions += "\n\nWriting style: " + style_instruction
    if language:
        from worldseed.dm.prompt import _language_display

        lang_name = _language_display(language)
        instructions += f"\n\nIMPORTANT: Write ALL text in {lang_name}, including titles, headings, and chapter names."

    return instructions


def replace_style_block(profile: AgentConfig, style_instruction: str) -> None:
    """Swap the writing-style block in a narrator profile's instructions in place."""
    if not profile.character:
        return
    instructions = profile.character.get("instructions", "")
    marker = "\n\nWriting style: "
    idx = instructions.find(marker)
    if idx >= 0:
        end = instructions.find("\n\n", idx + len(marker))
        if end < 0:
            end = len(instructions)
        instructions = instructions[:idx] + marker + style_instruction + instructions[end:]
    else:
        lang_marker = "\n\nIMPORTANT: Write ALL"
        lang_idx = instructions.find(lang_marker)
        if lang_idx >= 0:
            instructions = instructions[:lang_idx] + marker + style_instruction + instructions[lang_idx:]
        else:
            instructions += marker + style_instruction
    profile.character["instructions"] = instructions


def replace_language_line(profile: AgentConfig, language: str) -> None:
    """Update the trailing language directive in narrator instructions."""
    if not profile.character:
        return
    from worldseed.dm.prompt import _language_display

    instructions = profile.character.get("instructions", "")
    lines = [ln for ln in instructions.split("\n") if not ln.startswith("IMPORTANT: Write ALL")]
    if language:
        lang_name = _language_display(language)
        lines.append(f"IMPORTANT: Write ALL text in {lang_name}, including titles, headings, and chapter names.")
    profile.character["instructions"] = "\n".join(lines)


def apply_narration(
    *,
    state: StateStore,
    event_log: EventLog,
    inbox_manager: InboxManager | None,
    recorder: RunRecorder | NullRecorder,
    tick: int,
    params: dict[str, Any],
) -> int | str:
    """Run the side effects of a narrator chapter submission.

    Returns the new chapter number on success, or an error string. Bypasses
    the action pipeline entirely; the narrator is a system agent.
    """
    narrator_ent = state.get("narrator")
    if narrator_ent is None:
        return "Narrator entity not found"

    if narrator_ent.get("_last_narrate_tick", -1) == tick:
        return "Already narrated this tick"

    chapter: int = int(narrator_ent.get("chapter_count", 0)) + 1
    state.update_property("narrator", "chapter_count", chapter)
    state.update_property("narrator", "_last_narrate_tick", tick)

    recorder.record(
        "action",
        tick,
        agent_id="narrator",
        action_type="narrate",
        params=params,
        success=True,
        highlight=True,
    )
    recorder.record(
        "highlight",
        tick,
        label=params.get("title", ""),
        source="narration",
    )

    title = params.get("title", "")
    tldr = params.get("tldr", "")
    event_log.append(
        Event(
            tick=tick,
            type="narration",
            source="narrator",
            detail=f"{title}\n{tldr}",
            ttl="permanent",
            scope="admin",
        )
    )

    whisper_options = params.get("whisper_options", "")
    if whisper_options and inbox_manager is not None:
        for line in whisper_options.strip().split("\n"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                target_id = parts[0].strip()
                note = parts[1].strip()
                if note and state.get(target_id) is not None:
                    inbox_manager.get_or_create(target_id).append_whisper(
                        InboxWhisper(
                            tick=tick,
                            source="narrator",
                            detail=note,
                            type="narrator_hint",
                        )
                    )

    return chapter
