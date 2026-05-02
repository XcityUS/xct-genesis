// Pure formatting and view-model helpers used across pilot sections, graphs,
// and key-deliverables. No React, no JSX. The one exception is `deliverableIcon`
// which returns a phosphor-icons component reference — still pure data, just
// with a UI library type.

import { Archive, FileText, Flask, GitBranch, NotePencil, Table } from '@phosphor-icons/react'
import type { PilotBranch, PilotDataset, PilotDeliverableKind } from './pilot-data-types'
import type { GraphBranch, GraphStatus, KeyDeliverableAsset, KeyDeliverableCard } from './types'

// ── status / class helpers ─────────────────────────────────────────────────

export function statusClass(status: GraphStatus | PilotBranch['status']) {
  return `pilot-status-${status}`
}

export function stripTitle(title: string) {
  return title.replace(/^(Branch|Angle)\s+\w+\s*·\s*/, '').replace(/^[\w]+\s*·\s*/, '')
}

export function branchStats(branch: { attempts?: string; result?: string }) {
  const parts: string[] = []
  const exp = branch.attempts?.match(/(\d+)\s*次\s*experiment/)
  const paper = branch.attempts?.match(/(paper_\d+)/)
  const delta = branch.result?.match(/Δ\s*val_loss[\s=]*([-+]?\d*\.?\d+(?:\s*~\s*[-+]?\d*\.?\d+)?)/)
  if (exp) parts.push(`实验 ${exp[1]}`)
  if (paper) parts.push(`→ ${paper[1]}`)
  if (delta) parts.push(`Δ ${delta[1]}`)
  return parts.join(' · ')
}

export function statusSummary(items: Array<{ status: PilotBranch['status'] }>) {
  const counts = items.reduce(
    (acc, item) => {
      acc[item.status] += 1
      return acc
    },
    { chosen: 0, killed: 0, parked: 0 },
  )
  return `${counts.chosen} chosen · ${counts.parked} parked · ${counts.killed} killed`
}

export function shortenChange(text: string) {
  return text.replace(/\([^)]*\)\s*$/g, '').trim()
}

export function detailHref(branch: Pick<GraphBranch | PilotBranch, 'letter'>) {
  return `#pilot-detail-${branch.letter}`
}

export function snap4(value: number) {
  return Math.round(value / 4) * 4
}

// ── deliverable kind / icon mapping ────────────────────────────────────────

export function deliverableIcon(kind: PilotDeliverableKind) {
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

export function kindCode(kind: PilotDeliverableKind) {
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

export function kindLabel(kind: PilotDeliverableKind) {
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

export function inferDeliverableKind(branch: Pick<PilotBranch, 'title' | 'versions'>): PilotDeliverableKind {
  const title = branch.title.toLowerCase()
  if (title.includes('paper')) return 'paper'
  if (title.includes('experiment')) return 'pending_result'
  if (title.includes('commit')) return 'commit'
  if (title.includes('memo')) return 'memo'
  if (title.includes('results') || title.includes('.tsv')) return 'result_table'
  return branch.versions?.length ? 'artifact' : 'track'
}

export function deliverableTypeRank(kind: PilotDeliverableKind) {
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

// ── key-deliverable label / metric / why helpers ───────────────────────────

export function anchorLabel(card: KeyDeliverableCard, visibleIndex: number) {
  if (/^t\d+/i.test(card.timeLabel)) return `#${card.timeLabel}`
  if (/^#/.test(card.timeLabel)) return card.timeLabel
  return `#${String(visibleIndex + 1).padStart(2, '0')}`
}

export function supportLabel(asset: KeyDeliverableAsset) {
  const labels: Record<KeyDeliverableAsset['tone'], string> = {
    primary: kindLabel(asset.kind),
    evidence: 'evidence',
    relation: 'linked',
    version: 'versions',
    review: 'reviews',
  }
  return `${asset.label} ${labels[asset.tone]}`
}

export function compactMetric(text: string) {
  const val = text.match(/val_loss\s+([-+]?\d*\.?\d+)/i)?.[0]
  const delta = text.match(/Δ\s*[-+]?\d*\.?\d+/)?.[0]
  const review = text.match(/\d+\/\d+\s+accept/i)?.[0]
  return [val, delta, review].filter(Boolean).join(' · ') || truncateText(text, 64)
}

export function deliverableWhy(branch: PilotBranch, kind: PilotDeliverableKind, inbound: number) {
  if (kind === 'pending_result') return '指标上值得继续推进,但还没完成 paper/review。'
  if (inbound > 1) return `被 ${inbound} 个后续交付物依赖,是这次探索的基础结论。`
  if (branch.relations?.length) return '建立在前置交付物上,用于形成组合或扩展结论。'
  if (branch.status === 'chosen') return '已通过当前判定口径,可以作为可引用结论。'
  return truncateText(branch.decision || branch.thesis || '保留为后续判断依据。', 88)
}

export function deliverableCardScore(branch: PilotBranch, kind: PilotDeliverableKind, inbound: number) {
  const delta = Math.abs(Number(branch.result?.match(/Δ\s*([-+]?\d*\.?\d+)/)?.[1] || 0))
  let score = 0
  if (branch.status === 'chosen') score += 50
  if (kind === 'pending_result') score += 26
  if (kind === 'paper') score += 18
  score += inbound * 8
  score += delta * 100
  return score
}

// ── string normalize / truncate ────────────────────────────────────────────

export function shortStatus(label: string) {
  return label.replace(/[✓✗△]/g, '').trim() || label
}

export function normalizedName(name: string) {
  return name.toLowerCase().replace(/[^a-z0-9一-鿿]+/g, '')
}

export function truncateText(text: string, max: number) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

export function capitalize(text: string) {
  return text.slice(0, 1).toUpperCase() + text.slice(1).toLowerCase()
}

export function stripDeliverablePrefix(text: string) {
  return text.replace(/^(Paper|Experiment|Track)\s+[A-Z0-9]+\s*·\s*/i, '').trim()
}

export function readableDeliverableName(branch: Pick<PilotBranch, 'title'>) {
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

// ── study context / protocol helpers ───────────────────────────────────────

export function questionLabel(label: string) {
  const labels: Record<string, string> = {
    Topic: '主题',
    Scope: '范围',
    'Why now': '缘由',
  }
  return labels[label] || label
}

export function ruleParts(rule: string) {
  const match = rule.match(/^<strong>(.*?)<\/strong>:?(.*)$/)
  if (!match) return { lead: '', rest: rule }
  return { lead: match[1], rest: match[2].trim() }
}

export function scopeParts(scope: string) {
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

export function protocolListClass(base: string, count: number) {
  return [
    base,
    count > 8 ? 'pilot-protocol-list-dense' : '',
    count > 10 ? 'pilot-protocol-list-scroll' : '',
  ].filter(Boolean).join(' ')
}

export function protocolIndex(index: number, total: number) {
  return String(index + 1).padStart(total >= 100 ? 3 : 2, '0')
}

// ── section title helpers ──────────────────────────────────────────────────

export function graphCaption(data: PilotDataset) {
  if (data.branchMap.shape === 'wide') return '图 1. 同一主题下的并行演化路径；横向滚动保留所有分支与连接线。'
  if (data.branchMap.shape === 'deep') return '图 1. 单一路线按版本推进；中间桥接块只显示触发改动的关键信息。'
  if (data.branchMap.shape === 'tree') return '图 1. 从根问题拆到子产物的演化路径；大树会折叠低优先级后代，详情仍在「产物详情」。'
  return '图 1. 多条路线各自迭代版本；泳道内长链会自动折叠中间版本。'
}

export function detailSectionTitle(data: PilotDataset) {
  return data.branchMap.shape === 'deep' ? '版本演化' : '产物详情'
}

export function graphSectionTitle(_data: PilotDataset) {
  return '演化路径'
}
