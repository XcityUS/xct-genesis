// Tree shape: root question splits into sub-hypotheses. Used by tree_rag style
// scenarios where investigation forks downward into specialized branches.

import { useMemo } from 'react'
import { detailHref, snap4, statusClass } from '../format'
import type { GraphBranch, GraphModel } from '../types'
import { GraphBranchCardContent } from './GraphBranchCardContent'

export function TreeGraph({ model }: { model: GraphModel }) {
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
