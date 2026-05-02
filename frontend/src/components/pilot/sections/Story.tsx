// 故事线 — chronological run narrative. Slot 1, between Masthead and Verdict.
// Reader walks through what happened first; verdict / deliverables come after.
// Optional section — silent when data.story is absent or empty.

import type { PilotDataset, PilotStoryMoment } from '../pilot-data-types'

export function Story({ data }: { data: PilotDataset }) {
  const story = data.story
  if (!story) return null
  const intro = story.intro?.trim()
  const moments = story.moments || []
  if (!intro && !moments.length) return null

  return (
    <div className="pilot-story">
      {intro && <p className="pilot-story-intro">{intro}</p>}
      {moments.length > 0 && (
        <ol className="pilot-story-timeline">
          {moments.map((moment, index) => (
            <li className="pilot-story-moment" key={`${index}-${moment.label}`}>
              <span className="pilot-story-tick">{momentTick(moment, index)}</span>
              <div className="pilot-story-body">
                <div className="pilot-story-head">
                  {moment.actor && <strong>{moment.actor}</strong>}
                  <span>{moment.label}</span>
                </div>
                {moment.quote && <blockquote>{moment.quote}</blockquote>}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

function momentTick(moment: PilotStoryMoment, index: number) {
  if (moment.tick !== undefined) return `t${moment.tick}`
  if (moment.time) return moment.time
  return `#${String(index + 1).padStart(2, '0')}`
}
