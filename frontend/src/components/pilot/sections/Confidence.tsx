// 置信度边界 — tested / untested / next reflection from data.confidence.

import type { PilotDataset } from '../pilot-data-types'

export function Confidence({ data }: { data: PilotDataset }) {
  return (
    <div className="pilot-conf">
      <div><h3>已验证</h3><ul>{data.confidence.tested.map(x => <li key={x}>{x}</li>)}</ul></div>
      <div><h3>未验证</h3><ul>{data.confidence.untested.map(x => <li key={x}>{x}</li>)}</ul></div>
      <div><h3>下一步</h3><ul>{data.confidence.next.map(x => <li key={x}>{x}</li>)}</ul></div>
    </div>
  )
}
