// 产物详情 / 版本演化 — per-branch <details> with thesis / content / versions /
// attempts / result / evidence / reviews / diff / decision rendering.

import type { ReactNode } from 'react'
import { statusClass } from '../format'
import type { PilotBranch } from '../pilot-data-types'

export function BranchDetail({ branch, defaultOpen }: { branch: PilotBranch; defaultOpen: boolean }) {
  return (
    <details className="pilot-detail" id={`pilot-detail-${branch.letter}`} open={defaultOpen}>
      <summary>
        <span>{branch.letter}</span>
        <strong>{branch.title}</strong>
        <em className={`pilot-pill ${statusClass(branch.status)}`}>{branch.statusLabel}</em>
      </summary>
      <div className="pilot-detail-body">
        {branch.thesis && <Field label="论点">{branch.thesis}</Field>}
        {branch.content && (
          <div className="pilot-content-block">
            <div className="pilot-content-head">正文版本 <code>{branch.contentMeta}</code></div>
            <pre>{branch.content}</pre>
          </div>
        )}
        {branch.versions && (
          <div className="pilot-version-stack">
            {branch.versions.map((version, index) => (
              <div className={`pilot-mini-version ${statusClass(version.status)}`} key={version.letter}>
                <div className="pilot-mini-head">
                  <strong>{version.letter}</strong>
                  <span>{version.statusLabel}</span>
                  {version.contentMeta && <code>{version.contentMeta}</code>}
                </div>
                {version.image_ref && (
                  <img className="pilot-graph-node-image" src={version.image_ref} alt={`${version.letter} ${version.statusLabel}`} loading="lazy" />
                )}
                {index > 0 && version.diff && <div className="pilot-mini-diff">{version.diff.summary}</div>}
                {version.reviews?.map(review => (
                  <div className="pilot-version-review" key={`${version.letter}-${review.who}`}>
                    <div>
                      <b className={`pilot-review-${review.verdict}`}>{review.verdictText}</b>
                      <strong>{review.who}</strong>
                    </div>
                    {review.quote && <span>{review.quote}</span>}
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
        {branch.attempts && <Field label="尝试">{branch.attempts}</Field>}
        {branch.result && <Field label="结果">{branch.result}</Field>}
        {branch.evidence && (
          <div className="pilot-evidence">
            {branch.evidence.map(e => <blockquote key={e.line}>{e.line}</blockquote>)}
          </div>
        )}
        {branch.reviews && (
          <div className="pilot-reviews">
            {branch.reviews.map(review => (
              <div className="pilot-review-row" key={review.who}>
                <b className={`pilot-review-${review.verdict}`}>{review.verdictText}</b>
                <strong>{review.who}</strong>
                <span>{review.quote}</span>
              </div>
            ))}
          </div>
        )}
        {branch.diff && (
          <div className="pilot-diff">
            <div className="pilot-diff-summary">{branch.diff.summary}</div>
            <ul>
              {branch.diff.changes.map(change => (
                <li className={`pilot-change-${change.type}`} key={`${change.type}-${change.text}`}>{change.text}</li>
              ))}
            </ul>
          </div>
        )}
        {branch.decision && <Field label="决定">{branch.decision}</Field>}
      </div>
    </details>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="pilot-field">
      <div className="pilot-field-label">{label}</div>
      <div className="pilot-field-val">{children}</div>
    </div>
  )
}
