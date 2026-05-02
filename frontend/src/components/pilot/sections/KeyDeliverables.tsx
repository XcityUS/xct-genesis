// 关键产物 — sortable / filterable card grid of the run's key outputs.
// Builder helpers stay co-located with the component (tightly cohesive,
// no other consumer); the component owns sortMode + filterMode local state.

import { useEffect, useMemo, useState } from 'react'
import {
  anchorLabel,
  compactMetric,
  deliverableCardScore,
  deliverableIcon,
  deliverableTypeRank,
  deliverableWhy,
  detailHref,
  inferDeliverableKind,
  kindCode,
  kindLabel,
  normalizedName,
  readableDeliverableName,
  shortStatus,
  statusClass,
  stripTitle,
  supportLabel,
} from '../format'
import type { PilotBranch, PilotDataset, PilotDeliverableKind } from '../pilot-data-types'
import type { DeliverableFilterMode, DeliverableSortMode, KeyDeliverableAsset, KeyDeliverableCard } from '../types'

export function KeyDeliverables({ data }: { data: PilotDataset }) {
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
                    {card.imageRef && (
                      <img className="pilot-key-card-image" src={card.imageRef} alt={card.name} loading="lazy" />
                    )}
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

// ── builder helpers (kept here to preserve cohesion; not used elsewhere) ───

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
      imageRef: branch.image_ref || branch.versions?.find(v => v.image_ref)?.image_ref,
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

function deliverableSourceFromVersions(data: PilotDataset) {
  if (data.branchMap.shape !== 'deep') return ''
  const final = [...data.branches].reverse().find(branch => branch.status === 'chosen') || data.branches[data.branches.length - 1]
  if (!final) return ''
  const title = stripTitle(final.title)
  return title.includes('·') ? title.split('·')[0].trim() : title.trim()
}

function verdictDeliverableToCard(item: { icon: string; name: string; meta: string; image_ref?: string }, index: number, derivedSource = ''): KeyDeliverableCard | null {
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
    imageRef: item.image_ref,
  }
}
