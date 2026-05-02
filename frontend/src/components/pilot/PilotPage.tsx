// PilotPage — the /pilot route entry. Hosts the demo dataset switcher,
// fetches the latest live autoresearch run on mount, and renders into the
// pure PilotRenderer. All section logic lives in components/pilot/sections/.

import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { PilotRenderer } from './PilotRenderer'
import { PILOT_DATA, PILOT_DATASETS } from './pilot-data-fixtures'
import type { PilotDataset } from './pilot-data-types'
import { fetchLatestAutoresearchPilotDataset } from './pilot-run-data'
import '@/styles/pilot.css'

export default function PilotPage() {
  const [key, setKey] = useState<keyof typeof PILOT_DATA>('autoresearch')
  const [latestAutoresearch, setLatestAutoresearch] = useState<PilotDataset | null>(null)
  const [latestState, setLatestState] = useState<'loading' | 'api' | 'snapshot' | 'demo'>('loading')
  const data = key === 'autoresearch' && latestAutoresearch ? latestAutoresearch : PILOT_DATA[key]
  const switcherMeta = key === 'autoresearch'
    ? latestState === 'loading'
      ? 'loading latest autoresearch run...'
      : latestState === 'api'
        ? `live api · ${data.meta}`
        : latestState === 'snapshot'
          ? `local snapshot · ${data.meta}`
          : `demo fallback · ${data.meta}`
    : data.meta

  useEffect(() => {
    const controller = new AbortController()
    fetchLatestAutoresearchPilotDataset(controller.signal)
      .then(result => {
        if (controller.signal.aborted) return
        setLatestAutoresearch(result?.dataset || null)
        setLatestState(result?.source || 'demo')
      })
      .catch(() => {
        if (controller.signal.aborted) return
        setLatestAutoresearch(null)
        setLatestState('demo')
      })
    return () => controller.abort()
  }, [])

  return (
    <div className="pilot-page">
      <div className="pilot-switcher">
        <span>视图类型:</span>
        <div>
          {PILOT_DATASETS.map(item => (
            <Button key={item.key} size="sm" variant={item.key === key ? 'default' : 'outline'} onClick={() => setKey(item.key as keyof typeof PILOT_DATA)}>
              {item.label}
            </Button>
          ))}
        </div>
        <code>{switcherMeta}</code>
      </div>
      <PilotRenderer data={data} />
    </div>
  )
}
