// PilotRenderer — pure presentational shell that composes the 8 pilot
// sections from a PilotDataset prop. Used by:
//   - PilotPage (the demo switcher at /pilot)
//   - PresentPage (workspace-driven view at /present/:workspaceId, phase 3)
//
// No fetch, no side effects, no localStorage. Section state (zoom, sort,
// filter) lives inside the section components themselves, not here.

import { detailSectionTitle, graphSectionTitle } from './format'
import { DiagramFigure } from './graph/Graph'
import { layout as Layout } from './layout-handle'
import type { PilotDataset } from './pilot-data-types'
import { Actions } from './sections/Actions'
import { BranchDetail } from './sections/BranchDetail'
import { Confidence } from './sections/Confidence'
import { KeyDeliverables } from './sections/KeyDeliverables'
import { Masthead } from './sections/Masthead'
import { Story } from './sections/Story'
import { StudyContext } from './sections/StudyContext'
import { Verdict } from './sections/Verdict'

export function PilotRenderer({ data }: { data: PilotDataset }) {
  const { Section } = Layout
  return (
    <main className="pilot-doc">
      <Masthead data={data} />

      {data.story && (
        <Section title="故事线">
          <Story data={data} />
        </Section>
      )}

      <Section title="决策摘要">
        <Verdict data={data} />
      </Section>

      <Section title="关键产物">
        <KeyDeliverables data={data} />
      </Section>

      <Section title={graphSectionTitle(data)}>
        <DiagramFigure data={data} />
      </Section>

      <Section title={detailSectionTitle(data)}>
        {data.branches.map((branch, index) => (
          <BranchDetail branch={branch} defaultOpen={index === 0} key={branch.letter} />
        ))}
      </Section>

      <Section title="研究上下文">
        <StudyContext data={data} />
      </Section>

      <Section title="置信度边界">
        <Confidence data={data} />
      </Section>

      <Section title="后续动作">
        <Actions data={data} />
      </Section>
    </main>
  )
}
