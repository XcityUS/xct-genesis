// 后续动作 — buttons row from data.verdict.actions (default: 分享链接 / 导出 PDF).

import { Button } from '@/components/ui/button'
import type { PilotDataset } from '../pilot-data-types'

export function Actions({ data }: { data: PilotDataset }) {
  return (
    <div className="pilot-continue">
      {(data.verdict.actions || ['分享链接', '导出 PDF']).map((action, index) => (
        <Button key={action} variant={index === 0 ? 'default' : 'outline'}>{action}</Button>
      ))}
    </div>
  )
}
