// Reusable <section> wrapper used by every pilot top-level section.
// Provides the auto-numbered counter via the .pilot-section CSS class.

import type { ReactNode } from 'react'

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="pilot-section">
      <h2>{title}</h2>
      {children}
    </section>
  )
}
