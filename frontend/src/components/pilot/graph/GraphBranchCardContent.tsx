// Shared card content used by all 4 graph shapes (wide / deep / tree / mixed).
// Renders the icon + title + status + metric block inside a graph node card.

import {
  compactMetric,
  deliverableIcon,
  inferDeliverableKind,
  kindLabel,
  readableDeliverableName,
  shortStatus,
  statusClass,
} from '../format'
import type { GraphBranch } from '../types'

export function GraphBranchCardContent({ branch, typeLabel }: { branch: GraphBranch; typeLabel?: string }) {
  const kind = branch.kind || inferDeliverableKind(branch)
  const CardIcon = deliverableIcon(kind)
  const metric = compactMetric(branch.result || branch.attempts || branch.decision || 'no metric')
  const anchor = branch.createdTick === undefined ? branch.letter : `#t${branch.createdTick}`
  const imageRef = branch.image_ref || branch.versions.find(v => v.image_ref)?.image_ref
  return (
    <>
      {imageRef && <img className="pilot-graph-node-image" src={imageRef} alt={branch.title} loading="lazy" />}
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
