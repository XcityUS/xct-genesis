# 用 Codex 跑 WorldSeed

这篇文档解释现在这套 Codex for WorldSeed 是怎么用的，重点是
`worldseed run`、`worldseed codex-runner`、workspace、`ws.py` 和可选的
`scene.codex` 配置之间的关系。

## 一句话模型

```text
YAML 定义世界规则和角色
worldseed run 启动 WorldSeed server 和 workspace
codex-runner 唤醒 Codex agent
Codex agent 读 AGENT.md/scenario.yaml，写文件，调用 ws.py act
WorldSeed engine 校验 action，更新 state，记录 stream
```

Codex 是执行载体。WorldSeed agent 不是一个独立进程，而是一套角色、目标、
上下文、可用 action 和 world state。`codex-runner` 把这些东西组织成一次
Codex activation，让 Codex 作为该 agent 的身体去感知、写作、提交动作。

## 核心文件关系

### `configs/*.yaml`

场景 YAML 是业务规则所在的位置。它定义：

- `scene`: 场景元信息、tick、运行模式、可选 `scene.codex`
- `entities`: world state 里的初始对象
- `agents`: agent 角色、目标、drives、边界
- `actions`: agent 可以提交的动作、参数、前置条件、effects、events

例如 autowrite 的写作流程、版本制度、critic 打回规则、final deliverables
要求，都应该写在 `configs/auto_copywriter.yaml`，不应该写进
`codex_runner.py`。

### `src/worldseed/cli/run.py`

`worldseed run ...` 的入口。它负责：

- 读取场景 YAML
- 创建 `WorldEngine`
- 启动 FastAPI server
- 启动自动 tick runner
- 调用 `init_workspace(...)`
- 注册 YAML 中声明的 agents
- 打印每个 agent 的启动提示

它只负责把世界跑起来，不负责让 researcher/writer/critic 真的干活。

### `src/worldseed/cli/_workspace.py`

workspace 脚手架。它负责生成：

```text
workspace/
  manifest.json
  scenario.yaml
  trajectory.md
  story.md
  ws.py
  shared/
  agents/{agent_id}/
    AGENT.md
    status.json
    workspace/
    scratch/
    files/
```

`AGENT.md` 是从 YAML 里的 agent character 和 action schema 生成的。它告诉
Codex agent 当前角色是谁、可做哪些 action、文件应该写到哪里。

### `src/worldseed/cli/codex_runner.py`

Codex runner 是 agent activation 的执行器。它负责：

- 读取 workspace 的 `manifest.json`
- 读取 workspace 的 `scenario.yaml`
- 读取可选 `scene.codex`
- 构造 activation prompt
- 用 `codex exec` 启动一个或多个 Codex agent
- 设置环境变量：
  - `WORLDSEED_WORKSPACE`
  - `WORLDSEED_AGENT_ID`
  - `WORLDSEED_URL`
- 等 Codex agent 自己调用 `ws.py perceive` 和 `ws.py act`

它不应该判断 autowrite 文案好不好，也不应该知道哪个平台需要什么文案。这些
是 YAML 和 agent prompt 的职责。

### `scripts/worldseed_agent.py` / workspace `ws.py`

`ws.py` 是 agent 和 WorldSeed server 的桥。`_workspace.py` 会把
`scripts/worldseed_agent.py` 复制到 workspace 里。

Codex agent 通过它和 engine 通信：

```bash
python3 "$WORLDSEED_WORKSPACE/ws.py" perceive
python3 "$WORLDSEED_WORKSPACE/ws.py" act ACTION key=value
python3 "$WORLDSEED_WORKSPACE/ws.py" status --state working --focus "..."
python3 "$WORLDSEED_WORKSPACE/ws.py" publish ACTION --lane FILE.jsonl --row '{...}' key=value
```

`perceive` 返回当前 world state、inbox、legal `action_options`。`act` 提交一个
action，由 engine 根据 YAML 做校验和状态更新。

## 最小运行方式

先启动世界：

```bash
uv run worldseed run configs/auto_copywriter.yaml \
  --workspace /tmp/worldseed-auto-writer \
  --run-id auto-writer \
  --port 8031 \
  --force
```

然后用 Codex runner 唤醒某个 agent：

```bash
uv run worldseed codex-runner \
  --workspace /tmp/worldseed-auto-writer \
  --url http://127.0.0.1:8031 \
  --agents researcher \
  --max-cycles 1 \
  --tick-mode auto \
  --agent-timeout 360 \
  --signal-timeout 0.2 \
  --dangerous-bypass
```

在 `--tick-mode auto` 下，tick 由 server 后台自动推进。runner 不手动改 world
state，只负责在合适的时间唤醒 Codex agent。

典型 autowrite 单 baton 跑法：

```text
researcher  -> research_context
strategist  -> set_strategy
writer      -> produce_artifact v1
critic      -> critique_artifact
writer      -> produce_artifact v2 if revised
critic      -> critique_artifact accept/revise
editor      -> decide_next finalize
```

真正下一棒是谁不应该写死在 Python。它由当前 world state 里的
`workflow.assignee`、legal `action_options` 和 agent 自己的 routing 判断决定。

## 默认模式和 `scene.codex`

`codex-runner` 一定会读取场景 YAML 里的 `scene.codex`：

```yaml
scene:
  id: ...
  codex:
    ...
```

但 `scene.codex` 是可选的。如果没有配置，runner 走默认行为：

- Codex 的 cwd 是 repo root
- 只注入通用 `WORLDSEED_*` 环境变量
- 不加额外场景 env hint
- 不加额外 activation instructions
- 不启用 async refresh
- edit scope 使用通用规则：agent 私有目录 + action schema 要求的 `shared/`

这就是 autowrite 当前的模式。autowrite 只需要写 Markdown、提交 action、读
shared handoff，因此默认行为够用。

## 什么时候需要 `scene.codex`

只有当场景需要特殊 Codex 运行环境时，才应该加 `scene.codex`。

常见需求：

- 每个 agent 需要不同 cwd
- 每个 agent 需要独立 git worktree
- agent 需要额外环境变量
- agent prompt 需要额外运行说明
- 场景有后台异步任务，完成后需要刷新 world state
- edit scope 不能只用默认 `agents/<id>/` 和 `shared/`

例如 autoresearch 需要运行实验代码，所以它配置了 per-agent git worktree：

```yaml
scene:
  codex:
    cwd:
      mode: git_worktree_per_agent
      root_env: AUTORESEARCH_WORKSPACE
      main_subdir: main
      worktrees_subdir: worktrees
      base_ref: baseline
      branch_prefix: codex/
    env:
      AUTORESEARCH_WORKSPACE: "{cwd_root}"
      AUTORESEARCH_AGENT_WORKTREE: "{agent_cwd}"
    env_hint: |
      AUTORESEARCH_WORKSPACE={cwd_root}
      AUTORESEARCH_AGENT_WORKTREE={agent_cwd}
      Scene code workspace for code actions: {agent_cwd}/train_gpt.py
    edit_scope_hint: |
      Do not edit files outside your own WorldSeed lane and your own git worktree:
      {workspace}/agents/{agent_id}/
      {agent_cwd}
```

这类配置是运行环境层，不是业务判断层。autoresearch 的论文、实验、评审规则
仍然应该在 YAML actions 和 agent drives 里表达。

## Autowrite 为什么不需要额外定制

autowrite 的核心工作是内容协作：

- researcher 写 `shared/handoffs/` 里的研究 brief
- strategist 写 strategy handoff
- writer 写版本化 content artifact
- critic 写 review handoff 和结构化原因
- editor 写 final pack 和 `shared/deliverables/*.md`

这些都可以在默认 workspace 模型里完成：

```text
agents/{id}/workspace/   私有草稿
agents/{id}/scratch/     临时笔记
shared/handoffs/         agent 之间交接的公开 Markdown
shared/deliverables/     用户真正关心的最终 Markdown
stream.jsonl             机器历史和审计，不是主要协作文档
```

所以 autowrite 不需要配置 `scene.codex.cwd`、`scene.codex.env` 或
`scene.codex.async_refresh`。如果以后 autowrite 要让某个 agent 操作外部 repo、
调用专属工具目录或绑定外部进程，再加 `scene.codex`。

## 文件产物约定

对于文档型场景，推荐这个约定：

```text
shared/handoffs/
  research_*.md
  strategy_*.md
  artifact_*_v1.md
  review_*_v1.md
  artifact_*_v2.md
  decision_*.md
  final_pack_*.md

shared/deliverables/
  jike.md
  xiaohongshu.md
  x_thread.md
```

`shared/handoffs/` 是过程产物，agent 会读。`shared/deliverables/` 是用户面
产物，应该只有可直接发布或复制的正文，不要混入 metadata、版本说明、critic
意见、内部 checklists。

JSONL 适合作为 stream、audit、index，不应该替代主要 Markdown artifact。

## 并行和单 baton

`codex-runner` 支持 `--parallel`，系统层面可以同时启动多个 Codex agent。

但是否并行是场景设计问题：

- autoresearch 适合并行，因为多个研究员可以独立跑实验、写论文、互评
- autowrite 当前更适合单 baton，因为 research -> strategy -> draft -> critique
  有明确依赖，过早并行会制造重复稿和不清楚的最终交付

如果场景要并行，YAML 里应该有清晰的分支对象、合并 action、选择 action 和
冲突处理规则。不要只靠 runner 的 `--parallel` 让 agent 同时醒来。

## 调试顺序

遇到问题时按这个顺序查：

1. 验证 YAML：

   ```bash
   uv run worldseed validate configs/auto_copywriter.yaml --ticks 0
   ```

2. 确认 server 活着：

   ```bash
   curl -s http://127.0.0.1:8031/health
   ```

3. 看当前 state：

   ```bash
   curl -s http://127.0.0.1:8031/api/runs/<run_id>/state
   ```

4. 看 agent 是否有 legal action：

   ```bash
   WORLDSEED_WORKSPACE=/tmp/worldseed-auto-writer \
   WORLDSEED_AGENT_ID=writer \
   WORLDSEED_URL=http://127.0.0.1:8031 \
   python3 /tmp/worldseed-auto-writer/ws.py perceive
   ```

5. 看 runner 输出和 `.codex-runner/`：

   ```text
   workspace/.codex-runner/{agent}-{timestamp}.txt
   ```

6. 看持久化 stream：

   ```text
   $WORLDSEED_HOME/runs/<run_id>/stream.jsonl
   ```

如果 agent 提交失败，通常先看 action params 是否缺 required 字段、precondition
是否不满足、路径是否不是 workspace-relative、文件是否没有先写出来。

## 设计原则

- Python runner 只做通用 runtime wiring。
- 业务流程、角色压力、质量门、打回原因、版本制度写在场景 YAML。
- Agent 之间通过 world state 和 public Markdown handoff 协作，不读别人的私有
  workspace。
- `codex-runner` 唤醒 agent，但 agent 必须自己 `perceive`、判断 legal action、
  写文件、提交 `act`。
- 自动 tick 是 engine 负责的，不要为了 e2e 手动改 state。
- `scene.codex` 是特殊运行环境配置，不是每个场景都需要。
