// PilotDataset types — split from pilot-data.ts so PresentPage / PilotRenderer
// can import types without bundling the demo fixtures.

export type PilotShape = 'wide' | 'deep' | 'tree' | 'mixed'
export type PilotStatus = 'chosen' | 'killed' | 'parked'
export type PilotDeliverableKind = 'paper' | 'pending_result' | 'commit' | 'memo' | 'result_table' | 'artifact' | 'track'

export interface PilotRelation {
  target: string
  kind: 'cites' | 'builds_on' | 'supersedes' | 'verifies' | 'derived_from' | 'materialized_by' | 'text_reference'
  confidence?: 'field' | 'inferred'
}

export interface PilotVersionReview {
  who: string
  verdict: 'accept' | 'reject' | 'pending' | string
  verdictText: string
  quote?: string
}

export interface PilotVersion {
  letter: string
  status: PilotStatus
  statusLabel: string
  contentMeta?: string
  diff?: { summary: string; changes?: Array<{ type: string; text: string }> }
  image_ref?: string
  reviews?: PilotVersionReview[]
}

export interface PilotBranch {
  letter: string
  title: string
  kind?: PilotDeliverableKind
  status: PilotStatus
  statusLabel: string
  createdTick?: number
  parent?: string | null
  relations?: PilotRelation[]
  thesis?: string
  attempts?: string
  result?: string
  evidence?: Array<{ line: string; qt?: boolean }>
  decision?: string
  content?: string
  contentMeta?: string
  reviews?: Array<{ who: string; verdict: string; verdictText: string; quote: string }>
  diff?: { summary: string; changes: Array<{ type: string; text: string }> }
  versions?: PilotVersion[]
  image_ref?: string
}

export interface PilotDeliverable {
  icon: string
  name: string
  meta: string
  image_ref?: string
}

export interface PilotStoryMoment {
  tick?: number
  time?: string
  actor?: string
  label: string
  quote?: string
}

export interface PilotStory {
  intro?: string
  moments?: PilotStoryMoment[]
}

export interface PilotDataset {
  eyebrow: string
  title: string
  subtitle: string
  meta: string
  story?: PilotStory
  verdict: {
    lead: string
    bullets?: string[]
    recommend?: string
    deliverables?: PilotDeliverable[]
    actions?: string[]
  }
  question: Record<string, string>
  panel: Array<{ avatar: string; name: string; bio: string }>
  rules: string[]
  branchMap: { shape: PilotShape }
  branches: PilotBranch[]
  confidence: {
    tested: string[]
    untested: string[]
    next: string[]
  }
}
