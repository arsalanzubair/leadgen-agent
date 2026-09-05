/**
 * NodeInspector.tsx -- the side panel for a selected graph node.
 *
 * Shows what the node does, what it wrote to the lead, and how it performed on
 * this run. The `graphNode` name and module filename are included on purpose:
 * an operator debugging a run reads Python logs, and those log lines use the
 * graph node name, not "N5.5".
 */

import { ExternalLink, X } from "lucide-react";

import { Badge, Divider, Num } from "@/components/ui/primitives";
import { formatDuration, formatTime, nodeStatusLabel, nodeStatusTone } from "@/lib/format";
import { NODE_BY_ID } from "@/lib/graphSpec";
import { cn } from "@/lib/utils";
import type { NodeId, NodeRunState } from "@/types/agent";

export function NodeInspector({
  nodeId,
  state,
  onClose,
}: {
  nodeId: NodeId;
  state: NodeRunState | undefined;
  onClose: () => void;
}) {
  const spec = NODE_BY_ID[nodeId];
  if (!spec) return null;

  const status = state?.status ?? "waiting";

  return (
    <div className="flex h-full flex-col overflow-hidden border-l border-border bg-surface">
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3.5">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Num className="rounded border border-border bg-surface-raised px-1.5 py-0.5 text-micro text-secondary">
              {spec.id}
            </Num>
            <Badge tone={nodeStatusTone(status)} dot>
              {nodeStatusLabel(status)}
            </Badge>
          </div>
          <h3 className="mt-2 text-body font-medium text-primary">{spec.label}</h3>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close inspector"
          className="rounded-control p-1 text-secondary transition-colors duration-150 hover:bg-surface-raised hover:text-primary"
        >
          <X size={15} />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        <p className="text-meta leading-[19px] text-secondary">{spec.description}</p>

        {spec.humanInLoop ? (
          <div className="mt-3 rounded-control border border-warning/25 bg-warning-muted px-3 py-2">
            <p className="text-micro text-warning">
              {spec.id === "N6b"
                ? "LinkedIn is never automated. This node prepares the message; a person sends it."
                : "This node pauses the lead until a person decides. The pause is checkpointed, so it survives a restart."}
            </p>
          </div>
        ) : null}

        <Divider className="my-4" />

        {/* This run's performance. */}
        <h4 className="mb-2.5 text-meta font-medium text-primary">This run</h4>
        <dl className="space-y-2">
          <Row label="Duration" value={formatDuration(state?.duration_ms ?? null)} mono />
          <Row label="Leads in" value={state ? String(state.leads_in) : "-"} mono />
          <Row label="Leads out" value={state ? String(state.leads_out) : "-"} mono />
          <Row
            label="Routed off"
            value={state ? String(state.leads_diverted) : "-"}
            mono
            tone={state && state.leads_diverted > 0 ? "danger" : undefined}
          />
          <Row label="Started" value={formatTime(state?.started_at ?? null)} mono />
          <Row label="Finished" value={formatTime(state?.finished_at ?? null)} mono />
        </dl>

        {state?.note ? (
          <p className="mt-3 rounded-control border border-border bg-bg px-3 py-2 text-meta text-secondary">
            {state.note}
          </p>
        ) : null}

        <Divider className="my-4" />

        <h4 className="mb-2.5 text-meta font-medium text-primary">
          Writes to the lead
        </h4>
        <div className="flex flex-wrap gap-1.5">
          {spec.outputs.map((output) => (
            <code
              key={output}
              className="num rounded border border-border bg-bg px-1.5 py-0.5 text-micro text-secondary"
            >
              {output}
            </code>
          ))}
        </div>

        <Divider className="my-4" />

        <h4 className="mb-2.5 text-meta font-medium text-primary">In the codebase</h4>
        <dl className="space-y-2">
          <Row label="Graph node" value={spec.graphNode} mono />
          <Row label="Module" value={`src/nodes/${spec.module}`} mono />
          <Row label="Calls an LLM" value={spec.usesLlm ? "yes" : "no"} />
        </dl>

        <p className="mt-4 flex items-start gap-1.5 text-micro text-tertiary">
          <ExternalLink size={11} className="mt-0.5 shrink-0" />
          Python log lines for this node use the name{" "}
          <code className="num text-secondary">{spec.graphNode}</code>.
        </p>
      </div>
    </div>
  );
}

function Row({
  label,
  value,
  mono = false,
  tone,
}: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: "danger";
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-meta text-tertiary">{label}</dt>
      <dd
        className={cn(
          "text-right text-meta",
          mono && "num",
          tone === "danger" ? "text-danger" : "text-primary",
        )}
      >
        {value}
      </dd>
    </div>
  );
}
