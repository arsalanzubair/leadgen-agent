/**
 * AgentRun.tsx -- one run, as a graph.
 *
 * The graph is the page: it gets the full width and a tall fixed viewport,
 * with the node inspector as a side panel rather than a modal so the topology
 * stays visible while you read a node.
 */

import { ArrowLeft, Eye, Radio, TriangleAlert } from "lucide-react";
import * as React from "react";
import { Link, useParams } from "react-router-dom";

import { PageHeader } from "@/components/layout/AppShell";
import { NodeInspector } from "@/components/agent/NodeInspector";
import { PipelineGraph } from "@/components/agent/PipelineGraph";
import { Badge, Button, Card, Num } from "@/components/ui/primitives";
import { EmptyState, ErrorState, NodeProgress, Skeleton } from "@/components/ui/states";
import { useAsync } from "@/hooks/useAsync";
import { useTenant } from "@/hooks/useTenant";
import { formatDuration, nicheLabel, relativeTime } from "@/lib/format";
import { NODE_BY_ID } from "@/lib/graphSpec";
import { leadgen } from "@/services";
import type { NodeId } from "@/types/agent";

export function AgentRunPage() {
  const { id } = useParams<{ id: string }>();
  const { scope, currentTenant } = useTenant();
  const [selected, setSelected] = React.useState<NodeId | null>(null);

  const run = useAsync(
    () => leadgen.getAgentRun(id ?? "", scope),
    [id, scope.tenant_id],
    { skip: !currentTenant || !id },
  );

  if (run.loading) {
    return (
      <>
        <PageHeader title="Agent run" />
        <Card weight="feature" className="p-5">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="mt-6 h-[420px] w-full" />
        </Card>
      </>
    );
  }

  if (run.error) {
    return (
      <>
        <PageHeader title="Agent run" />
        <ErrorState
          title="Could not load this run"
          message={run.error}
          safeguard={run.safeguard ?? undefined}
          onRetry={run.reload}
        />
      </>
    );
  }

  if (!run.data) {
    return (
      <>
        <PageHeader title="Agent run" />
        <EmptyState
          title="That run does not exist"
          body="It may belong to a different tenant, or the run id may be out of date."
          action={
            <Button variant="primary" asChild>
              <Link to="/agent">Back to the agent</Link>
            </Button>
          }
        />
      </>
    );
  }

  const data = run.data;
  const runningNode = data.nodes.find((node) => node.status === "running");
  const failedNodes = data.nodes.filter((node) => node.status === "failed");
  const elapsed = data.nodes.reduce((sum, node) => sum + (node.duration_ms ?? 0), 0);

  return (
    <>
      <PageHeader
        eyebrow={
          <Link
            to="/agent"
            className="inline-flex items-center gap-1.5 text-meta text-secondary hover:text-primary"
          >
            <ArrowLeft size={14} />
            Agent
          </Link>
        }
        title={
          <span className="flex flex-wrap items-center gap-3">
            <span>Run</span>
            <Num className="text-[22px] text-secondary">{data.run_id}</Num>
            {data.dry_run ? (
              <Badge tone="warning">
                <Eye size={11} />
                dry run - nothing is sent
              </Badge>
            ) : (
              <Badge tone="success">
                <Radio size={11} />
                live
              </Badge>
            )}
          </span>
        }
        subtitle={data.prompt || undefined}
        actions={
          <Button variant="secondary" asChild>
            <Link to={`/activity?run=${data.run_id}`}>Node log</Link>
          </Button>
        }
      />

      {/* Run stats */}
      <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <RunStat label="Status" value={data.status} tone={data.status === "running" ? "accent" : undefined} />
        <RunStat label="Targets" value={String(data.targets.length)} mono />
        <RunStat label="Discovered" value={String(data.leads_discovered)} mono />
        <RunStat label="Qualified" value={String(data.leads_qualified)} mono />
        <RunStat label="Node time" value={formatDuration(elapsed)} mono />
        <RunStat label="Model" value={data.llm_provider} />
      </div>

      {runningNode ? (
        <Card weight="standard" className="mb-5 border-accent/25 bg-accent-muted/30 px-4 py-3">
          <NodeProgress
            label={`${runningNode.node_id} ${NODE_BY_ID[runningNode.node_id]?.label ?? ""} - ${
              runningNode.note || "working"
            }`}
          />
        </Card>
      ) : null}

      {failedNodes.length ? (
        <Card weight="standard" className="mb-5 border-danger/25 bg-danger-muted/30 p-4">
          <div className="flex items-start gap-2.5">
            <TriangleAlert size={15} className="mt-0.5 shrink-0 text-danger" />
            <div>
              <p className="text-meta text-primary">
                <Num>{failedNodes.length}</Num> node
                {failedNodes.length === 1 ? "" : "s"} reported a failure
              </p>
              <p className="mt-1 text-micro text-secondary">
                The affected leads were flagged for manual review rather than being
                sent. The rest of the batch continued.
              </p>
            </div>
          </div>
        </Card>
      ) : null}

      {/* The graph */}
      <Card weight="feature" className="overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3.5">
          <div>
            <h2 className="text-body font-medium text-primary">Pipeline</h2>
            <p className="mt-0.5 text-micro text-tertiary">
              Click a node to inspect it. Drag to pan, scroll to zoom.
            </p>
          </div>
          {data.low_yield_targets.length ? (
            <Badge tone="warning">
              <Num>{data.low_yield_targets.length}</Num> low-yield targets
            </Badge>
          ) : null}
        </div>

        {/* The graph takes the full width; the inspector floats over it on the
            right. Reserving a 320px column permanently would cost the graph a
            quarter of its space for a panel that is empty most of the time. */}
        <div className="relative h-[460px] sm:h-[560px] lg:h-[600px]">
          <PipelineGraph
            nodeStates={data.nodes}
            selectedNode={selected}
            onSelectNode={setSelected}
          />

          {selected ? (
            <div className="absolute inset-y-0 right-0 z-10 w-full max-w-[340px] animate-fade-in shadow-raised sm:w-[340px]">
              <NodeInspector
                nodeId={selected}
                state={data.nodes.find((node) => node.node_id === selected)}
                onClose={() => setSelected(null)}
              />
            </div>
          ) : null}
        </div>

        <div className="border-t border-border">
          <RunSidebar
            targets={data.targets}
            lowYield={data.low_yield_targets}
            startedAt={data.started_at}
            manualReview={data.needs_manual_review.length}
            archived={data.archived.length}
          />
        </div>
      </Card>
    </>
  );
}

function RunStat({
  label,
  value,
  mono = false,
  tone,
}: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: "accent";
}) {
  return (
    <Card weight="flat" className="px-3.5 py-3">
      <div className="text-micro text-tertiary">{label}</div>
      <div
        className={
          mono
            ? "num mt-1 text-body font-medium text-primary"
            : tone === "accent"
              ? "mt-1 text-body font-medium text-accent-text"
              : "mt-1 text-body font-medium text-primary"
        }
      >
        {value}
      </div>
    </Card>
  );
}

/** The default side panel: what this run is actually working on. */
function RunSidebar({
  targets,
  lowYield,
  startedAt,
  manualReview,
  archived,
}: {
  targets: { niche_id: string; region: string; language: string }[];
  lowYield: { niche_id: string; region: string }[];
  startedAt: string;
  manualReview: number;
  archived: number;
}) {
  const lowYieldKeys = new Set(lowYield.map((t) => `${t.niche_id}/${t.region}`));

  return (
    <div className="px-5 py-4">
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
        <h3 className="text-meta font-medium text-primary">
          Targets <Num className="text-secondary">({targets.length})</Num>
        </h3>
        <span className="text-micro text-tertiary">
          started {relativeTime(startedAt)}
        </span>
        {manualReview > 0 ? (
          <span className="text-micro text-warning">
            <Num>{manualReview}</Num> flagged for manual review
          </span>
        ) : null}
        {archived > 0 ? (
          <span className="text-micro text-secondary">
            <Num>{archived}</Num> archived
          </span>
        ) : null}
      </div>

      <ul className="mt-3 flex flex-wrap gap-2">
        {targets.map((target) => {
          const key = `${target.niche_id}/${target.region}`;
          const thin = lowYieldKeys.has(key);
          return (
            <li
              key={key}
              className={
                thin
                  ? "flex items-center gap-2 rounded-control border border-warning/30 bg-warning-muted px-2.5 py-1.5"
                  : "flex items-center gap-2 rounded-control border border-border bg-bg px-2.5 py-1.5"
              }
            >
              <span className="text-micro text-primary">
                {nicheLabel(target.niche_id)}
              </span>
              <Num className="text-micro text-tertiary">
                {target.region}/{target.language}
              </Num>
              {thin ? (
                <Badge tone="warning" className="shrink-0">
                  low yield
                </Badge>
              ) : null}
            </li>
          );
        })}
      </ul>

      <p className="mt-3 text-micro text-tertiary">
        A low-yield target found fewer new leads than its configured minimum. It is
        flagged, not failed.
      </p>
    </div>
  );
}
