// Deep shape: linear v1 → v2 → v3 sequence with diff bridges between versions.
// Used by autoeditor-style iterative-revision scenarios.

import { detailHref, shortenChange, statusClass } from '../format'
import type { GraphModel } from '../types'
import { GraphBranchCardContent } from './GraphBranchCardContent'

export function DeepGraph({ model }: { model: GraphModel }) {
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
