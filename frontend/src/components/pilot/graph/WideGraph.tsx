// Wide shape: parallel branches under a single fork. Used by autoresearch,
// mixed_campaign-style scenarios where multiple hypotheses run in parallel.

import { detailHref, statusClass } from '../format'
import type { GraphModel } from '../types'
import { GraphBranchCardContent } from './GraphBranchCardContent'

export function WideGraph({ model }: { model: GraphModel }) {
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
