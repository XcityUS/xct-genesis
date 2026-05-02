// 研究上下文 — Topic + Scope (split into 4 columns) + Panel (agent roster) +
// Rules (judgment criteria). Both lists auto-densify when count > 8.

import { protocolIndex, protocolListClass, questionLabel, ruleParts, scopeParts } from '../format'
import type { PilotDataset } from '../pilot-data-types'

export function StudyContext({ data }: { data: PilotDataset }) {
  return (
    <div className="pilot-study-brief">
      <div className="pilot-study-intro">
        <div className="pilot-study-thesis">
          <span>研究问题</span>
          <h3>{data.question.Topic}</h3>
          <p><strong>{questionLabel('Why now')}</strong>{data.question['Why now']}</p>
        </div>
        <dl className="pilot-study-scope">
          {scopeParts(data.question.Scope).map(part => (
            <div key={part.label}>
              <dt>{part.label}</dt>
              <dd>{part.value}</dd>
            </div>
          ))}
        </dl>
      </div>
      <div className="pilot-study-protocol">
        <div className={protocolListClass('pilot-study-roster', data.panel.length)}>
          <h3>参与角色 <small>{data.panel.length}</small></h3>
          {data.panel.map(agent => (
            <div className="pilot-roster-row" key={agent.name}>
              <b>{agent.avatar}</b>
              <span><strong>{agent.name}</strong><small>{agent.bio}</small></span>
            </div>
          ))}
        </div>
        <div className={protocolListClass('pilot-study-rules', data.rules.length)}>
          <h3>判定口径 <small>{data.rules.length}</small></h3>
          {data.rules.map((rule, index) => {
            const parts = ruleParts(rule)
            return (
              <div className="pilot-rule-row" key={rule}>
                <b>{protocolIndex(index, data.rules.length)}</b>
                <span>
                  {parts.lead && <strong>{parts.lead}</strong>}
                  {parts.rest && <small>{parts.rest}</small>}
                </span>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
