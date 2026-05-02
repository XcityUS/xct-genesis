// Graph dispatcher + zoom controls + DiagramFigure wrapper.
// Routes to the right shape sub-renderer (wide / deep / tree / mixed) based on
// `model.shape` and provides the zoom toolbar around it. The zoom-scroll
// container supports pointer-drag panning (in addition to scroll bars and
// touchpad swipe), and suppresses the click that would otherwise fire on a
// nested <a> when the gesture moved past a small threshold.

import { useMemo, useRef, useState } from 'react'
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react'
import { graphCaption } from '../format'
import type { PilotDataset } from '../pilot-data-types'
import type { GraphModel } from '../types'
import { compactGraphModel } from './model'
import { DeepGraph } from './DeepGraph'
import { MixedGraph } from './MixedGraph'
import { TreeGraph } from './TreeGraph'
import { WideGraph } from './WideGraph'

const DRAG_CLICK_THRESHOLD = 5

export function Graph({ data }: { data: PilotDataset }) {
  const model = useMemo(() => compactGraphModel(data), [data])
  const [zoom, setZoom] = useState(1)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<{ pointerId: number; startX: number; startY: number; scrollLeft: number; scrollTop: number; xTarget: HTMLElement; yTarget: HTMLElement; moved: boolean } | null>(null)
  const [isDragging, setIsDragging] = useState(false)

  const updateZoom = (next: number) => setZoom(Math.min(1.6, Math.max(0.55, Math.round(next * 100) / 100)))

  // Each shape has its own inner scroller (.pilot-graph-scroll / .pilot-tree-scroll)
  // that absorbs canvas overflow, so the outer .pilot-graph-zoom-scroll only sees
  // overflow when zoom > 1. Pick whichever element actually has scrollable distance
  // along each axis at pointerdown time.
  const pickScrollTargets = (outer: HTMLElement) => {
    const inner = outer.querySelector<HTMLElement>('.pilot-graph-scroll, .pilot-tree-scroll, .pilot-lane-track')
    const xTarget = inner && inner.scrollWidth > inner.clientWidth ? inner : outer
    const yTarget = outer.scrollHeight > outer.clientHeight ? outer : (inner ?? outer)
    return { xTarget, yTarget }
  }

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const node = scrollRef.current
    if (!node) return
    const { xTarget, yTarget } = pickScrollTargets(node)
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      scrollLeft: xTarget.scrollLeft,
      scrollTop: yTarget.scrollTop,
      xTarget,
      yTarget,
      moved: false,
    }
    node.setPointerCapture(event.pointerId)
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const dx = event.clientX - drag.startX
    const dy = event.clientY - drag.startY
    if (!drag.moved && Math.hypot(dx, dy) > DRAG_CLICK_THRESHOLD) {
      drag.moved = true
      setIsDragging(true)
    }
    if (drag.moved) {
      drag.xTarget.scrollLeft = drag.scrollLeft - dx
      drag.yTarget.scrollTop = drag.scrollTop - dy
    }
  }

  const handlePointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    const node = scrollRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    if (node && node.hasPointerCapture(event.pointerId)) node.releasePointerCapture(event.pointerId)
    dragRef.current = null
    setIsDragging(false)
  }

  // Suppress the synthetic click that follows a drag past the threshold, so
  // panning over a deliverable card doesn't navigate to its detail anchor.
  const handleClickCapture = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (isDragging) {
      event.preventDefault()
      event.stopPropagation()
    }
  }

  return (
    <div className="pilot-graph-viewport" style={{ '--pilot-graph-zoom': zoom } as CSSProperties}>
      <div className="pilot-graph-toolbar" aria-label="图谱缩放控制">
        <span>{graphToolbarLabel(model)}</span>
        <div>
          <button type="button" onClick={() => updateZoom(zoom - 0.1)} aria-label="缩小">-</button>
          <output>{Math.round(zoom * 100)}%</output>
          <button type="button" onClick={() => updateZoom(zoom + 0.1)} aria-label="放大">+</button>
          <button type="button" onClick={() => updateZoom(1)}>fit</button>
        </div>
      </div>
      <div
        ref={scrollRef}
        className={`pilot-graph-zoom-scroll${isDragging ? ' is-dragging' : ''}`}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
        onClickCapture={handleClickCapture}
      >
        <div className="pilot-graph-zoom-inner">
          <GraphBody model={model} />
        </div>
      </div>
    </div>
  )
}

function GraphBody({ model }: { model: GraphModel }) {
  if (model.shape === 'wide') return <WideGraph model={model} />
  if (model.shape === 'deep') return <DeepGraph model={model} />
  if (model.shape === 'tree') return <TreeGraph model={model} />
  return <MixedGraph model={model} />
}

function graphToolbarLabel(model: GraphModel) {
  if (model.shape === 'wide') return `${model.scale.branchCount} branches`
  if (model.shape === 'deep') return `${model.scale.branchCount} versions`
  if (model.shape === 'tree') return `${model.scale.branchCount} nodes`
  return `${model.scale.branchCount} lanes · ${model.scale.versionCount} versions`
}

export function DiagramFigure({ data }: { data: PilotDataset }) {
  return (
    <figure className="pilot-graph-figure">
      <div className="pilot-graph-box"><Graph data={data} /></div>
      <figcaption>{graphCaption(data)}</figcaption>
    </figure>
  )
}
