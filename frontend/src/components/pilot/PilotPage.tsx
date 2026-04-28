import { useEffect, useMemo, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { Archive, FileText, Flask, GitBranch, NotePencil, Table } from '@phosphor-icons/react'
import { Button } from '@/components/ui/button'
import { PILOT_DATA, PILOT_DATASETS, type PilotBranch, type PilotDataset, type PilotDeliverableKind, type PilotRelation, type PilotStatus, type PilotVersion } from './pilot-data'
import { fetchLatestAutoresearchPilotDataset } from './pilot-run-data'
import '@/styles/pilot.css'

type GraphStatus = PilotStatus | 'collapsed'

type GraphVersion = PilotVersion & {
  index: number
}

type GraphBranch = Omit<PilotBranch, 'status' | 'versions'> & {
  id: string
  index: number
  status: GraphStatus
  hiddenCount?: number
  isCollapsed?: boolean
  versions: GraphVersion[]
  visibleVersions?: GraphVersion[]
}

interface GraphModel {
  shape: PilotDataset['branchMap']['shape']
  branches: GraphBranch[]
  visibleBranches: GraphBranch[]
  visibleVersions: GraphBranch[]
  mode: 'full' | 'scroll' | 'dense-scroll' | 'collapsed'
  hidden: { branches: number; versions: number; descendants: number }
  scale: {
    branchCount: number
    versionCount: number
    maxVersionsPerBranch: number
  }
}

const GRAPH_LIMITS = {
  wideFullBranches: 5,
  wideDenseBranches: 18,
  deepFullVersions: 6,
  deepKeyVersions: 6,
  mixedFullVersionsPerLane: 6,
  mixedKeyVersionsPerLane: 5,
  treeFullNodes: 36,
  treeMaxDepth: 3,
  treeMaxChildrenPerNode: 4,
}

function statusClass(status: GraphStatus) {
  return `pilot-status-${status}`
}

function stripTitle(title: string) {
  return title.replace(/^(Branch|Angle)\s+\w+\s*·\s*/, '').replace(/^[\w]+\s*·\s*/, '')
}

function branchStats(branch: { attempts?: string; result?: string }) {
  const parts: string[] = []
  const exp = branch.attempts?.match(/(\d+)\s*次\s*experiment/)
  const paper = branch.attempts?.match(/(paper_\d+)/)
  const delta = branch.result?.match(/Δ\s*val_loss[\s=]*([-+]?\d*\.?\d+(?:\s*~\s*[-+]?\d*\.?\d+)?)/)
  if (exp) parts.push(`实验 ${exp[1]}`)
  if (paper) parts.push(`→ ${paper[1]}`)
  if (delta) parts.push(`Δ ${delta[1]}`)
  return parts.join(' · ')
}

function statusSummary(items: Array<{ status: PilotStatus }>) {
  const counts = items.reduce(
    (acc, item) => {
      acc[item.status] += 1
      return acc
    },
    { chosen: 0, killed: 0, parked: 0 },
  )
  return `${counts.chosen} chosen · ${counts.parked} parked · ${counts.killed} killed`
}

function shortenChange(text: string) {
  return text.replace(/\([^)]*\)\s*$/g, '').trim()
}

function detailHref(branch: Pick<GraphBranch | PilotBranch, 'letter'>) {
  return `#pilot-detail-${branch.letter}`
}

function snap4(value: number) {
  return Math.round(value / 4) * 4
}

function buildGraphModel(data: PilotDataset): GraphModel {
  const branches = data.branches.map((branch, index): GraphBranch => ({
    ...branch,
    id: branch.letter,
    index,
    parent: branch.parent || null,
    versions: (branch.versions || []).map((version, versionIndex) => ({ ...version, index: versionIndex })),
  }))
  const versionCount = branches.reduce((sum, branch) => sum + branch.versions.length, 0)

  return {
    shape: data.branchMap.shape,
    branches,
    visibleBranches: branches,
    visibleVersions: branches,
    mode: 'full',
    hidden: { branches: 0, versions: 0, descendants: 0 },
    scale: {
      branchCount: branches.length,
      versionCount,
      maxVersionsPerBranch: branches.reduce((max, branch) => Math.max(max, branch.versions.length), 0),
    },
  }
}

function compactGraphModel(data: PilotDataset): GraphModel {
  const model = buildGraphModel(data)
  if (model.shape === 'wide') {
    return {
      ...model,
      mode: model.branches.length > GRAPH_LIMITS.wideDenseBranches ? 'dense-scroll' : model.branches.length > GRAPH_LIMITS.wideFullBranches ? 'scroll' : 'full',
    }
  }

  if (model.shape === 'deep') {
    const compact = compactSequence(model.branches, GRAPH_LIMITS.deepFullVersions, GRAPH_LIMITS.deepKeyVersions)
    return {
      ...model,
      visibleVersions: compact.items,
      mode: compact.hidden ? 'collapsed' : 'full',
      hidden: { ...model.hidden, versions: compact.hidden },
    }
  }

  if (model.shape === 'mixed') {
    let hiddenVersions = 0
    const visibleBranches = model.branches.map(branch => {
      const compact = compactSequence(branch.versions, GRAPH_LIMITS.mixedFullVersionsPerLane, GRAPH_LIMITS.mixedKeyVersionsPerLane)
      hiddenVersions += compact.hidden
      return { ...branch, visibleVersions: compact.items }
    })
    return {
      ...model,
      visibleBranches,
      mode: hiddenVersions ? 'collapsed' : 'full',
      hidden: { ...model.hidden, versions: hiddenVersions },
    }
  }

  if (model.branches.length <= GRAPH_LIMITS.treeFullNodes) return model

  const compact = compactTree(model.branches)
  return {
    ...model,
    visibleBranches: compact.items,
    mode: 'collapsed',
    hidden: { ...model.hidden, descendants: compact.hidden },
  }
}

function compactSequence<T extends { index: number; status: GraphStatus; statusLabel: string; contentMeta?: string; diff?: unknown }>(items: T[], fullLimit: number, keyLimit: number) {
  if (items.length <= fullLimit) return { items, hidden: 0 }

  const first = items[0]
  const last = items[items.length - 1]
  const keyItems = items.slice(1, -1).filter(isKeySequenceItem)
  let visible = [first, ...keyItems, last]
    .filter((item, index, all) => all.findIndex(other => other.index === item.index) === index)
    .sort((a, b) => a.index - b.index)

  if (visible.length > keyLimit) {
    const middleSlots = Math.max(0, keyLimit - 2)
    const middle = visible
      .slice(1, -1)
      .sort((a, b) => keyPriority(b) - keyPriority(a) || a.index - b.index)
      .slice(0, middleSlots)
      .sort((a, b) => a.index - b.index)
    visible = [visible[0], ...middle, visible[visible.length - 1]]
  }

  return { items: visible, hidden: items.length - visible.length }
}

function isKeySequenceItem(item: { status: GraphStatus; statusLabel: string; contentMeta?: string; diff?: unknown }) {
  const label = `${item.statusLabel} ${item.contentMeta || ''}`.toLowerCase()
  return item.status === 'chosen'
    || item.status === 'parked'
    || Boolean(item.diff)
    || label.includes('final')
    || label.includes('accepted')
    || label.includes('退稿')
    || label.includes('撞')
}

function keyPriority(item: { status: GraphStatus; statusLabel: string; contentMeta?: string; diff?: unknown }) {
  let score = 0
  if (item.status === 'chosen') score += 5
  if (item.status === 'parked') score += 3
  if (item.diff) score += 2
  const label = `${item.statusLabel} ${item.contentMeta || ''}`.toLowerCase()
  if (label.includes('final') || label.includes('accepted')) score += 4
  if (label.includes('撞')) score += 3
  return score
}

function compactTree(branches: GraphBranch[]) {
  const children: Record<string, GraphBranch[]> = {}
  for (const branch of branches) {
    const parent = branch.parent || '__root__'
    children[parent] ||= []
    children[parent].push(branch)
  }

  const output: GraphBranch[] = []
  let hidden = 0

  function countDescendants(nodes: GraphBranch[]): number {
    return nodes.reduce((sum, node) => sum + 1 + countDescendants(children[node.id] || []), 0)
  }

  function collapsed(parent: string | null, count: number, index: number): GraphBranch {
    return {
      id: `${parent || 'root'}__collapsed_${index}`,
      index: branches.length + index,
      letter: '…',
      title: `${count} 个节点已折叠`,
      status: 'collapsed',
      statusLabel: `+${count}`,
      parent,
      thesis: '完整节点见「版本详情」',
      versions: [],
      hiddenCount: count,
      isCollapsed: true,
    }
  }

  function pushNode(node: GraphBranch, depth: number) {
    output.push(node)
    const kids = children[node.id] || []
    if (!kids.length) return

    if (depth >= GRAPH_LIMITS.treeMaxDepth) {
      const count = countDescendants(kids)
      hidden += count
      output.push(collapsed(node.id, count, output.length))
      return
    }

    const visibleKids = kids.slice(0, GRAPH_LIMITS.treeMaxChildrenPerNode)
    visibleKids.forEach(child => pushNode(child, depth + 1))
    const hiddenKids = kids.slice(visibleKids.length)
    if (hiddenKids.length) {
      const count = countDescendants(hiddenKids)
      hidden += count
      output.push(collapsed(node.id, count, output.length))
    }
  }

  const roots = children.__root__ || []
  const visibleRoots = roots.slice(0, GRAPH_LIMITS.treeMaxChildrenPerNode)
  visibleRoots.forEach(root => pushNode(root, 0))
  const hiddenRoots = roots.slice(visibleRoots.length)
  if (hiddenRoots.length) {
    const count = countDescendants(hiddenRoots)
    hidden += count
    output.push(collapsed(null, count, output.length))
  }

  return { items: output, hidden }
}

function Graph({ data }: { data: PilotDataset }) {
  const model = useMemo(() => compactGraphModel(data), [data])
  const [zoom, setZoom] = useState(1)

  const updateZoom = (next: number) => setZoom(Math.min(1.6, Math.max(0.55, Math.round(next * 100) / 100)))

  return (
    <div className="pilot-graph-viewport" style={{ '--pilot-graph-zoom': zoom } as CSSProperties}>
      <div className="pilot-graph-toolbar" aria-label="图谱缩放控制">
        <span>{graphToolbarLabel(model)}</span>
        <div>
          <button type="button" onClick={() => updateZoom(zoom - 0.1)} aria-label="缩小">-</button>
          <output>{Math.round(zoom * 100)}%</output>
          <button type="button" onClick={() => updateZoom(zoom + 0.1)} aria-label="放大">+</button>
          <button type="button" onClick={() => updateZoom(1)}>fit</button>
        </div>
      </div>
      <div className="pilot-graph-zoom-scroll">
        <div className="pilot-graph-zoom-inner">
          <GraphBody model={model} />
        </div>
      </div>
    </div>
  )
}

function GraphBody({ model }: { model: GraphModel }) {
  if (model.shape === 'wide') return <WideGraph model={model} />
  if (model.shape === 'deep') return <DeepGraph model={model} />
  if (model.shape === 'tree') return <TreeGraph model={model} />
  return <MixedGraph model={model} />
}

function graphToolbarLabel(model: GraphModel) {
  if (model.shape === 'wide') return `${model.scale.branchCount} branches`
  if (model.shape === 'deep') return `${model.scale.branchCount} versions`
  if (model.shape === 'tree') return `${model.scale.branchCount} nodes`
  return `${model.scale.branchCount} lanes · ${model.scale.versionCount} versions`
}

function GraphBranchCardContent({ branch, typeLabel }: { branch: GraphBranch; typeLabel?: string }) {
  const kind = branch.kind || inferDeliverableKind(branch)
  const CardIcon = deliverableIcon(kind)
  const metric = compactMetric(branch.result || branch.attempts || branch.decision || 'no metric')
  const anchor = branch.createdTick === undefined ? branch.letter : `#t${branch.createdTick}`
  return (
    <>
      <div className="pilot-key-card-head">
        <span className="pilot-key-tick">{anchor}</span>
        <span className="pilot-key-type">{typeLabel || kindLabel(kind)}</span>
      </div>
      <div className="pilot-key-title-row">
        <span className="pilot-key-row-icon" aria-hidden="true">
          <CardIcon weight="regular" />
        </span>
        <h3>{branch.isCollapsed ? branch.title : readableDeliverableName(branch)}</h3>
      </div>
      <div className="pilot-key-row-meta">
        <span className={`pilot-key-state ${statusClass(branch.status)}`}>{shortStatus(branch.statusLabel)}</span>
        <code>{metric}</code>
      </div>
    </>
  )
}

function WideGraph({ model }: { model: GraphModel }) {
  const branches = model.visibleBranches
  const count = branches.length
  const cardW = 220
  const gap = 16
  const isScrollable = model.mode !== 'full'
  const width = isScrollable ? count * cardW + Math.max(0, count - 1) * gap : 896
  const first = isScrollable ? cardW / 2 : width / (2 * count)
  const last = isScrollable ? (count - 1) * (cardW + gap) + cardW / 2 : (width * (2 * count - 1)) / (2 * count)

  return (
    <div className="pilot-graph-shell">
      <div className="pilot-graph-scroll">
        <div className="pilot-wide-canvas" style={{ width: isScrollable ? width : '100%' }}>
          <svg className="pilot-fork" viewBox={`0 0 ${width} 40`} preserveAspectRatio="none">
            <path d={`M ${width / 2} 0 L ${width / 2} 8`} />
            <path d={`M ${first} 20 L ${last} 20`} />
            <path d={`M ${width / 2} 8 L ${width / 2} 20`} />
            {branches.map((_, i) => {
              const x = isScrollable ? i * (cardW + gap) + cardW / 2 : (width * (2 * i + 1)) / (2 * count)
              return <path key={i} d={`M ${x} 20 L ${x} 40`} />
            })}
          </svg>
          <div
            className={`pilot-par-grid ${isScrollable ? 'is-scroll' : ''} ${model.mode === 'dense-scroll' ? 'is-dense' : ''}`}
            style={isScrollable ? { gridTemplateColumns: `repeat(${count}, ${cardW}px)`, gap, width } : { gridTemplateColumns: `repeat(${count}, minmax(0, 1fr))`, gap }}
          >
            {branches.map(branch => (
              <a href={detailHref(branch)} key={branch.id} className={`pilot-par-card ${statusClass(branch.status)}`}>
                <GraphBranchCardContent branch={branch} />
              </a>
            ))}
          </div>
        </div>
      </div>
      {isScrollable && <div className="pilot-graph-note">{model.mode === 'dense-scroll' ? '压缩并行轨道' : '可滚动并行轨道'} · {count} 条分支 · 横向滚动查看完整拓扑</div>}
    </div>
  )
}

function DeepGraph({ model }: { model: GraphModel }) {
  const versions = model.visibleVersions
  return (
    <div className="pilot-evolution pilot-evolution-graph">
      {versions.map((version, index) => (
        <div className="pilot-evolution-step" key={version.letter}>
          {index > 0 && version.diff && (
            <div className="pilot-bridge">
              <div className="pilot-directive">
                <div className="pilot-directive-label">▼ 触发改动</div>
                <div className="pilot-directive-summary">{version.diff.summary}</div>
                {version.diff.changes && (
                  <ul className="pilot-change-list">
                    {version.diff.changes.slice(0, 4).map(change => (
                      <li className={`pilot-change-${change.type}`} key={`${change.type}-${change.text}`}>
                        {shortenChange(change.text)}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
          <a href={detailHref(version)} className={`pilot-deep-card ${statusClass(version.status)}`}>
            <GraphBranchCardContent branch={version} typeLabel="version" />
            {version.contentMeta && <p className="pilot-key-row-why">{version.contentMeta}</p>}
            {version.reviews && (
              <div className="pilot-review-chips">
                {version.reviews.map(review => <span className={`pilot-review-chip-${review.verdict}`} key={review.who}>{review.who.slice(0, 6)} {review.verdictText}</span>)}
              </div>
            )}
          </a>
        </div>
      ))}
      {model.hidden.versions > 0 && <div className="pilot-graph-note">关键帧时间线 · 已折叠 {model.hidden.versions} 个中间版本 · 查看「版本详情」获取完整链路</div>}
    </div>
  )
}

function TreeGraph({ model }: { model: GraphModel }) {
  const branches = model.visibleBranches
  const cardW = 220
  const cardH = 132
  const xGap = 36
  const yGap = 76
  const topY = 52
  const children = useMemo(() => {
    const map: Record<string, GraphBranch[]> = {}
    for (const b of branches) {
      const parent = b.parent || '__root__'
      map[parent] ||= []
      map[parent].push(b)
    }
    return map
  }, [branches])
  const roots = children.__root__ || []
  const positions: Record<string, { x: number; y: number }> = {}
  const widths: Record<string, number> = {}

  function measure(node: GraphBranch): number {
    const kids = children[node.id] || []
    if (!kids.length) return widths[node.id] = cardW
    const width = kids.reduce((sum, kid, i) => sum + measure(kid) + (i ? xGap : 0), 0)
    return widths[node.id] = Math.max(cardW, width)
  }

  function place(node: GraphBranch, left: number, depth: number) {
    const width = widths[node.id] || cardW
    positions[node.id] = { x: snap4(left + width / 2 - cardW / 2), y: topY + depth * (cardH + yGap) }
    let cursor = left
    for (const [i, kid] of (children[node.id] || []).entries()) {
      if (i) cursor += xGap
      place(kid, cursor, depth + 1)
      cursor += widths[kid.id] || cardW
    }
  }

  roots.forEach(measure)
  const contentWidth = roots.reduce((sum, root, i) => sum + (i ? xGap : 0) + (widths[root.id] || cardW), 0)
  const width = Math.max(896, contentWidth)
  let cursor = snap4((width - contentWidth) / 2)
  roots.forEach((root, i) => {
    if (i) cursor += xGap
    place(root, cursor, 0)
    cursor += widths[root.id] || cardW
  })
  const maxDepth = Math.max(...branches.map(b => {
    let depth = 0
    let parent = b.parent
    while (parent) {
      depth += 1
      parent = branches.find(x => x.id === parent)?.parent || null
    }
    return depth
  }), 0)
  const height = topY + (maxDepth + 1) * cardH + maxDepth * yGap + 34

  return (
    <div className="pilot-graph-shell">
      <div className="pilot-tree-scroll">
        <div className="pilot-tree-canvas" style={{ width, height }}>
          <svg className="pilot-tree-svg" viewBox={`0 0 ${width} ${height}`}>
            {roots.map(root => {
              const cx = snap4(positions[root.id].x + cardW / 2)
              return <path key={`root-${root.id}`} d={`M ${width / 2} 12 L ${width / 2} 28 L ${cx} 28 L ${cx} ${topY}`} />
            })}
            {branches.filter(b => b.parent).map(branch => {
              const p = positions[branch.parent!]
              const c = positions[branch.id]
              if (!p || !c) return null
              const px = snap4(p.x + cardW / 2)
              const py = snap4(p.y + cardH)
              const cx = snap4(c.x + cardW / 2)
              const cy = snap4(c.y)
              const mid = snap4((py + cy) / 2)
              return <path key={branch.id} d={`M ${px} ${py} L ${px} ${mid} L ${cx} ${mid} L ${cx} ${cy}`} />
            })}
          </svg>
          {branches.map(branch => {
            const pos = positions[branch.id]
            if (!pos) return null
            const content = <GraphBranchCardContent branch={branch} />
            if (branch.isCollapsed) {
              return <div key={branch.id} className={`pilot-tree-card ${statusClass(branch.status)}`} style={{ left: pos.x, top: pos.y, width: cardW, height: cardH }}>{content}</div>
            }
            return <a href={detailHref(branch)} key={branch.id} className={`pilot-tree-card ${statusClass(branch.status)}`} style={{ left: pos.x, top: pos.y, width: cardW, height: cardH }}>{content}</a>
          })}
        </div>
      </div>
      {model.hidden.descendants > 0 && <div className="pilot-graph-note">压缩树视图 · 已折叠 {model.hidden.descendants} 个后代节点 · 查看「版本详情」获取完整分支列表</div>}
    </div>
  )
}

function MixedGraph({ model }: { model: GraphModel }) {
  const branches = model.visibleBranches
  const headW = 260
  const nodeW = 148
  const nodeGap = 30
  const maxVisibleVersions = Math.max(...branches.map(branch => branch.visibleVersions?.length || branch.versions.length || 1), 1)
  const width = Math.max(896, headW + 56 + maxVisibleVersions * nodeW + Math.max(0, maxVisibleVersions - 1) * nodeGap + 48)

  return (
    <div className="pilot-graph-shell">
      <div className="pilot-graph-scroll">
        <div className="pilot-mixed-canvas" style={{ width }}>
          <div className="pilot-route-stack">
            {branches.map(branch => {
              const visibleVersions = branch.visibleVersions || branch.versions
              return (
                <div key={branch.id} className={`pilot-route-row ${statusClass(branch.status)}`}>
                  <a href={detailHref(branch)} className="pilot-route-head">
                    <GraphBranchCardContent branch={branch} />
                    <div className="pilot-version-meta">{branch.versions.length} versions · {statusSummary(branch.versions)}</div>
                  </a>
                  <ol className="pilot-route-track">
                    {visibleVersions.map((version, index) => {
                      const previous = index > 0 ? visibleVersions[index - 1] : null
                      const skipped = previous ? version.index - previous.index - 1 : 0
                      return (
                        <li key={version.letter} className="pilot-route-step">
                          {index > 0 && skipped > 0 && <span className="pilot-route-gap">+{skipped}</span>}
                          <span className={`pilot-route-node ${statusClass(version.status)}`}>
                            <strong>{version.letter}</strong>
                            <em>{version.statusLabel}</em>
                            {version.contentMeta && <small>{version.contentMeta}</small>}
                          </span>
                        </li>
                      )
                    })}
                  </ol>
                </div>
              )
            })}
          </div>
        </div>
      </div>
      {model.hidden.versions > 0 && <div className="pilot-graph-note">压缩泳道视图 · 已折叠 {model.hidden.versions} 个中间版本 · 查看「版本详情」获取完整版本链</div>}
    </div>
  )
}

type DeliverableNode = {
  id: string
  branch: PilotBranch
  kind: PilotDeliverableKind
  relations: PilotRelation[]
  depth: number
}

type KeyDeliverableCard = {
  id: string
  href?: string
  kind: PilotDeliverableKind
  name: string
  status: PilotStatus
  statusText: string
  metric: string
  why: string
  source: string
  score: number
  order: number
  timeLabel: string
  assets: KeyDeliverableAsset[]
}

type KeyDeliverableAsset = {
  id: string
  label: string
  kind: PilotDeliverableKind
  tone: 'primary' | 'evidence' | 'relation' | 'version' | 'review'
}

type DeliverableSortMode = 'recommended' | 'timeline' | 'type'
type DeliverableFilterMode = 'all' | PilotDeliverableKind

function KeyDeliverables({ data }: { data: PilotDataset }) {
  const [sortMode, setSortMode] = useState<DeliverableSortMode>('recommended')
  const [filterMode, setFilterMode] = useState<DeliverableFilterMode>('all')
  const allCards = useMemo(() => buildKeyDeliverables(data, sortMode), [data, sortMode])
  const counts = allCards.reduce<Record<string, number>>((acc, card) => {
    acc[card.kind] = (acc[card.kind] || 0) + 1
    return acc
  }, {})
  const availableKinds = useMemo(() => Object.keys(counts)
    .map(kind => kind as PilotDeliverableKind)
    .sort((a, b) => deliverableTypeRank(a) - deliverableTypeRank(b)), [counts])
  const cards = useMemo(() => {
    const filtered = filterMode === 'all' ? allCards : allCards.filter(card => card.kind === filterMode)
    return filtered.slice(0, 8)
  }, [allCards, filterMode])
  const groups = useMemo(() => groupKeyDeliverables(cards, sortMode), [cards, sortMode])

  useEffect(() => {
    if (filterMode !== 'all' && !counts[filterMode]) setFilterMode('all')
  }, [counts, filterMode])

  return (
    <div className="pilot-key-deliverables">
      <div className="pilot-key-ledger">
        <strong>关键产物</strong>
        <span>{cards.length}{cards.length !== allCards.length ? ` / ${allCards.length}` : ''} items</span>
        <div className="pilot-key-filter" aria-label="关键产物类型过滤">
          <span>类型</span>
          <button type="button" className={filterMode === 'all' ? 'is-active' : ''} onClick={() => setFilterMode('all')}>
            All
          </button>
          {availableKinds.map(kind => (
            <button
              type="button"
              key={kind}
              className={filterMode === kind ? 'is-active' : ''}
              onClick={() => setFilterMode(kind)}
            >
              {kindLabel(kind)} <b>{counts[kind]}</b>
            </button>
          ))}
        </div>
        <div className="pilot-key-sort" aria-label="关键产物排序">
          <span>排序</span>
          {[
            ['recommended', '推荐顺序'],
            ['timeline', '时间线'],
            ['type', '类型'],
          ].map(([mode, label]) => (
            <button
              type="button"
              key={mode}
              className={sortMode === mode ? 'is-active' : ''}
              onClick={() => setSortMode(mode as DeliverableSortMode)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="pilot-key-grid">
        {groups.map(group => (
          <div className="pilot-key-group" key={group.key}>
            {sortMode === 'type' && <div className="pilot-key-group-title"><span>{group.label}</span><b>{group.cards.length}</b></div>}
            <div className="pilot-key-group-grid">
              {group.cards.map((card, cardIndex) => {
                const displayAnchor = anchorLabel(card, cardIndex)
                const CardIcon = deliverableIcon(card.kind)
                const supportAssets = card.assets.filter(asset => asset.tone !== 'primary')
                const body = (
                  <>
                    <div className="pilot-key-card-head">
                      <span className="pilot-key-tick">{displayAnchor}</span>
                      <span className="pilot-key-type">{kindLabel(card.kind)}</span>
                    </div>
                    <div className="pilot-key-title-row">
                      <span className="pilot-key-row-icon" aria-hidden="true">
                        <CardIcon weight="regular" />
                      </span>
                      <h3>{card.name}</h3>
                    </div>
                    <div className="pilot-key-row-meta">
                      <span className={`pilot-key-state ${statusClass(card.status)}`}>{card.statusText}</span>
                      <code>{card.metric}</code>
                    </div>
                    {card.source && <p className="pilot-key-source"><strong>source</strong>{card.source}</p>}
                    <p className="pilot-key-row-why">{card.why}</p>
                    {supportAssets.length > 0 && (
                      <div className="pilot-key-support">
                        {supportAssets.slice(0, 4).map(asset => (
                          <span key={asset.id}>{supportLabel(asset)}</span>
                        ))}
                      </div>
                    )}
                  </>
                )
                if (card.href) {
                  return <a href={card.href} className={`pilot-key-card ${statusClass(card.status)}`} key={card.id}>{body}</a>
                }
                return <div className={`pilot-key-card ${statusClass(card.status)}`} key={card.id}>{body}</div>
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function deliverableIcon(kind: PilotDeliverableKind) {
  const icons: Record<PilotDeliverableKind, typeof FileText> = {
    paper: FileText,
    pending_result: Flask,
    commit: GitBranch,
    memo: NotePencil,
    result_table: Table,
    artifact: Archive,
    track: Archive,
  }
  return icons[kind]
}

function kindCode(kind: PilotDeliverableKind) {
  const codes: Record<PilotDeliverableKind, string> = {
    paper: 'P',
    pending_result: 'E',
    commit: 'C',
    memo: 'M',
    result_table: 'T',
    artifact: 'A',
    track: 'K',
  }
  return codes[kind]
}

function anchorLabel(card: KeyDeliverableCard, visibleIndex: number) {
  if (/^t\d+/i.test(card.timeLabel)) return `#${card.timeLabel}`
  if (/^#/.test(card.timeLabel)) return card.timeLabel
  return `#${String(visibleIndex + 1).padStart(2, '0')}`
}

function buildKeyDeliverables(data: PilotDataset, sortMode: DeliverableSortMode): KeyDeliverableCard[] {
  const branchCards = data.branches
    .map(branchToKeyCard(data.branches))
    .filter(card => card.kind !== 'track')
  const derivedSource = deliverableSourceFromVersions(data)
  const verdictCards = (data.verdict.deliverables || [])
    .map((item, index) => verdictDeliverableToCard(item, index, derivedSource))
    .filter((card): card is KeyDeliverableCard => Boolean(card))
    .filter(card => !branchCards.some(existing => normalizedName(existing.name) === normalizedName(card.name)))
  const fallbackTrackCards = data.branches
    .map(branchToKeyCard(data.branches))
    .filter(card => card.kind === 'track')

  // When the run already exposes concrete deliverable branches, avoid mixing in
  // verdict summary aggregates like "5 papers" or "22 experiments".
  const cards = data.branchMap.shape === 'deep' && verdictCards.length
    ? verdictCards
    : branchCards.length
      ? branchCards
      : verdictCards.length
        ? verdictCards
        : fallbackTrackCards

  return sortKeyDeliverables(cards, sortMode)
}

function sortKeyDeliverables(cards: KeyDeliverableCard[], mode: DeliverableSortMode) {
  return [...cards].sort((a, b) => {
    if (mode === 'timeline') return a.order - b.order || b.score - a.score
    if (mode === 'type') return deliverableTypeRank(a.kind) - deliverableTypeRank(b.kind) || b.score - a.score || a.order - b.order
    return b.score - a.score || a.order - b.order || a.name.localeCompare(b.name)
  })
}

function deliverableTypeRank(kind: PilotDeliverableKind) {
  const typeRank: Record<PilotDeliverableKind, number> = {
    paper: 1,
    pending_result: 2,
    result_table: 3,
    commit: 4,
    memo: 5,
    artifact: 6,
    track: 7,
  }
  return typeRank[kind]
}

function groupKeyDeliverables(cards: KeyDeliverableCard[], mode: DeliverableSortMode) {
  if (mode !== 'type') return [{ key: mode, label: mode, cards }]
  const groups: Array<{ key: string; label: string; cards: KeyDeliverableCard[] }> = []
  for (const card of cards) {
    const key = card.kind
    let group = groups.find(item => item.key === key)
    if (!group) {
      group = { key, label: kindLabel(card.kind).toUpperCase(), cards: [] }
      groups.push(group)
    }
    group.cards.push(card)
  }
  return groups
}

function branchToKeyCard(branches: PilotBranch[]) {
  return (branch: PilotBranch): KeyDeliverableCard => {
    const order = branches.findIndex(item => item.letter === branch.letter)
    const timelineOrder = branch.createdTick ?? (order === -1 ? 999 : order)
    const inbound = branches.filter(item => item.relations?.some(relation => relation.target === branch.letter) || item.parent === branch.letter).length
    const relationCount = (branch.relations?.length || 0) + inbound
    const kind = branch.kind || inferDeliverableKind(branch)
    const metric = compactMetric(branch.result || branch.attempts || branch.decision || 'no metric')

    return {
      id: branch.letter,
      href: detailHref(branch),
      kind,
      name: readableDeliverableName(branch),
      status: branch.status,
      statusText: shortStatus(branch.statusLabel),
      metric,
      why: deliverableWhy(branch, kind, inbound),
      source: relationCount ? `${relationCount} linked relation${relationCount > 1 ? 's' : ''}` : branch.attempts || 'standalone',
      score: deliverableCardScore(branch, kind, inbound),
      order: timelineOrder,
      timeLabel: branch.createdTick === undefined ? `#${String((order === -1 ? 999 : order) + 1).padStart(2, '0')}` : `t${branch.createdTick}`,
      assets: deliverableAssetsForBranch(branch, kind, relationCount),
    }
  }
}

function deliverableAssetsForBranch(branch: PilotBranch, kind: PilotDeliverableKind, relationCount: number): KeyDeliverableAsset[] {
  const assets: KeyDeliverableAsset[] = [{ id: `${branch.letter}-primary`, label: kindCode(kind), kind, tone: 'primary' }]
  const evidenceCount = branch.evidence?.length || 0
  const reviewCount = branch.reviews?.length || branch.evidence?.filter(item => item.qt).length || 0
  const versionCount = branch.versions?.length || 0

  if (evidenceCount > 0) assets.push({ id: `${branch.letter}-evidence`, label: String(evidenceCount), kind: 'result_table', tone: 'evidence' })
  if (relationCount > 0) assets.push({ id: `${branch.letter}-relations`, label: String(relationCount), kind: 'artifact', tone: 'relation' })
  if (versionCount > 1) assets.push({ id: `${branch.letter}-versions`, label: String(versionCount), kind: 'track', tone: 'version' })
  if (reviewCount > 0) assets.push({ id: `${branch.letter}-reviews`, label: String(reviewCount), kind: 'memo', tone: 'review' })

  return assets
}

function supportLabel(asset: KeyDeliverableAsset) {
  const labels: Record<KeyDeliverableAsset['tone'], string> = {
    primary: kindLabel(asset.kind),
    evidence: 'evidence',
    relation: 'linked',
    version: 'versions',
    review: 'reviews',
  }
  return `${asset.label} ${labels[asset.tone]}`
}

function deliverableSourceFromVersions(data: PilotDataset) {
  if (data.branchMap.shape !== 'deep') return ''
  const final = [...data.branches].reverse().find(branch => branch.status === 'chosen') || data.branches[data.branches.length - 1]
  if (!final) return ''
  const title = stripTitle(final.title)
  return title.includes('·') ? title.split('·')[0].trim() : title.trim()
}

function verdictDeliverableToCard(item: { icon: string; name: string; meta: string }, index: number, derivedSource = ''): KeyDeliverableCard | null {
  const text = `${item.name} ${item.meta}`.toLowerCase()
  let kind: PilotDeliverableKind | null = null
  if (text.includes('paper')) kind = 'paper'
  else if (text.includes('commit')) kind = 'commit'
  else if (text.includes('memo')) kind = 'memo'
  else if (text.includes('result') || text.includes('experiment') || text.includes('.tsv')) kind = 'result_table'
  else kind = 'artifact'

  return {
    id: `artifact-${index}-${item.name}`,
    kind,
    name: item.name,
    status: 'chosen',
    statusText: 'Deliverable',
    metric: item.meta.split('\n')[0] || item.icon,
    why: kind === 'commit'
      ? '复现实验和落地实现的入口。'
      : kind === 'memo'
        ? '记录选择和不选择的理由。'
        : kind === 'result_table'
          ? '完整实验账本,用于追溯指标和失败样本。'
          : '这次 run 的最终可交付输出,可以直接打开、发布或引用。',
    source: derivedSource || item.meta.split('\n').slice(1).join(' · ') || item.icon,
    score: 42 - index,
    order: 500 + index,
    timeLabel: `artifact #${index + 1}`,
    assets: [{ id: `artifact-${index}-primary`, label: kindCode(kind), kind, tone: 'primary' }],
  }
}

function readableDeliverableName(branch: Pick<PilotBranch, 'title'>) {
  const rawTitle = stripTitle(branch.title)
  const prefix = rawTitle.match(/^(Paper|Experiment)\s+([A-Z0-9]+)\s*·\s*/i)
  const title = stripDeliverablePrefix(rawTitle)
  const colon = title.split(':')[0]?.trim()
  const acronyms = Array.from(new Set(title.match(/\b(?:[A-Z]{2,}[A-Za-z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)\b/g) || []))
    .filter(token => !['GPT'].includes(token))
  const concise = (() => {
    if (acronyms.length >= 2) return acronyms.slice(0, 3).join(' + ')
    if (colon && colon.length >= 3 && colon.length <= 30 && !/improvements?|achieves?|study|paper/i.test(colon)) return colon

    const words = title
      .replace(/[():,+]/g, ' ')
      .split(/\s+/)
      .filter(Boolean)
      .filter(word => !/^(the|and|with|for|from|into|small|scale|language|model|models|training|improves?|improved|improvement|achieves?|through|using|based|paper)$/i.test(word))
      .slice(0, 4)
    const base = words.join(' ')
    if (acronyms.length === 1 && !base.includes(acronyms[0])) return `${base} + ${acronyms[0]}`.trim()
    return base || title.slice(0, 34)
  })()

  if (prefix) return `${capitalize(prefix[1])} ${prefix[2].padStart(3, '0')} · ${concise}`
  return concise
}

function capitalize(text: string) {
  return text.slice(0, 1).toUpperCase() + text.slice(1).toLowerCase()
}

function stripDeliverablePrefix(text: string) {
  return text.replace(/^(Paper|Experiment|Track)\s+[A-Z0-9]+\s*·\s*/i, '').trim()
}

function compactMetric(text: string) {
  const val = text.match(/val_loss\s+([-+]?\d*\.?\d+)/i)?.[0]
  const delta = text.match(/Δ\s*[-+]?\d*\.?\d+/)?.[0]
  const review = text.match(/\d+\/\d+\s+accept/i)?.[0]
  return [val, delta, review].filter(Boolean).join(' · ') || truncateText(text, 64)
}

function deliverableWhy(branch: PilotBranch, kind: PilotDeliverableKind, inbound: number) {
  if (kind === 'pending_result') return '指标上值得继续推进,但还没完成 paper/review。'
  if (inbound > 1) return `被 ${inbound} 个后续交付物依赖,是这次探索的基础结论。`
  if (branch.relations?.length) return '建立在前置交付物上,用于形成组合或扩展结论。'
  if (branch.status === 'chosen') return '已通过当前判定口径,可以作为可引用结论。'
  return truncateText(branch.decision || branch.thesis || '保留为后续判断依据。', 88)
}

function deliverableCardScore(branch: PilotBranch, kind: PilotDeliverableKind, inbound: number) {
  const delta = Math.abs(Number(branch.result?.match(/Δ\s*([-+]?\d*\.?\d+)/)?.[1] || 0))
  let score = 0
  if (branch.status === 'chosen') score += 50
  if (kind === 'pending_result') score += 26
  if (kind === 'paper') score += 18
  score += inbound * 8
  score += delta * 100
  return score
}

function shortStatus(label: string) {
  return label.replace(/[✓✗△]/g, '').trim() || label
}

function normalizedName(name: string) {
  return name.toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, '')
}

function truncateText(text: string, max: number) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function DeliverableRelationFigure({ data }: { data: PilotDataset }) {
  return (
    <figure className="pilot-delivery-figure">
      <DeliverableRelationMap data={data} />
      <figcaption>{deliverableCaption(data)}</figcaption>
    </figure>
  )
}

function DeliverableRelationMap({ data }: { data: PilotDataset }) {
  const { nodes, edges } = useMemo(() => buildDeliverableView(data.branches), [data.branches])
  const nodeW = 260
  const nodeH = 118
  const xGap = 104
  const yGap = 24
  const levels = nodes.reduce<Record<number, DeliverableNode[]>>((acc, node) => {
    acc[node.depth] ||= []
    acc[node.depth].push(node)
    return acc
  }, {})
  const depthCount = Math.max(...nodes.map(node => node.depth), 0) + 1
  const rowCount = Math.max(...Object.values(levels).map(level => level.length), 1)
  const width = Math.max(920, 48 + depthCount * nodeW + Math.max(0, depthCount - 1) * xGap + 48)
  const height = Math.max(260, 40 + rowCount * nodeH + Math.max(0, rowCount - 1) * yGap + 40)
  const positions: Record<string, { x: number; y: number }> = {}

  Object.entries(levels).forEach(([depth, level]) => {
    const x = 48 + Number(depth) * (nodeW + xGap)
    const stackHeight = level.length * nodeH + Math.max(0, level.length - 1) * yGap
    const startY = snap4((height - stackHeight) / 2)
    level.forEach((node, index) => {
      positions[node.id] = { x, y: startY + index * (nodeH + yGap) }
    })
  })

  return (
    <div className="pilot-delivery-shell">
      <div className="pilot-delivery-scroll">
        <div className="pilot-delivery-canvas" style={{ width, height }}>
          <svg className="pilot-delivery-svg" viewBox={`0 0 ${width} ${height}`}>
            <defs>
              <marker id="pilot-delivery-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                <path d="M1 1 L7 4 L1 7" />
              </marker>
            </defs>
            {edges.map(edge => {
              const from = positions[edge.source]
              const to = positions[edge.target]
              if (!from || !to) return null
              const sx = snap4(from.x + nodeW)
              const sy = snap4(from.y + nodeH / 2)
              const tx = snap4(to.x)
              const ty = snap4(to.y + nodeH / 2)
              const mid = snap4((sx + tx) / 2)
              const labelX = snap4((sx + tx) / 2)
              const labelY = snap4((sy + ty) / 2 - 6)
              return (
                <g key={`${edge.source}-${edge.target}-${edge.kind}`} className={edge.confidence === 'inferred' ? 'is-inferred' : ''}>
                  <path d={`M ${sx} ${sy} C ${mid} ${sy}, ${mid} ${ty}, ${tx} ${ty}`} markerEnd="url(#pilot-delivery-arrow)" />
                  <text x={labelX} y={labelY}>{edgeLabel(edge.kind)}</text>
                </g>
              )
            })}
          </svg>
          {nodes.map(node => {
            const pos = positions[node.id]
            if (!pos) return null
            return (
              <a
                href={detailHref(node.branch)}
                key={node.id}
                className={`pilot-delivery-node pilot-delivery-kind-${node.kind} ${statusClass(node.branch.status)}`}
                style={{ left: pos.x, top: pos.y, width: nodeW, minHeight: nodeH }}
              >
                <div className="pilot-delivery-node-head">
                  <span>{kindLabel(node.kind)}</span>
                  <em className={`pilot-pill ${statusClass(node.branch.status)}`}>{node.branch.statusLabel}</em>
                </div>
                <strong>{stripTitle(node.branch.title)}</strong>
                {node.branch.result && <p>{node.branch.result}</p>}
                {node.branch.attempts && <small>{node.branch.attempts}</small>}
              </a>
            )
          })}
        </div>
      </div>
      <div className="pilot-delivery-meta">
        <span>{nodes.length} deliverables</span>
        <span>{edges.length} relations</span>
        <span>{edges.filter(edge => edge.confidence === 'inferred').length} inferred</span>
      </div>
    </div>
  )
}

function buildDeliverableView(branches: PilotBranch[]) {
  const baseNodes = branches.map(branch => ({
    id: branch.letter,
    branch,
    kind: branch.kind || inferDeliverableKind(branch),
    relations: branch.relations?.length
      ? branch.relations
      : branch.parent
        ? [{ target: branch.parent, kind: 'derived_from' as const, confidence: 'inferred' as const }]
        : [],
    depth: 0,
  }))
  const nodeMap = new Map(baseNodes.map(node => [node.id, node]))
  const edges = baseNodes.flatMap(node => node.relations
    .filter(relation => nodeMap.has(relation.target))
    .map(relation => ({
      source: relation.target,
      target: node.id,
      kind: relation.kind,
      confidence: relation.confidence || 'field',
    })))

  function depthFor(node: DeliverableNode, seen = new Set<string>()): number {
    if (seen.has(node.id)) return 0
    seen.add(node.id)
    const parents = node.relations.map(relation => nodeMap.get(relation.target)).filter((item): item is DeliverableNode => Boolean(item))
    if (!parents.length) return 0
    return 1 + Math.max(...parents.map(parent => depthFor(parent, seen)))
  }

  const nodes = baseNodes
    .map(node => ({ ...node, depth: depthFor(node) }))
    .sort((a, b) => a.depth - b.depth || deliverablePriority(b.branch, b.kind) - deliverablePriority(a.branch, a.kind) || a.branch.letter.localeCompare(b.branch.letter))

  return { nodes, edges }
}

function inferDeliverableKind(branch: Pick<PilotBranch, 'title' | 'versions'>): PilotDeliverableKind {
  const title = branch.title.toLowerCase()
  if (title.includes('paper')) return 'paper'
  if (title.includes('experiment')) return 'pending_result'
  if (title.includes('commit')) return 'commit'
  if (title.includes('memo')) return 'memo'
  if (title.includes('results') || title.includes('.tsv')) return 'result_table'
  return branch.versions?.length ? 'artifact' : 'track'
}

function deliverablePriority(branch: PilotBranch, kind: PilotDeliverableKind) {
  let score = 0
  if (branch.status === 'chosen') score += 10
  if (kind === 'paper') score += 4
  if (kind === 'pending_result') score += 3
  if (branch.relations?.length) score += 2
  return score
}

function kindLabel(kind: PilotDeliverableKind) {
  const labels: Record<PilotDeliverableKind, string> = {
    paper: 'paper',
    pending_result: 'pending result',
    commit: 'commit',
    memo: 'memo',
    result_table: 'result table',
    artifact: 'artifact',
    track: 'track',
  }
  return labels[kind]
}

function edgeLabel(kind: PilotRelation['kind']) {
  const labels: Record<PilotRelation['kind'], string> = {
    cites: 'cites',
    builds_on: 'builds on',
    supersedes: 'supersedes',
    verifies: 'verifies',
    derived_from: 'derived from',
    materialized_by: 'materialized by',
    text_reference: 'mentions',
  }
  return labels[kind]
}

function deliverableCaption(data: PilotDataset) {
  const relationCount = data.branches.reduce((sum, branch) => sum + (branch.relations?.length || (branch.parent ? 1 : 0)), 0)
  return `图 1. ${data.branches.length} 个交付物节点 · ${relationCount} 条结构关系；paper.cites 是强边，文本引用会标成弱边。`
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="pilot-section">
      <h2>{title}</h2>
      {children}
    </section>
  )
}

function graphCaption(data: PilotDataset) {
  if (data.branchMap.shape === 'wide') return '图 1. 同一主题下的并行演化路径；横向滚动保留所有分支与连接线。'
  if (data.branchMap.shape === 'deep') return '图 1. 单一路线按版本推进；中间桥接块只显示触发改动的关键信息。'
  if (data.branchMap.shape === 'tree') return '图 1. 从根问题拆到子产物的演化路径；大树会折叠低优先级后代，详情仍在「产物详情」。'
  return '图 1. 多条路线各自迭代版本；泳道内长链会自动折叠中间版本。'
}

function DiagramFigure({ data }: { data: PilotDataset }) {
  return (
    <figure className="pilot-graph-figure">
      <div className="pilot-graph-box"><Graph data={data} /></div>
      <figcaption>{graphCaption(data)}</figcaption>
    </figure>
  )
}

function BranchDetail({ branch, defaultOpen }: { branch: PilotBranch; defaultOpen: boolean }) {
  return (
    <details className="pilot-detail" id={`pilot-detail-${branch.letter}`} open={defaultOpen}>
      <summary>
        <span>{branch.letter}</span>
        <strong>{branch.title}</strong>
        <em className={`pilot-pill ${statusClass(branch.status)}`}>{branch.statusLabel}</em>
      </summary>
      <div className="pilot-detail-body">
        {branch.thesis && <Field label="论点">{branch.thesis}</Field>}
        {branch.content && (
          <div className="pilot-content-block">
            <div className="pilot-content-head">正文版本 <code>{branch.contentMeta}</code></div>
            <pre>{branch.content}</pre>
          </div>
        )}
        {branch.versions && (
          <div className="pilot-version-stack">
            {branch.versions.map((version, index) => (
              <div className={`pilot-mini-version ${statusClass(version.status)}`} key={version.letter}>
                <div className="pilot-mini-head">
                  <strong>{version.letter}</strong>
                  <span>{version.statusLabel}</span>
                  {version.contentMeta && <code>{version.contentMeta}</code>}
                </div>
                {index > 0 && version.diff && <div className="pilot-mini-diff">{version.diff.summary}</div>}
              </div>
            ))}
          </div>
        )}
        {branch.attempts && <Field label="尝试">{branch.attempts}</Field>}
        {branch.result && <Field label="结果">{branch.result}</Field>}
        {branch.evidence && (
          <div className="pilot-evidence">
            {branch.evidence.map(e => <blockquote key={e.line}>{e.line}</blockquote>)}
          </div>
        )}
        {branch.reviews && (
          <div className="pilot-reviews">
            {branch.reviews.map(review => (
              <div className="pilot-review-row" key={review.who}>
                <b className={`pilot-review-${review.verdict}`}>{review.verdictText}</b>
                <strong>{review.who}</strong>
                <span>{review.quote}</span>
              </div>
            ))}
          </div>
        )}
        {branch.diff && (
          <div className="pilot-diff">
            <div className="pilot-diff-summary">{branch.diff.summary}</div>
            <ul>
              {branch.diff.changes.map(change => (
                <li className={`pilot-change-${change.type}`} key={`${change.type}-${change.text}`}>{change.text}</li>
              ))}
            </ul>
          </div>
        )}
        {branch.decision && <Field label="决定">{branch.decision}</Field>}
      </div>
    </details>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="pilot-field">
      <div className="pilot-field-label">{label}</div>
      <div className="pilot-field-val">{children}</div>
    </div>
  )
}

function questionLabel(label: string) {
  const labels: Record<string, string> = {
    Topic: '主题',
    Scope: '范围',
    'Why now': '缘由',
  }
  return labels[label] || label
}

function ruleParts(rule: string) {
  const match = rule.match(/^<strong>(.*?)<\/strong>:?(.*)$/)
  if (!match) return { lead: '', rest: rule }
  return { lead: match[1], rest: match[2].trim() }
}

function scopeParts(scope: string) {
  const labels = ['对象', '数据', '资源', '门槛']
  return scope.split(/\s*·\s*/).map((part, index) => {
    const [head, ...rest] = part.split(':')
    if (rest.length) {
      return { label: head.trim(), value: rest.join(':').trim() }
    }
    return {
      label: labels[index] || String(index + 1).padStart(2, '0'),
      value: part.trim(),
    }
  })
}

function protocolListClass(base: string, count: number) {
  return [
    base,
    count > 8 ? 'pilot-protocol-list-dense' : '',
    count > 10 ? 'pilot-protocol-list-scroll' : '',
  ].filter(Boolean).join(' ')
}

function protocolIndex(index: number, total: number) {
  return String(index + 1).padStart(total >= 100 ? 3 : 2, '0')
}

export default function PilotPage() {
  const [key, setKey] = useState<keyof typeof PILOT_DATA>('autoresearch')
  const [latestAutoresearch, setLatestAutoresearch] = useState<PilotDataset | null>(null)
  const [latestState, setLatestState] = useState<'loading' | 'api' | 'snapshot' | 'demo'>('loading')
  const data = key === 'autoresearch' && latestAutoresearch ? latestAutoresearch : PILOT_DATA[key]
  const switcherMeta = key === 'autoresearch'
    ? latestState === 'loading'
      ? 'loading latest autoresearch run...'
      : latestState === 'api'
        ? `live api · ${data.meta}`
        : latestState === 'snapshot'
          ? `local snapshot · ${data.meta}`
          : `demo fallback · ${data.meta}`
    : data.meta

  useEffect(() => {
    const controller = new AbortController()
    fetchLatestAutoresearchPilotDataset(controller.signal)
      .then(result => {
        if (controller.signal.aborted) return
        setLatestAutoresearch(result?.dataset || null)
        setLatestState(result?.source || 'demo')
      })
      .catch(() => {
        if (controller.signal.aborted) return
        setLatestAutoresearch(null)
        setLatestState('demo')
      })
    return () => controller.abort()
  }, [])

  return (
    <div className="pilot-page">
      <div className="pilot-switcher">
        <span>视图类型:</span>
        <div>
          {PILOT_DATASETS.map(item => (
            <Button key={item.key} size="sm" variant={item.key === key ? 'default' : 'outline'} onClick={() => setKey(item.key as keyof typeof PILOT_DATA)}>
              {item.label}
            </Button>
          ))}
        </div>
        <code>{switcherMeta}</code>
      </div>
      <main className="pilot-doc">
        <header className="pilot-header">
          <div>{data.eyebrow}</div>
          <h1>{data.title}</h1>
          <p>
            <span className="pilot-topic-label">TOPIC</span>
            <span className="pilot-topic-text">{data.subtitle}</span>
          </p>
        </header>

        <Section title="决策摘要">
          <div className="pilot-verdict">
            <div dangerouslySetInnerHTML={{ __html: data.verdict.lead }} />
            {data.verdict.bullets && <ul>{data.verdict.bullets.map(x => <li key={x}>{x}</li>)}</ul>}
            {data.verdict.recommend && (
              <div className="pilot-verdict-recommend">
                <b>建议</b>
                <span>{data.verdict.recommend}</span>
              </div>
            )}
          </div>
        </Section>

        <Section title="关键产物">
          <KeyDeliverables data={data} />
        </Section>

        <Section title={graphSectionTitle(data)}>
          <DiagramFigure data={data} />
        </Section>

        <Section title={detailSectionTitle(data)}>
          {data.branches.map((branch, index) => <BranchDetail branch={branch} defaultOpen={index === 0} key={branch.letter} />)}
        </Section>

        <Section title="研究上下文">
          <div className="pilot-study-brief">
            <div className="pilot-study-intro">
              <div className="pilot-study-thesis">
                <span>研究问题</span>
                <h3>{data.question.Topic}</h3>
                <p><strong>{questionLabel('Why now')}</strong>{data.question['Why now']}</p>
              </div>
              <dl className="pilot-study-scope">
                {scopeParts(data.question.Scope).map(part => (
                  <div key={part.label}>
                    <dt>{part.label}</dt>
                    <dd>{part.value}</dd>
                  </div>
                ))}
              </dl>
            </div>
            <div className="pilot-study-protocol">
              <div className={protocolListClass('pilot-study-roster', data.panel.length)}>
                <h3>参与角色 <small>{data.panel.length}</small></h3>
                {data.panel.map(agent => (
                  <div className="pilot-roster-row" key={agent.name}>
                    <b>{agent.avatar}</b>
                    <span><strong>{agent.name}</strong><small>{agent.bio}</small></span>
                  </div>
                ))}
              </div>
              <div className={protocolListClass('pilot-study-rules', data.rules.length)}>
                <h3>判定口径 <small>{data.rules.length}</small></h3>
                {data.rules.map((rule, index) => {
                  const parts = ruleParts(rule)
                  return (
                    <div className="pilot-rule-row" key={rule}>
                      <b>{protocolIndex(index, data.rules.length)}</b>
                      <span>
                        {parts.lead && <strong>{parts.lead}</strong>}
                        {parts.rest && <small>{parts.rest}</small>}
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          </div>
        </Section>

        <Section title="置信度边界">
          <div className="pilot-conf">
            <div><h3>已验证</h3><ul>{data.confidence.tested.map(x => <li key={x}>{x}</li>)}</ul></div>
            <div><h3>未验证</h3><ul>{data.confidence.untested.map(x => <li key={x}>{x}</li>)}</ul></div>
            <div><h3>下一步</h3><ul>{data.confidence.next.map(x => <li key={x}>{x}</li>)}</ul></div>
          </div>
        </Section>

        <Section title="后续动作">
          <div className="pilot-continue">
            {(data.verdict.actions || ['分享链接', '导出 PDF']).map((action, index) => <Button key={action} variant={index === 0 ? 'default' : 'outline'}>{action}</Button>)}
          </div>
        </Section>
      </main>
    </div>
  )
}

function detailSectionTitle(data: PilotDataset) {
  return data.branchMap.shape === 'deep' ? '版本演化' : '产物详情'
}

function graphSectionTitle(_data: PilotDataset) {
  return '演化路径'
}
