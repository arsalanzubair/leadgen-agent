# _reference/ — archived, not part of the app

Nothing in this folder is compiled, imported, or reachable from the running
product. It sits outside `src/`, so Vite never sees it and `tsc -b` never
type-checks it.

## graph-view/

The pipeline visualisation that used to live at `/agent/runs/:id`: a React Flow
canvas of the backend's 13 processing steps, with a per-step inspector.

It was removed from the product deliberately. The dashboard is for people who
run outreach campaigns, not for people who maintain the pipeline — and a
diagram of internal processing steps is the single clearest way to make a
business tool feel like somebody's debugging console. There is no "advanced
mode" that brings it back; the same information is now expressed as plain
sequential progress ("Finding businesses" → "Checking how well they match").

Kept only because the topology transcription was verified against the real
backend and would be tedious to redo. If you ever want it back, note that it
depends on `@xyflow/react`, which is no longer a dependency.
