# ADR-0001: Multi-Tenant Trust Model, Key Storage, Concurrency Limits, and Model List Delivery

**Status:** Accepted  
**Date:** 2026-06-21 (v2 — supersedes 2026-06-17 v1)  
**Deciders:** Architect (Sonnet 4.6), Orchestrator, Javen  
**Context Issue:** XCT-266 (parent: XCT-265 — 多租户改造 BYOK + 多 world 并发)

---

## Revision History

| Version | Date | Change |
|---|---|---|
| v1 | 2026-06-17 | Initial: JWT+JWKS, session cookie, N=1, real-time tokenhub fetch |
| v2 | 2026-06-21 | Updated D2 (EngineSession cache), D3 (N=3 LRU); maintained D1, D4; rejected D5 (B20) as out of scope |

---

## Context

WorldSeed (xct-genesis) is currently single-tenant: one global FastAPI `app.state.engine`, all LLM keys read from `os.environ`, no auth layer, and no isolation between users. The multi-tenant goal is:

> A user logs into `xcity.one`, gets their tokenhub API key + accessible model list, then launches an isolated world at `genesis.xcity.one` using their own token — without affecting other users.

Four cross-cutting decisions must be locked to proceed. A fifth (blockchain token standard) was proposed by the Orchestrator but is rejected as out of scope for this ADR — see Decision 5.

---

## Decision 1: xcity.one ↔ Genesis Trust Model

### Decision

**MAINTAINED from v1: Option (a) — xcity.one signs a short-lived JWT (RS256, 15-min TTL); Genesis validates via JWKS endpoint.**

xcity.one exposes `GET /.well-known/jwks.json`. Genesis caches the JWKS (TTL: 5 min) and validates `Authorization: Bearer <jwt>` on every protected route. Token lifetime: 15 min, with a silent refresh flow in the frontend. Refresh token stored in HttpOnly + Secure + SameSite=Lax cookie.

### Why v2 maintains v1 (counter-proposal to Orchestrator draft)

The Orchestrator's draft proposed upgrading to OAuth 2.0 Authorization Code Flow + PKCE, citing **"30-day JWT with no revocation = unacceptable for billing-grade product"** as the rejection reason for option (a).

This premise is incorrect: v1 specified a **15-min TTL**, not 30-day. The practical revocation window is ≤ 15 minutes — the same window OAuth 2.0 short-lived access tokens provide in practice. The distinction is:

| Property | v1 JWT + JWKS (15-min) | OAuth 2.0 + PKCE |
|---|---|---|
| Implementation time | ~1 day | ~1 week |
| Revocation window | ≤ 15 min | Immediate (via introspection/blocklist) |
| Stateless at Genesis | Yes | No (requires token introspection or shared blocklist) |
| Tickets unblocked | Immediately | +~1 week |

At the time of writing, 8 child tickets (XCT-265/268/269/270/271/272/273/274) have been frozen for ~3d 20h. An additional 1-week delay to implement OAuth 2.0 Code Flow infrastructure is not justified by the improvement from "15-min revocation window" to "immediate revocation" — especially for Phase 1 where billing enforcement is not yet live.

**OAuth 2.0 + PKCE is confirmed as the Phase 2 upgrade path.** The JWT middleware interface (Bearer token + `get_current_user` FastAPI dependency) is forward-compatible: only the token issuer changes.

### Counter-Arguments

| Concern | Response |
|---|---|
| **Billing-grade needs immediate revocation** | Phase 1 has no live billing enforcement. 15-min TTL is acceptable. When billing goes live (Phase 2), OAuth replaces this with zero interface change at Genesis. |
| **JWT compromise window** | Mitigated by: short TTL, HTTPS-only, HttpOnly refresh cookie, silent refresh limited to active sessions. |
| **Key rotation** | JWKS cache TTL ≤ 5 min + re-fetch on 401 handles rotation without downtime. |

### Impact

- **Auth middleware** (XCT-267): `get_current_user(token: str = Depends(oauth2_scheme))` validates JWT, extracts `user_id` + `session_id`.
- **Frontend** (XCT-272): passes `Authorization: Bearer <jwt>` on all Genesis API calls.
- **xcity.one**: must expose a JWKS endpoint.

### Phase 2 Upgrade Path

Replace with OAuth 2.0 Authorization Code Flow + PKCE when billing enforcement goes live. Genesis-side: swap JWKS URL for OIDC discovery endpoint, add token introspection or Redis blocklist. No changes to downstream route handlers.

---

## Decision 2: User tokenhub Key Storage

### Decision

**UPDATED in v2: Key is never persisted to disk or DB. Flow:**

1. xcity.one sets the tokenhub key in an **HttpOnly + Secure + SameSite=Lax cookie** (transport layer).
2. On world start, Genesis extracts the key from the cookie and holds it in the **`EngineSession` object in server memory** for the lifetime of the session.
3. `EngineSession` is purged on **idle timeout (default: 30 min)** — key is never written anywhere else.
4. Per-request: DM / Gazette / Narrator read the key from `EngineSession.credentials`, not from the cookie on every call.

### Change from v1

v1 re-read the key from the cookie on every request. v2 caches it in `EngineSession` in-memory. This reduces per-request cookie parsing overhead and gives explicit lifecycle control (idle eviction). The security posture is identical: key never touches disk or DB.

### Rationale

- Zero DB attack surface: no encrypted storage, no KMS, no key rotation infrastructure needed.
- `EngineSession` already owns world state; holding credentials there is architecturally consistent.
- 30-min idle eviction matches typical user session patterns and limits DRAM exposure window.
- HttpOnly + Secure + SameSite=Lax prevents JS key extraction and CSRF.

### Counter-Arguments

| Concern | Response |
|---|---|
| **DRAM key exposure** | Mitigated by 30-min idle eviction + OS memory protection. Risk is lower than DB storage breach. |
| **Cross-device session** | After idle, user re-authenticates via xcity.one (standard UX). Phase 2 adds KMS-backed persistent storage for "remember me" use cases. |
| **Memory leak on crash** | EngineSession is garbage-collected; no manual cleanup required. |

### Impact

- **Credential per-request injection** (XCT-268): `EngineSession.credentials.tokenhub_key` injected into DM / Gazette / Narrator.
- **Session lifecycle** (XCT-269): `start_world` populates `EngineSession.credentials` from cookie; idle timer triggers `evict_session(user_id)`.

### Phase 2 Upgrade Path

When "remember me" / cross-device is required: add KMS-wrapped key storage. `EngineSession.credentials` interface unchanged — only the hydration path changes (cookie → KMS decrypt).

---

## Decision 3: Per-User Active World Limit

### Decision

**UPDATED in v2: N = 3 with LRU eviction.**

- Each user may have up to 3 concurrently active worlds.
- When a 4th world is started, the least-recently-active world is checkpointed and evicted.
- Idle worlds (30 min, same as credentials) are proactively evicted before LRU kicks in.

### Change from v1

v1 set N = 1 for Phase 1 simplicity. The Orchestrator correctly identified that N = 1 is too restrictive for power users exploring multiple scenarios. N = 3 with LRU is the right balance: bounded resource usage, flexible enough for real use.

### Rationale

- N = 3 covers the common power-user pattern: one active world + one paused + one in setup.
- LRU eviction with checkpoint preserves state — evicted worlds are resumable.
- Idle timeout (30 min) reduces memory pressure before LRU even triggers.
- Resource ceiling: 3 × ~1 CPU core equivalent = predictable per-user cost.

### Counter-Arguments

| Concern | Response |
|---|---|
| **Implementation complexity vs N=1** | `Dict[user_id, OrderedDict[world_id, EngineSession]]` + `evict_lru(user_id)` helper. ~2–3h additional work over N=1. |
| **N=3 may still be too few** | Raise to 5 when usage data warrants it. Interface is the same. |
| **Eviction surprises user** | Frontend must surface active world count and warn on eviction. Evicted worlds appear in past-runs with resume option. |

### Impact

- **Multi-session engine** (XCT-269): `sessions: Dict[user_id, OrderedDict[world_id, EngineSession]]`. `start_world()` enforces N≤3 with LRU eviction. `evict_lru()` triggers checkpoint before teardown.
- **Persistence namespacing** (XCT-270): checkpoint written to `~/.worldseed/users/{user_id}/runs/{run_id}/` on eviction.
- **Frontend** (XCT-272): world list UI shows active count (N/3) with eviction warnings.

### Phase 2 Upgrade Path

Raise N per user tier (e.g., free: 1, pro: 3, enterprise: 10). Same `OrderedDict` structure; N becomes a per-user config value rather than a global constant.

---

## Decision 4: xcity.one Model List Delivery to Genesis

### Decision

**UNCHANGED from v1: Option (a) — Genesis fetches the model list in real-time from tokenhub using the user's API key.**

On `GET /api/world/models`, Genesis calls `tokenhub.xcity.one/models` with the user's key from `EngineSession.credentials` and returns the result. Cached in `EngineSession` for the session lifetime (model list doesn't change mid-world).

### Rationale

- Always reflects current key permissions (plan upgrades, key rotation take effect immediately).
- ~150 ms RTT is acceptable at world-start; not a per-tick hot path.
- Embedding model list in JWT claim makes it stale between token refresh cycles.

### Counter-Arguments

| Concern | Response |
|---|---|
| **tokenhub availability** | 5s timeout + 2 retries with exponential backoff. Cached last-known list as fallback. |
| **Extra latency** | One-time at world start; cached in `EngineSession` for all subsequent in-world calls. |

### Impact

- **Credentials injection** (XCT-268): `EngineSession.model_list` populated at world start via `tokenhub.get_models(key)`.
- **Frontend** (XCT-272): model selector reads from `GET /api/world/models` (Genesis proxy), not from JWT payload.

### Phase 2 Upgrade Path

Redis cache (`models:{sha256(key)}`, TTL 5 min) if tokenhub latency or availability becomes a concern. xcity.one invalidates on plan change.

---

## Decision 5 (PROPOSED BY ORCHESTRATOR — REJECTED AS OUT OF SCOPE)

### Proposal

The Orchestrator's draft added **"Token standard: B20 on Base vs ERC-20"** as decision #5, citing a 6/25 Base mainnet launch deadline.

### Rejection

**This decision does not belong in ADR-0001.** Reasons:

1. **Category mismatch**: ADR-0001 governs auth trust, credential storage, concurrency, and model routing within `xct-genesis`. B20 vs ERC-20 is a product/economics/blockchain decision that spans the entire Xcity platform — not a Genesis-specific architectural concern.

2. **Scope defined by the issue body**: XCT-266 explicitly scopes to 4 decisions: trust model, key storage, world limit, model list. Blockchain token standard was not in scope.

3. **The Orchestrator's own draft contradicts itself**: the draft states _"Pricing / token economics → XCT-239 (CTO 静默, separate escalation)"_ while simultaneously adding B20 as decision #5 in this ADR.

4. **Deadline urgency ≠ correct venue**: A 3d 6h deadline is a reason to create a separate urgent ADR, not to attach a blockchain decision to an auth ADR.

5. **Architectural independence**: The choice between B20 and ERC-20 has no dependency on and no effect on the trust model, key storage strategy, or world concurrency limits decided here.

**Recommended action**: Create ADR-0002 (or escalate XCT-239) for the B20 vs ERC-20 decision with the appropriate stakeholders (CTO, product). If the 6/25 deadline is real, that ADR needs to be filed and decided in the next 24 hours — independently of this one.

---

## Summary

| # | Decision | Choice | Phase 2 Path |
|---|---|---|---|
| 1 | xcity.one ↔ Genesis trust | JWT + JWKS (RS256, 15-min TTL, silent refresh) | OAuth 2.0 Authorization Code Flow + PKCE |
| 2 | tokenhub key storage | Never persisted; EngineSession in-memory + 30-min idle eviction | KMS-wrapped persistent storage for cross-device |
| 3 | Active worlds per user | N = 3, LRU eviction with checkpoint | N configurable per user tier |
| 4 | Model list delivery | Genesis real-time fetch from tokenhub; cached in EngineSession | Redis cache + plan-change invalidation |
| 5 | Blockchain token standard | **Out of scope** — file ADR-0002 / escalate XCT-239 | — |

## Blocked / Unblocked Issues

**Unblocked by this ADR:**
- XCT-267: Auth middleware (JWT validation)
- XCT-268: Credential per-request injection
- XCT-269: Multi-session engine refactor (N=3 LRU)
- XCT-270: Persistent per-user namespace
- XCT-272: Frontend BYOK + SSO

**Not blocked by this ADR (can proceed in parallel):**
- XCT-271: In-process Python agent runtime
- XCT-273: OpenClaw multi-tenant strategy (→ ADR-0002)
- XCT-274: MULTI_TENANT.md (references this ADR)
