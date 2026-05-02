# Workspace And Story

This adapter treats the workspace as the durable evidence folder.

## trajectory.md

For Codex and workers during the run.

Contains:

```text
objective
success criteria
roles
suggested pressure sequence
expected artifact types
stop/ship conditions
```

It is not engine logic and not the user-facing story.

## story.md

For the user near the end.

It should explain:

```text
what happened
who produced what
which branches diverged
which critiques changed direction
what was revised
what was selected or rejected
where final files live
```

`story.md` should cite artifact ids, critique ids, selected/rejected ids, and
final refs.

## present.json

The final case study. Optional. The default ship format when the run has
enough evidence to curate. Rendered at `/present/<workspace-id>` by the same
React component that drives `/pilot`.

Generate the skeleton:

```bash
uv run worldseed present-skeleton --workspace <workspace path>
```

Writes `present-skeleton.json` with mechanical fields auto-filled (`eyebrow`,
`panel`, `branches` structure, `story.intro` synced from `story.md`) and
`"TODO: ..."` placeholders for narrative. Curate each TODO from the run
evidence (stream.jsonl, lane jsonl, files), then save as `present.json` next
to it.

Schema is `PilotDataset` in
`frontend/src/components/pilot/pilot-data-types.ts`. See `/pilot` for 4
worked examples (autoresearch / autoeditor / tree_rag / mixed_campaign).

Good present.json:

```text
starts with the conclusion (verdict.lead)
walks the chronological story (story.intro + story.moments)
attaches critiques to specific versions (versions[].reviews)
shows revision lineage in the branch graph
references workspace files via /workspaces/<id>/agents/<aid>/files/...
```

If the run has too little evidence to curate honestly, ship `story.md` plus
artifact refs and skip present.json.

