# Genesis 多租户设计文档

> 调研结论与整体设计 — 对应 XCT-265 (多租户改造 — BYOK + 多 world 并发)
> 架构决策详见 [docs/adr/0001-multi-tenant-trust-model.md](adr/0001-multi-tenant-trust-model.md)

---

## 目标用户故事

用户在 `xcity.one` 登录 → 拿到自己的 tokenhub API key + 可访问 model list → 带着这些去 `genesis.xcity.one` 启一个独立 world，跑自己的 token、不影响别人。

---

## 现状（单租户）缺陷分析

| 缺陷 | 位置 | 风险 |
|------|------|------|
| `app.state.engine` 全局单例 | `main.py` | 并发 `/api/world/start` 互相覆盖 |
| LLM key 来自 `os.environ` | 所有 LLM client 初始化 | 所有用户共享同一 key，计费混乱 |
| 无 auth 层 | 所有 `/api/` 路由 | 公网任意访问 |
| `~/.worldseed/runs/{run_id}/` 平铺 | 文件系统 | `/api/past-runs` 对所有人可见 |
| OpenClaw API key 烤进配置文件 | `~/.openclaw/openclaw.json` | 全 agent 共享单 key |

---

## Phase 1 架构（～1 周）

目标：跑通「登录 → 启 world → 我的 token 被消耗」链路。

### 整体流程

```
用户浏览器                xcity.one              genesis.xcity.one
    │                        │                          │
    ├─── 登录 ──────────────►│                          │
    │◄── JWT + tokenhub_key  │                          │
    │    (HttpOnly cookie)   │                          │
    │                        │                          │
    ├─── POST /api/world/start ─────────────────────────►│
    │    Authorization: Bearer <jwt>                     │
    │    Cookie: tokenhub_key=<key>                      │
    │                        │                   verify_jwt(jwt)
    │                        │                   ↓ JWKS endpoint
    │                        │                   extract user_id
    │                        │                   GET tokenhub/models (with key)
    │                        │                   evict_existing(user_id)
    │                        │                   create Engine(session_id, key)
    │◄─── {session_id, models} ──────────────────────────│
    │                        │                          │
    ├─── WebSocket /ws/{session_id} ─────────────────────►│
    │    (world interaction)  │                          │
```

### 组件变更

#### 1. Auth Middleware（XCT-267）

```python
# genesis/middleware/auth.py
async def verify_jwt(token: str) -> UserClaims:
    # 启动时从 xcity.one/.well-known/jwks.json 拉取，5 min cache
    jwks = await get_jwks(settings.XCITY_JWKS_URL)
    payload = jwt.decode(token, jwks, algorithms=["RS256"])
    return UserClaims(user_id=payload["sub"], email=payload["email"])

async def auth_middleware(request: Request, call_next):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not token:
        raise HTTPException(401)
    request.state.user = await verify_jwt(token)
    request.state.tokenhub_key = request.cookies.get("tokenhub_key")
    return await call_next(request)
```

#### 2. 凭证 per-request 注入（XCT-268）

DM / Gazette / Narrator 的 LLM 调用从全局 env key 改为从 `request.state.tokenhub_key` 注入：

```python
# 旧
client = openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

# 新
client = openai.AsyncOpenAI(
    api_key=session.tokenhub_key,
    base_url=settings.TOKENHUB_BASE_URL,  # https://tokenhub.xcity.one/v1
)
```

#### 3. 多 session engine（XCT-269）

```python
# genesis/engine_registry.py
class EngineRegistry:
    _engines: Dict[str, Engine] = {}  # session_id -> Engine
    _user_sessions: Dict[str, str] = {}  # user_id -> session_id

    async def start(self, user_id: str, session_id: str, key: str) -> Engine:
        # limit=1: evict existing world for this user
        if user_id in self._user_sessions:
            old_sid = self._user_sessions[user_id]
            await self.teardown(old_sid, checkpoint=True)
        engine = Engine(session_id=session_id, tokenhub_key=key)
        self._engines[session_id] = engine
        self._user_sessions[user_id] = session_id
        return engine

    async def teardown(self, session_id: str, checkpoint: bool = False):
        engine = self._engines.pop(session_id, None)
        if engine and checkpoint:
            await engine.save_checkpoint()  # → per-user 命名空间
        if engine:
            await engine.shutdown()
```

`app.state.engine` 全局单例替换为 `app.state.registry = EngineRegistry()`。

#### 4. per-user 命名空间（XCT-270）

```
~/.worldseed/runs/
  {user_id}/
    {run_id}/
      checkpoint.json
      logs/
```

`/api/past-runs` 从 `request.state.user.user_id` 过滤，只返回本人记录。

#### 5. In-process Python agent runtime（XCT-271）

OpenClaw 以子进程方式运行，API key 烤进配置无法 per-user 注入。Phase 1 替换策略：

```python
# genesis/runtime/inprocess.py
class InProcessRuntime:
    """asyncio 内运行 agent loop，key 从 session context 注入。"""

    def __init__(self, session: WorldSession):
        self._session = session

    async def run_agent(self, agent_type: str, prompt: str) -> AsyncIterator[str]:
        client = openai.AsyncOpenAI(
            api_key=self._session.tokenhub_key,
            base_url=settings.TOKENHUB_BASE_URL,
        )
        async for chunk in await client.chat.completions.create(
            model=self._session.selected_model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        ):
            yield chunk.choices[0].delta.content or ""
```

#### 6. 前端 BYOK + SSO（XCT-272）

1. 用户在 xcity.one 登录 → xcity.one 设置 JWT cookie（根域 `.xcity.one`，`HttpOnly=False` 供 JS 读取）和 tokenhub_key cookie（`HttpOnly=True`）。
2. genesis.xcity.one 前端在 API 请求中附带 JWT header。
3. BYOK 流程：如用户有自己的 key，可在 genesis 前端输入，genesis 后端更新 session cookie（不写入 xcity.one）。

---

## Phase 2 路线（～1-2 周，待 Phase 1 稳定后）

| 子任务 | 方向 |
|--------|------|
| 信任模型升级 | JWT → OAuth Authorization Code Flow（支持 token revocation） |
| Key 落库 | session cookie → KMS + 加密存库（支持跨设备） |
| World 并发 | limit=1 → LRU(N) + 资源 quota |
| OpenClaw 多租户 | 独立决策（XCT-273）：协议扩展 vs 弃用 |
| Model list 缓存 | Redis cache(TTL=5min) + xcity.one invalidation hook |

---

## 安全考量

| 威胁 | 缓解 |
|------|------|
| JWT 伪造 | JWKS 验签（RS256），Genesis 不接受 HS256 |
| tokenhub_key 泄露 | HttpOnly cookie，不落数据库，短 TTL session |
| CSRF | SameSite=Lax + POST 请求需 JWT（跨站无法携带 Authorization header） |
| World 资源滥用 | per-user limit=1，CPU/memory cgroup（Phase 2） |
| past-runs 越权 | user_id 过滤，不暴露他人 run_id |

---

## 子任务与 ADR 依赖关系

```
XCT-266 ADR ──────────────────────────►  XCT-267 Auth middleware
(信任模型决策)                            XCT-272 Frontend BYOK

XCT-269 多 session engine (可并行)
XCT-268 凭证 per-request 注入 (可并行)
XCT-270 per-user 命名空间 (可并行)
XCT-271 In-process runtime (可并行)

以上全部 done → XCT-274 MULTI_TENANT.md 更新最终状态
```

---

*最后更新: 2026-06-17 by Architect (XCT-266)*
