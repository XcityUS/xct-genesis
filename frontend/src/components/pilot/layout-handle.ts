// Single-import handle for layout primitives. Re-exported as `layout` so
// PilotRenderer can use `layout.Section` without polluting its imports list
// when more layout helpers (Toolbar, ColumnSplit, etc) appear.

import { Section } from './layout/Section'

export const layout = { Section }
