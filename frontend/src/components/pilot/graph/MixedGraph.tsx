// Mixed shape: multiple lanes (rows) × per-lane version sequence. Used by
// mixed_campaign-style scenarios with parallel angles each iterating versions.

import { detailHref, statusClass, statusSummary } from '../format'
import type { GraphModel } from '../types'
import { GraphBranchCardContent } from './GraphBranchCardContent'

export function MixedGraph({ model }: { model: GraphModel }) {
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
