// Internal pilot types — graph + key-deliverable view-model shapes used across
// PilotRenderer's section / graph subcomponents. Public dataset types live in
// pilot-data-types.ts; these are derived for rendering only.

import type { PilotBranch, PilotDeliverableKind, PilotStatus, PilotVersion } from './pilot-data-types'

export type GraphStatus = PilotStatus | 'collapsed'

export type GraphVersion = PilotVersion & {
  index: number
}

export type GraphBranch = Omit<PilotBranch, 'status' | 'versions'> & {
  id: string
  index: number
  status: GraphStatus
  hiddenCount?: number
  isCollapsed?: boolean
  versions: GraphVersion[]
  visibleVersions?: GraphVersion[]
}

export interface GraphModel {
  shape: 'wide' | 'deep' | 'tree' | 'mixed'
  branches: GraphBranch[]
  visibleBranches: GraphBranch[]
  visibleVersions: GraphBranch[]
  mode: 'full' | 'scroll' | 'dense-scroll' | 'collapsed'
  hidden: { branches: number; versions: number; descendants: number }
  scale: {
    branchCount: number
    versionCount: number
    maxVersionsPerBranch: number
  }
}

export const GRAPH_LIMITS = {
  wideFullBranches: 5,
  wideDenseBranches: 18,
  deepFullVersions: 6,
  deepKeyVersions: 6,
  mixedFullVersionsPerLane: 6,
  mixedKeyVersionsPerLane: 5,
  treeFullNodes: 36,
  treeMaxDepth: 3,
  treeMaxChildrenPerNode: 4,
} as const

export type KeyDeliverableCard = {
  id: string
  href?: string
  kind: PilotDeliverableKind
  name: string
  status: PilotStatus
  statusText: string
  metric: string
  why: string
  source: string
  score: number
  order: number
  timeLabel: string
  assets: KeyDeliverableAsset[]
  imageRef?: string
}

export type KeyDeliverableAsset = {
  id: string
  label: string
  kind: PilotDeliverableKind
  tone: 'primary' | 'evidence' | 'relation' | 'version' | 'review'
}

export type DeliverableSortMode = 'recommended' | 'timeline' | 'type'
export type DeliverableFilterMode = 'all' | PilotDeliverableKind
