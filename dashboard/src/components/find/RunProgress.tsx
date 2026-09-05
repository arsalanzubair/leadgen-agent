/**
 * RunProgress.tsx -- what is happening, as a list of four plain steps.
 *
 * A list, not a diagram. Somebody watching a search run wants to know roughly
 * where it is and whether it is stuck; a picture of how the work is organised
 * internally answers a question they never asked.
 *
 * The four steps here are coarser than the work actually being done, and
 * deliberately so -- thirteen ticking rows reads as noise, and the detail is
 * available on Activity for anyone who wants it.
 */

import { Check, Loader2, Minus, TriangleAlert } from "lucide-react";
import { Link } from "react-router-dom";

import { ModeTag } from "@/components/layout/ModeIndicator";
import { Button, Card, Num } from "@/components/ui/primitives";
import { plural } from "@/lib/format";
import { RUN_PROGRESS_STEPS, runStatusLabel } from "@/lib/statusLabels";
import { cn } from "@/lib/utils";
import type { AgentRun, StageProgress } from "@/types/agent";

type StepStatus = "done" | "running" | "waiting" | "problem";

/** Roll the underlying stage states up into one status per visible step. */
function stepStatus(stages: StageProgress[], group: string[]): StepStatus {
  const relevant = stages.filter((stage) => group.includes(stage.stage));
  if (!relevant.length) return "waiting";
  if (relevant.some((stage) => stage.status === "failed")) return "problem";
  if (relevant.some((stage) => stage.status === "running")) return "running";
  if (relevant.every((stage) => stage.status === "completed" || stage.status === "skipped")) {
    return "done";
  }
  if (relevant.some((stage) => stage.status === "completed")) return "running";
  return "waiting";
}

/** The most specific thing we can honestly say about where it is right now. */
function currentNote(stages: StageProgress[]): string {
  const running = stages.find((stage) => stage.status === "running");
  return running?.note ?? "";
}

export function RunProgress({ run }: { run: AgentRun }) {
  const finished = run.status === "completed" || run.status === "failed";
  const note = currentNote(run.stages);

  return (
    <Card weight="feature" className="overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          {finished ? (
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-success-muted">
              <Check size={13} className="text-success" />
            </span>
          ) : (
            <Loader2 size={16} className="animate-spin text-accent-text" />
          )}
          <h2 className="text-section font-medium text-primary">
            {finished ? "Search finished" : "Searching"}
          </h2>
        </div>
        <div className="flex items-center gap-2">
          <ModeTag testMode={run.dry_run} />
          <span className="text-micro text-tertiary">{runStatusLabel(run.status)}</span>
        </div>
      </div>

      <ol className="divide-y divide-border">
        {RUN_PROGRESS_STEPS.map((step) => {
          const status = stepStatus(run.stages, step.stages);
          return (
            <li key={step.id} className="flex items-center gap-3 px-5 py-3">
              <StepMark status={status} />
              <span
                className={cn(
                  "min-w-0 flex-1 text-body",
                  status === "waiting" ? "text-tertiary" : "text-primary",
                )}
              >
                {step.label}
              </span>
              {status === "running" && note ? (
                <span className="hidden max-w-[46%] truncate text-micro text-secondary sm:block">
                  {note}
                </span>
              ) : null}
            </li>
          );
        })}
      </ol>

      {finished ? (
        <div className="border-t border-border px-5 py-4">
          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
            <Stat label="Found" value={run.leads_discovered} />
            <Stat label="Good matches" value={run.leads_qualified} />
            {run.needs_manual_review.length ? (
              <Stat
                label="Need a closer look"
                value={run.needs_manual_review.length}
                tone="attention"
              />
            ) : null}
          </div>

          {run.low_yield_targets.length ? (
            <p className="mt-3 flex items-start gap-2 text-micro text-secondary">
              <TriangleAlert size={13} className="mt-0.5 shrink-0 text-warning" />
              <span>
                {plural(run.low_yield_targets.length, "search")} turned up fewer
                new businesses than expected. Widening the places you search, or
                lowering your match bar, usually helps.
              </span>
            </p>
          ) : null}

          <div className="mt-4 flex flex-wrap gap-2">
            <Button asChild variant="primary">
              <Link to="/leads">See what was found</Link>
            </Button>
            <Button asChild variant="secondary">
              <Link to="/outreach/approvals">Review the messages</Link>
            </Button>
          </div>
        </div>
      ) : (
        <div className="border-t border-border px-5 py-3">
          <p className="text-micro text-tertiary">
            You can leave this page - it keeps going, and anything needing your
            approval will be waiting on Home.
          </p>
        </div>
      )}
    </Card>
  );
}

function StepMark({ status }: { status: StepStatus }) {
  if (status === "done") {
    return (
      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-success/40 bg-success-muted">
        <Check size={11} className="text-success" />
      </span>
    );
  }
  if (status === "running") {
    return (
      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-accent/40 bg-accent-soft">
        <Loader2 size={11} className="animate-spin text-accent-text" />
      </span>
    );
  }
  if (status === "problem") {
    return (
      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-danger/40 bg-danger-muted">
        <TriangleAlert size={11} className="text-danger" />
      </span>
    );
  }
  return (
    <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-border bg-bg">
      <Minus size={11} className="text-tertiary" />
    </span>
  );
}

function Stat({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: number;
  tone?: "neutral" | "attention";
}) {
  return (
    <span>
      <Num
        className={cn(
          "text-title font-semibold",
          tone === "attention" ? "text-warning" : "text-primary",
        )}
      >
        {value}
      </Num>
      <span className="ml-1.5 text-meta text-secondary">{label}</span>
    </span>
  );
}
