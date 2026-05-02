// 决策摘要 — verdict.lead (HTML) + bullets + recommend block.

import type { PilotDataset } from '../pilot-data-types'

export function Verdict({ data }: { data: PilotDataset }) {
  return (
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
  )
}
