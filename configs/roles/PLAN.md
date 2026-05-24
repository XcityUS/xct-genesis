# Plan: Create role YAMLs for XCITY Genesis (4 base roles)

## Context

The user is bootstrapping XCITY Genesis's product-level role system. The reference
spec (`XCITY Genesis产品设计、切入点和有趣化.md` §5.1) defines a **Role Stack**:
users are not single-job; multiple roles stack based on assets, behavior, and
governance status. This task creates structured YAML definitions for the first
4 base roles so downstream code (frontend role badges, permission gates,
progression rules, agent-spawning, etc.) has a canonical source of truth.

Roles to create:
1. Citizen 城市公民 — base identity, granted on signup
2. Land User 土地使用者 — holds land-use NFTs at 7 granularities (1㎡ → 1000 Acre+)
3. Solar Builder 光伏建设者 — photovoltaic, storage, dispatch
4. Agro Operator 农业经营者 — planting, livestock, vineyards, carbon farming

These are **product roles**, not WorldSeed scene agents. The existing
`configs/*.yaml` files (`teahouse.yaml`, `ai_layoffs.yaml`, `template.yaml`) are
WorldSeed scene configs — a different schema. Role YAMLs belong in a new
subdirectory.

## Approach

- Create new directory: `configs/roles/`
- One YAML per role, filename = `<role_id>.yaml` (snake_case English)
- Consistent schema across all 4 files (and extensible to roles #5–17 later)

### Schema (top-level keys)

```yaml
role:
  id: <snake_case>
  name: { en: <English>, zh: <中文> }
  tier: base | advanced
  prerequisite: <role_id> | none
  description: <2-4 lines>

identity:        # only for citizen; the digital identity layer
holdings:        # only for asset-holding roles (land_user); granularity table
asset:           # what NFT / on-chain instrument represents this role's stake
domain:          # role-specific domain knowledge (e.g. solar_builder.tech_stack)
permissions:
  may_act: [<verb>]
  may_hold: [<asset_type>]
progression:
  unlocks:
    - role: <role_id>
      condition: <DSL-ish predicate>
economics:       # optional reference prices (KWH / USDC equivalents from §8)
```

Schema is intentionally **declarative + free-form** (no engine reads these yet);
the goal is a stable contract for future consumers. Field naming follows the
existing repo's English-id-with-Chinese-comments convention seen in
`configs/template.yaml`.

## Files to create

- `configs/roles/citizen.yaml`         ✅ already written
- `configs/roles/land_user.yaml`       ✅ already written
- `configs/roles/solar_builder.yaml`   ⏳ pending
- `configs/roles/agro_operator.yaml`   ⏳ pending

### Content sources (from reference doc)

- **Solar Builder** (§5.1.3, §6.1.2, §7.1, §8): 1㎡ photovoltaic / community solar /
  power stations / storage (BESS) / dispatch / supply chain. Pricing reference:
  1㎡ solar plot = 100 KWH (10 USDC); 1 Acre = 50,000 KWH (5,000 USDC).
- **Agro Operator** (§5.1.4, §6.1.2): planting, livestock, greenhouse, vineyards,
  olive groves, pistachios, medicinal herbs, carbon-sink farming. Pricing:
  basic agro agent = 50 KWH/month (5 USDC/month).

## Files NOT to create (out of scope, but plan extensible)

Roles #5–17 (Mining, Compute, Industrial, Trader, Agent Manager, Robot Operator,
Governor, Project Sponsor, GP, LP, IPO Partner, Strategic Partner, City Developer)
— same schema applies; create later when needed.

## Verification

- Lint each YAML with `python -c "import yaml; yaml.safe_load(open(p))"` for the
  4 files.
- Confirm cross-references resolve: e.g., `citizen.progression.unlocks[].role`
  matches existing role ids.
- Visual sanity-check: `ls configs/roles/` shows 4 files; diff sizes are
  comparable (no single file should be >2× the others — indicates inconsistent
  depth).

## Not in scope

- No code (no parser, no permission engine, no progression evaluator).
- No commit / push — purely file creation in working tree.
- No edits to existing `configs/*.yaml` scene files.
