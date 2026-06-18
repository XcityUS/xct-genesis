# ADR-0001: Multi-Tenant Trust Model, Key Storage, Concurrency Limits, and Model List Delivery

**Status:** Accepted  
**Date:** 2026-06-17  
**Deciders:** Architect (Sonnet 4.6), Orchestrator  
**Context Issue:** XCT-266 (parent: XCT-265 — 多租户改造 BYOK + 多 world 并发)

---

## Context

WorldSeed (xct-genesis) is currently single-tenant: one global FastAPI `app.state.engine`, all LLM keys read from `os.environ`, no auth layer, and no isolation between users. The multi-tenant goal is:

> A user logs into `xcity.one`, gets their tokenhub API key + accessible model list, then launches an isolated world at `genesis.xcity.one` using their own token — without affecting other users.

Before implementing Auth middleware, Frontend BYOK, or multi-session engine, four cross-cutting architectural decisions must be locked to avoid rework.

---

## Decision 1: xcity.one ↔ Genesis Trust Model

### Decision

**Option (a) — xcity.one signs a short-lived JWT; Genesis validates via JWKS endpoint.**

xcity.one exposes `GET /.well-known/jwks.json`. Genesis caches the JWKS (TTL ≈ 1 h) and validates incoming `Authorization: Bearer <jwt>` on every protected route. Token lifetime: 15 min, with a silent refresh flow in the frontend.

### Rationale

- **Speed to ship:** Implementation fits in ~1 day (FastAPI `python-jose` or `PyJWT`, one JWKS fetch, one middleware). OAuth Authorization Code Flow requires a full authorization server, consent UI, token storage, and redirect handling — at least one week.
- **Fits the trust boundary:** xcity.one already owns the user identity. Genesis only needs to confirm "this user is who xcity.one says they are" — JWT signing covers that without an additional round-trip.
- **Operationally simple:** No shared database, no session affinity required. Genesis is stateless with respect to auth.
- **Industry standard for service-to-service within the same product:** JWT + JWKS is the pattern used by Supabase, Clerk, and Auth0 for embedding trust across subdomains.

### Counter-Arguments

| Concern | Response |
|---|---|
| **No revocation** — a compromised token is valid until expiry. | Mitigated by short TTL (15 min). Revocation infrastructure (Redis blocklist or token introspection) is a Phase 2 add-on; adding it does not require changing this decision. |
| **Key rotation complexity** — if xcity.one rotates its signing key, Genesis must detect the change. | JWKS caching with a short TTL (≤ 1 h) and a fallback re-fetch on `401` handles this without downtime. |
| **Option (c) shared cookie is simpler** | Requires xcity.one and genesis.xcity.one to share a `__Host-`-scoped cookie, which ties the two services to the same root domain forever. A JWT allows Genesis to become a separate-domain SaaS without re-architecting auth. |

### Impact

- **Auth middleware** (XCT-267): implement a FastAPI dependency `get_current_user(token: str = Depends(oauth2_scheme))` that validates the JWT and extracts `user_id` + `session_id`.
- **Frontend** (XCT-271): pass `Authorization: Bearer <jwt>` on all Genesis API calls; xcity.one must expose a JWKS endpoint.
- **No database changes** in Genesis for Phase 1.

### Phase 2 Upgrade Path

Add OAuth 2.0 Authorization Code Flow when revocation or third-party IdP support is needed. The JWT validation middleware is unchanged — only the token issuance path moves from "xcity.one stamps it" to "an OIDC-compliant authorization server stamps it."

---

## Decision 2: User tokenhub Key Storage

### Decision

**Option (a) — Key is never stored server-side. It lives in an HttpOnly, Secure, SameSite=Lax session cookie set by xcity.one and forwarded to Genesis.**

Flow:
1. xcity.one authenticates the user and retrieves their tokenhub key.
2. xcity.one sets an HttpOnly + Secure + SameSite=Lax cookie (or encodes the key inside the JWT claim with `key` field, sent only over HTTPS).
3. Genesis reads the key from the JWT payload or a separate encrypted cookie on each request and injects it as a per-request header when calling tokenhub/LiteLLM — never persisting it.

### Rationale

- **Zero storage attack surface:** A key that is never written to disk, a database, or a cache cannot be exfiltrated via SQL injection, Redis dump, or log scraping.
- **Principle of least privilege:** Genesis is a world engine, not a key vault. Delegating key custody to the user's browser session is architecturally correct.
- **Phase 1 complexity budget:** KMS (AWS KMS, Google Cloud KMS, Fernet with key rotation) adds infrastructure provisioning and secret management that is out of scope for a 1-week Phase 1.
- **HttpOnly + Secure + SameSite=Lax** eliminates XSS key theft and mitigates CSRF — acceptable risk for a cookie that scopes to `*.xcity.one`.

### Counter-Arguments

| Concern | Response |
|---|---|
| **Cookie theft via physical access / network** | SameSite=Lax + HTTPS + HttpOnly provides defense-in-depth. A stolen cookie is as dangerous as a stolen JWT — same threat model. |
| **Key visible in JWT payload** | Encode the key inside the JWT only if the token itself is treated as a secret (HTTPS + short TTL). Alternatively, put the key in a separate HttpOnly cookie and never in the JWT body. Prefer the separate cookie. |
| **No audit trail of key usage** | tokenhub itself logs API calls. Genesis can forward the key but does not need to log it. |
| **Option (b) KMS is more production-grade** | Agreed — KMS is the right long-term answer. It is explicitly earmarked for Phase 2. Starting with (b) in Phase 1 adds 1–2 weeks of infrastructure work for a security property that is not the Phase 1 gating concern. |
| **Option (c) re-enter key each time** | Poor UX. A user who must paste their API key every time they start a world will not adopt the feature. |

### Impact

- **Credential per-request injection** (XCT-268): the DM provider, Gazette, and Narrator must accept an optional `api_key` parameter sourced from the request context, not from `os.environ`.
- **Server-side:** no new tables, no KMS setup, no key rotation logic.
- **Frontend** (XCT-271): xcity.one must set the cookie on login; Genesis frontend must not read or log the cookie (HttpOnly enforces this).

### Phase 2 Upgrade Path

Move to server-side encrypted storage (Fernet or KMS-wrapped AES-256) when:
- Users want persistent worlds that restart without re-authentication.
- Compliance requirements (SOC 2, GDPR) mandate server-side key audit trails.
- The session cookie lifetime becomes a user friction point.

The per-request injection interface established in Phase 1 is forward-compatible: replace `cookie_key` with `kms_resolved_key` without changing the DM/Gazette/Narrator call sites.

---

## Decision 3: Per-User Active World Limit

### Decision

**Limit = 1. Starting a new world automatically shuts down the user's existing world.**

Behavior:
1. `POST /api/world/start` extracts `user_id` from the JWT.
2. If `session_store[user_id]` already contains an active engine, call `engine.shutdown()` and remove it.
3. Start the new engine, store it under `session_store[user_id]`.

### Rationale

- **Resource model simplicity:** Each running world holds asyncio tasks, an LLM call budget, and optionally an OpenClaw subprocess. Allowing N concurrent worlds per user requires LRU eviction policy, per-user resource quotas, and a scheduler — all out of scope for Phase 1.
- **Matches the use case:** The Phase 1 user story is "I want to run my world." Simultaneous multi-world is a power-user feature that can be unlocked when there is a billing/quota model to gate it.
- **Memory safety:** With a global `Dict[user_id, Engine]`, a leaked reference from a shutdown engine can cause subtle bugs. A hard limit of 1 simplifies the invariant: `len(session_store[user_id]) <= 1` is always true.
- **Predictable cost:** Each world consumes LLM tokens. Limiting to 1 prevents a single user from accidentally spawning runaway worlds.

### Counter-Arguments

| Concern | Response |
|---|---|
| **Users lose in-progress world state** | Phase 1 persistence (`stream.jsonl` + `state.json`) lets users resume. The shutdown is data-preserving, not data-destructive. |
| **Power users want multiple worlds** | Agreed. Phase 2 adds N-world support with LRU eviction and resource limits. The data model (`Dict[user_id, List[Engine]]`) is a trivial extension of the Phase 1 `Dict[user_id, Engine]`. |
| **Auto-shutdown is surprising UX** | Frontend must warn: "You already have an active world. Starting a new one will close it." Explicit user confirmation mitigates surprise. |

### Impact

- **Multi-session engine refactor** (XCT-269): change `app.state.engine: WorldEngine` → `app.state.sessions: Dict[str, WorldEngine]`, keyed by `user_id`. Add `start_world(user_id, config)` and `stop_world(user_id)` helpers.
- **All routes** that currently access `app.state.engine` must instead call `get_engine_for_user(user_id)`, raising `404` if no active world exists.
- **Persistence namespacing** (XCT-270): run directory moves from `~/.worldseed/runs/{run_id}/` → `~/.worldseed/users/{user_id}/runs/{run_id}/`.

### Phase 2 Upgrade Path

When multi-world support is needed:
1. Change `Dict[user_id, WorldEngine]` → `Dict[user_id, Dict[world_id, WorldEngine]]`.
2. Add LRU eviction: when `len(worlds) >= user.max_worlds`, evict the least-recently-active.
3. Add per-user resource quota enforcement (max concurrent ticks, max LLM calls/min).
4. Expose `GET /api/worlds` so users can see and switch between active worlds.

---

## Decision 4: xcity.one Model List Delivery to Genesis

### Decision

**Option (a) — Genesis fetches the model list in real-time from tokenhub using the user's API key.**

On `GET /api/settings` (or at world start), Genesis calls `tokenhub.xcity.one/models` with `Authorization: Bearer <user_key>` and returns the response to the frontend. Results may be cached per-user for up to 5 minutes to reduce latency.

### Rationale

- **Accuracy:** A user's accessible model list can change (subscription upgrade/downgrade, key rotation) between sessions. A real-time fetch reflects the current state; a JWT claim reflects the state at token issuance.
- **No clock-skew problem:** JWT claims expire. If the user's model list is embedded in a 15-min token, it is stale for up to 14:59 after a plan change. With real-time fetch, the model list is always current.
- **Decoupling:** Genesis does not need to know xcity.one's token payload format or schema version. The tokenhub API is the single source of truth.
- **Latency is acceptable:** One extra HTTP call at world-start (or settings load) with a 5-min client-side cache adds <100 ms in practice. This is not a hot path.

### Counter-Arguments

| Concern | Response |
|---|---|
| **tokenhub availability dependency** | If tokenhub is down, Genesis cannot start a world. Mitigated by: (1) cache the last-known model list in the session; (2) fall back to a configurable default model list. Neither requires changing this decision. |
| **Option (b) JWT claim is faster** | True for the common case. But when a user upgrades their plan, they must re-login to get a new JWT for the model list to update — confusing UX. The 5-min real-time cache achieves nearly the same latency with no stale-data risk. |
| **Extra network call per world start** | Acceptable. This is a one-time call at session initialization, not a per-tick hot path. |

### Impact

- **Settings endpoint** (XCT-271 / frontend): `GET /api/settings` proxies `tokenhub /models` using the user's key from the session. Genesis must propagate the user's tokenhub key to this call.
- **No changes to JWT structure:** xcity.one does not need to embed model data in its tokens.
- **Error handling:** `GET /api/settings` returns a graceful error (with cached fallback list) if tokenhub is unreachable.

### Phase 2 Upgrade Path

If latency becomes a concern (e.g., mobile on slow networks):
- Pre-fetch model list during xcity.one login and embed a snapshot in the JWT claim.
- Genesis accepts the JWT claim as a fast-path fallback, but still re-validates against tokenhub on world start.

---

## Summary

| # | Decision | Chosen Option | Phase 2 Path |
|---|---|---|---|
| 1 | xcity.one ↔ Genesis trust | JWT + JWKS, 15-min TTL | OAuth 2.0 Authorization Code Flow |
| 2 | User tokenhub key storage | Never stored; HttpOnly session cookie | Server-side KMS-wrapped storage |
| 3 | Per-user active world limit | 1 (auto-close old on new start) | N worlds with LRU eviction + quota |
| 4 | Model list delivery | Genesis real-time fetch from tokenhub | JWT claim fast-path with tokenhub validation |

## Blocked / Unblocked Issues

**Blocked by this ADR (can now proceed):**
- XCT-267: Auth middleware (JWT validation in FastAPI)
- XCT-271: Frontend BYOK + SSO integration

**Not blocked (can proceed in parallel):**
- XCT-269: Multi-session engine refactor
- XCT-268: Credential per-request injection (DM / Gazette / Narrator)
- XCT-270: Persistent per-user namespace
- In-process Python agent runtime
