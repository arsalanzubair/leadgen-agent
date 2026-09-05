/**
 * PipelineGraph.tsx -- the N0-N9 graph, in React Flow.
 *
 * This is the product's differentiator, so it gets bespoke node rendering
 * rather than React Flow's defaults:
 *
 *   - each node shows its id in mono, its label, and its lead counts, so the
 *     graph doubles as the run's numbers;
 *   - diverted leads (archived / manual review) are shown ON the node that
 *     diverted them, because "where did the other 4 go" is the question the
 *     graph should answer;
 *   - only the currently-active edge animates. Animating everything would make
 *     a finished run look like it were still working;
 *   - branch edges (below-threshold to archive, N5.5's split) are dashed and
 *     dimmer so the main spine reads first.
 */

import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Bot, Check, CircleDot, Loader2, UserCheck } from "lucide-react";
import * as React from "react";

import { Num } from "@/components/ui/primitives";
import { GRAPH_EDGES, GRAPH_NODES, NODE_POSITIONS } from "@/lib/graphSpec";
import { cn } from "@/lib/utils";
import type { NodeId, NodeRunState, NodeStatus } from "@/types/agent";

// --------------------------------------------------------------------------- //
// The node
// --------------------------------------------------------------------------- //

interface PipelineNodeData extends Record<string, unknown> {
  nodeId: NodeId;
  label: string;
  status: NodeStatus;
  leadsIn: number;
  leadsOut: number;
  leadsDiverted: number;
  usesLlm: boolean;
  humanInLoop: boolean;
  selected: boolean;
}

type Side = "left" | "right" | "top" | "bottom";

const SIDE: Record<Side, Position> = {
  left: Position.Left,
  right: Position.Right,
  top: Position.Top,
  bottom: Position.Bottom,
};

/**
 * Which sides an edge should leave and enter by, given where the two nodes sit.
 * Same row means a horizontal hop; different rows mean a vertical one. Without
 * this every edge would leave the right side and re-enter the left, which in a
 * snake layout produces loops that read as errors.
 */
function pickSides(
  from: { x: number; y: number },
  to: { x: number; y: number },
): { source: Side; target: Side } {
  const sameRow = Math.abs(from.y - to.y) < 90;
  if (sameRow) {
    return to.x >= from.x
      ? { source: "right", target: "left" }
      : { source: "left", target: "right" };
  }
  const sameColumn = Math.abs(from.x - to.x) < 90;
  if (sameColumn) {
    return to.y > from.y
      ? { source: "bottom", target: "top" }
      : { source: "top", target: "bottom" };
  }
  // Diagonal: leave by the dominant horizontal direction, arrive vertically.
  return to.y > from.y
    ? { source: to.x >= from.x ? "right" : "left", target: "top" }
    : { source: to.x >= from.x ? "right" : "left", target: "bottom" };
}

/** Which edges are worth a label. */
function showLabel(edge: { kind: string; label: string }): boolean {
  if (!edge.label) return false;
  return edge.kind === "branch" || edge.label === "email" || edge.label === "linkedin";
}

const STATUS_RING: Record<NodeStatus, string> = {
  completed: "border-[#2f4a44] bg-[#111a17]",
  running: "border-accent bg-[#171634]",
  waiting: "border-border bg-surface",
  skipped: "border-border bg-surface opacity-55",
  failed: "border-danger/50 bg-[#1e1416]",
};

function PipelineNode({ data }: NodeProps<Node<PipelineNodeData>>) {
  const {
    nodeId,
    label,
    status,
    leadsIn,
    leadsOut,
    leadsDiverted,
    usesLlm,
    humanInLoop,
    selected,
  } = data;

  return (
    <div
      className={cn(
        "w-[160px] rounded-card border px-3 py-2.5 transition-colors duration-150",
        STATUS_RING[status],
        selected && "ring-2 ring-accent ring-offset-2 ring-offset-[#0B0D10]",
      )}
    >
      {/* A handle on each side, both directions. The snake layout needs to
          enter and leave a node from whichever side the next one actually is;
          they are invisible (see index.css) so this costs nothing visually. */}
      {(["left", "right", "top", "bottom"] as const).map((side) => (
        <React.Fragment key={side}>
          <Handle id={`t-${side}`} type="target" position={SIDE[side]} />
          <Handle id={`s-${side}`} type="source" position={SIDE[side]} />
        </React.Fragment>
      ))}

      <div className="flex items-center justify-between gap-2">
        <Num
          className={cn(
            "text-micro font-medium",
            status === "running"
              ? "text-accent-text"
              : status === "completed"
                ? "text-success"
                : status === "failed"
                  ? "text-danger"
                  : "text-tertiary",
          )}
        >
          {nodeId}
        </Num>
        <StatusGlyph status={status} />
      </div>

      <p
        className={cn(
          "mt-1 truncate text-meta font-medium",
          status === "waiting" || status === "skipped" ? "text-secondary" : "text-primary",
        )}
        title={label}
      >
        {label}
      </p>

      {/* Counts. Only rendered once the node has actually processed something,
          so a waiting node does not display a misleading row of zeroes. */}
      {status !== "waiting" && status !== "skipped" && (leadsIn > 0 || leadsOut > 0) ? (
        <div className="mt-2 flex items-center gap-2 border-t border-border/70 pt-1.5 text-micro">
          <span className="text-tertiary">
            in <Num className="text-secondary">{leadsIn}</Num>
          </span>
          <span className="text-tertiary">
            out <Num className="text-secondary">{leadsOut}</Num>
          </span>
          {leadsDiverted > 0 ? (
            <span className="ml-auto text-danger" title="Routed to archive or manual review">
              <Num>-{leadsDiverted}</Num>
            </span>
          ) : null}
        </div>
      ) : null}

      {/* The two markers that explain the pipeline's character: which nodes
          think, and which nodes need a person. */}
      {usesLlm || humanInLoop ? (
        <div className="mt-1.5 flex items-center gap-1.5">
          {usesLlm ? (
            <span
              className="inline-flex items-center gap-1 text-[10px] text-tertiary"
              title="This node calls the LLM"
            >
              <Bot size={10} />
              LLM
            </span>
          ) : null}
          {humanInLoop ? (
            <span
              className="inline-flex items-center gap-1 text-[10px] text-warning"
              title="This node requires a person"
            >
              <UserCheck size={10} />
              human
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function StatusGlyph({ status }: { status: NodeStatus }) {
  if (status === "completed") return <Check size={13} className="text-success" />;
  if (status === "running")
    return <Loader2 size={13} className="animate-spin text-accent-text" />;
  if (status === "failed") return <CircleDot size={13} className="text-danger" />;
  return (
    <span
      className="h-2.5 w-2.5 rounded-full border border-border-strong"
      title={status}
    />
  );
}

const nodeTypes = { pipeline: PipelineNode };

// --------------------------------------------------------------------------- //
// The graph
// --------------------------------------------------------------------------- //

export function PipelineGraph({
  nodeStates,
  selectedNode,
  onSelectNode,
  className,
}: {
  nodeStates: NodeRunState[];
  selectedNode: NodeId | null;
  onSelectNode: (id: NodeId | null) => void;
  className?: string;
}) {
  const stateById = React.useMemo(() => {
    const map = new Map<NodeId, NodeRunState>();
    for (const state of nodeStates) map.set(state.node_id, state);
    return map;
  }, [nodeStates]);

  const nodes = React.useMemo<Node<PipelineNodeData>[]>(
    () =>
      GRAPH_NODES.map((spec) => {
        const state = stateById.get(spec.id);
        return {
          id: spec.id,
          type: "pipeline",
          position: NODE_POSITIONS[spec.id],
          data: {
            nodeId: spec.id,
            label: spec.label,
            status: state?.status ?? "waiting",
            leadsIn: state?.leads_in ?? 0,
            leadsOut: state?.leads_out ?? 0,
            leadsDiverted: state?.leads_diverted ?? 0,
            usesLlm: spec.usesLlm,
            humanInLoop: spec.humanInLoop,
            selected: selectedNode === spec.id,
          },
          draggable: false,
        };
      }),
    [stateById, selectedNode],
  );

  const edges = React.useMemo<Edge[]>(
    () =>
      GRAPH_EDGES.filter((edge) => edge.layoutable).map((edge) => {
        const sides = pickSides(NODE_POSITIONS[edge.from], NODE_POSITIONS[edge.to]);
        const fromStatus = stateById.get(edge.from)?.status ?? "waiting";
        const toStatus = stateById.get(edge.to)?.status ?? "waiting";

        // Active means: the lead flow is crossing this edge right now.
        const active = fromStatus === "completed" && toStatus === "running";
        const complete =
          fromStatus === "completed" && (toStatus === "completed" || toStatus === "failed");

        return {
          id: `${edge.from}->${edge.to}`,
          source: edge.from,
          target: edge.to,
          sourceHandle: `s-${sides.source}`,
          targetHandle: `t-${sides.target}`,
          type: "smoothstep",
          // Labels only on the branches and the channel split. Labelling every
          // edge ("approved", "no reply", "cadence end") crowded the diagram
          // without telling the reader anything the node names do not.
          label: showLabel(edge) ? edge.label : undefined,
          labelShowBg: true,
          labelBgStyle: { fill: "#12151A", fillOpacity: 0.95 },
          labelBgPadding: [4, 2] as [number, number],
          labelBgBorderRadius: 3,
          labelStyle: {
            fill: "#8b93a1",
            fontSize: 10,
            fontFamily: "JetBrains Mono, monospace",
          },
          className: cn(
            edge.kind === "branch" && "is-branch",
            active && "is-active",
            complete && !active && "is-complete",
          ),
        } satisfies Edge;
      }),
    [stateById],
  );

  return (
    <div className={cn("relative h-full w-full", className)}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_, node) => onSelectNode(node.id as NodeId)}
        onPaneClick={() => onSelectNode(null)}
        fitView
        fitViewOptions={{ padding: 0.12, minZoom: 0.45, maxZoom: 1 }}
        minZoom={0.4}
        maxZoom={1.6}
        nodesConnectable={false}
        nodesDraggable={false}
        elementsSelectable
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={22}
          size={1}
          color="#20242b"
        />
        <Controls
          showInteractive={false}
          className="!bottom-4 !left-4 overflow-hidden !rounded-control !border !border-border !shadow-none"
        />
      </ReactFlow>

      <GraphLegend />
    </div>
  );
}

/** ✓ completed / ● running / ○ waiting, plus the branch-edge convention. */
function GraphLegend() {
  return (
    <div className="pointer-events-none absolute right-4 top-4 rounded-card border border-border bg-surface/90 px-3 py-2 backdrop-blur">
      <ul className="space-y-1 text-micro">
        <li className="flex items-center gap-2 text-secondary">
          <Check size={11} className="text-success" />
          completed
        </li>
        <li className="flex items-center gap-2 text-secondary">
          <span className="h-2 w-2 rounded-full bg-accent" />
          running
        </li>
        <li className="flex items-center gap-2 text-secondary">
          <span className="h-2 w-2 rounded-full border border-border-strong" />
          waiting
        </li>
        <li className="mt-1.5 flex items-center gap-2 border-t border-border pt-1.5 text-tertiary">
          <svg width="18" height="4" aria-hidden="true">
            <line
              x1="0"
              y1="2"
              x2="18"
              y2="2"
              stroke="#2b3038"
              strokeWidth="1.5"
              strokeDasharray="2 3"
            />
          </svg>
          branch to archive
        </li>
      </ul>
    </div>
  );
}
