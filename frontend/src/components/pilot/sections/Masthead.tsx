// Pilot header (eyebrow + h1 + topic line). First visual block on the page.

import type { PilotDataset } from '../pilot-data-types'

export function Masthead({ data }: { data: PilotDataset }) {
  return (
    <header className="pilot-header">
      <div>{data.eyebrow}</div>
      <h1>{data.title}</h1>
      <p>
        <span className="pilot-topic-label">TOPIC</span>
        <span className="pilot-topic-text">{data.subtitle}</span>
      </p>
    </header>
  )
}
