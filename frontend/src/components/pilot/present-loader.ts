// Fetches and validates present.json for a workspace. Hand-rolled assertion
// (zod is not in the package) — goal is to fail loudly with a friendly error
// instead of letting a malformed JSON crash deep inside compactGraphModel.

import type { PilotDataset } from './pilot-data-types'

export type PresentLoadResult =
  | { ok: true; data: PilotDataset }
  | { ok: false; status: 'not_found' | 'invalid' | 'network'; message: string }

export async function loadPresentDataset(workspaceId: string, signal?: AbortSignal): Promise<PresentLoadResult> {
  const url = `/workspaces/${encodeURIComponent(workspaceId)}/present.json`
  let response: Response
  try {
    response = await fetch(url, { signal })
  } catch (err) {
    if (signal?.aborted) throw err
    return { ok: false, status: 'network', message: `加载失败: ${(err as Error).message}` }
  }
  if (response.status === 404) {
    return { ok: false, status: 'not_found', message: `未找到 workspace「${workspaceId}」的 present.json` }
  }
  if (!response.ok) {
    return { ok: false, status: 'network', message: `HTTP ${response.status} 来自 ${url}` }
  }
  let raw: unknown
  try {
    raw = await response.json()
  } catch (err) {
    return { ok: false, status: 'invalid', message: `present.json 不是合法 JSON: ${(err as Error).message}` }
  }
  const issue = validateDataset(raw)
  if (issue) return { ok: false, status: 'invalid', message: `present.json 缺少必需字段: ${issue}` }
  return { ok: true, data: raw as PilotDataset }
}

function validateDataset(raw: unknown): string | null {
  if (!raw || typeof raw !== 'object') return 'root must be an object'
  const obj = raw as Record<string, unknown>
  for (const key of ['title', 'subtitle', 'meta', 'verdict', 'panel', 'rules', 'branchMap', 'branches', 'confidence']) {
    if (!(key in obj)) return key
  }
  if (!Array.isArray(obj.branches)) return 'branches must be an array'
  if (!obj.branchMap || typeof obj.branchMap !== 'object') return 'branchMap must be an object'
  return null
}
