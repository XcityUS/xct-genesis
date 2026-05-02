// Pure graph data-model layer: takes a PilotDataset and produces a GraphModel
// with shape-aware compaction (wide / deep / tree / mixed). No React, no JSX.

import type { PilotDataset } from '../pilot-data-types'
import { GRAPH_LIMITS, type GraphBranch, type GraphModel, type GraphStatus } from '../types'

export function buildGraphModel(data: PilotDataset): GraphModel {
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

export function compactGraphModel(data: PilotDataset): GraphModel {
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
