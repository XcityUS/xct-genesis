// PresentPage — workspace-driven view at /present/:workspaceId. Fetches
// /workspaces/{id}/present.json, validates shape, renders into the same
// PilotRenderer used by /pilot.

import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { PilotRenderer } from './PilotRenderer'
import type { PilotDataset } from './pilot-data-types'
import { loadPresentDataset } from './present-loader'
import '@/styles/pilot.css'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; data: PilotDataset }
  | { kind: 'error'; status: 'not_found' | 'invalid' | 'network'; message: string }

export default function PresentPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>()
  const [state, setState] = useState<LoadState>({ kind: 'loading' })

  useEffect(() => {
    if (!workspaceId) {
      setState({ kind: 'error', status: 'not_found', message: '缺少 workspace id' })
      return
    }
    const controller = new AbortController()
    setState({ kind: 'loading' })
    loadPresentDataset(workspaceId, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return
        if (result.ok === false) {
          setState({ kind: 'error', status: result.status, message: result.message })
          return
        }
        setState({ kind: 'ready', data: result.data })
      })
      .catch(err => {
        if (controller.signal.aborted) return
        setState({ kind: 'error', status: 'network', message: (err as Error).message })
      })
    return () => controller.abort()
  }, [workspaceId])

  if (state.kind === 'loading') {
    return (
      <div className="pilot-page">
        <div className="pilot-switcher"><span>workspace</span><code>{workspaceId} · loading present.json…</code></div>
      </div>
    )
  }
  if (state.kind === 'error') {
    return (
      <div className="pilot-page">
        <div className="pilot-switcher"><span>workspace</span><code>{workspaceId} · {state.status}</code></div>
        <main className="pilot-doc">
          <header className="pilot-header">
            <div>WORKSPACE NOT FOUND</div>
            <h1>{state.message}</h1>
            <p>请确认 ~/.worldseed/workspaces/{workspaceId}/present.json 已生成。</p>
          </header>
        </main>
      </div>
    )
  }
  return (
    <div className="pilot-page">
      <div className="pilot-switcher"><span>workspace</span><code>{workspaceId} · live present.json</code></div>
      <PilotRenderer data={state.data} />
    </div>
  )
}
