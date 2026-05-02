# Codex Subagents Guide

This guide is for running WorldSeed with a primary Codex session and Codex
subagents.

Read in this order:

1. [Core Adapter](00-core.md) — what Codex owns, what WorldSeed owns.
2. [Runtime Contract](01-contract.md) — primary session, workers, watcher, APIs.
3. [Actions And Publish](02-actions-and-publish.md) — how artifacts enter the world.
4. [Codex Loop](03-codex-loop.md) — how to run, wait, wake, and steer.
5. [Workspace And Story](04-workspace-and-story.md) — how evidence becomes a `present.json` case study.
6. [Scenario Architecture](05-scenario-architecture.md) — how to design scenes that can produce emergence.
7. [用 Codex 跑 WorldSeed](06-codex-runner-usage.zh-CN.md) — current `worldseed run` + `codex-runner` usage, including optional `scene.codex`.

Use [Scenario Architecture](05-scenario-architecture.md) before writing a new
scenario. Runtime wiring alone usually produces a simple producer/critic
pipeline; interesting runs need role pressure, private information,
consequences, and artifact history.
