/**
 * states.tsx -- empty, loading and error states.
 *
 * Three rules the product holds to:
 *   - an empty state names the next action, never just "No data";
 *   - loading is a skeleton of the thing being loaded, never a bare spinner;
 *   - an error says what happened AND what to do about it, because the whole
 *     failure posture of the thing behind this UI is "stop, don't send".
 */

import { AlertTriangle, RefreshCw, type LucideIcon } from "lucide-react";
import type * as React from "react";

import { Button, Card } from "./primitives";
import { cn } from "@/lib/utils";

// --------------------------------------------------------------------------- //
// Skeletons
// --------------------------------------------------------------------------- //

export function Skeleton({
  className,
  style,
}: {
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <div
      style={style}
      className={cn(
        "relative overflow-hidden rounded-control bg-surface-raised",
        "after:absolute after:inset-0 after:-translate-x-full after:animate-shimmer",
        "after:bg-gradient-to-r after:from-transparent after:via-[var(--shimmer)] after:to-transparent",
        className,
      )}
    />
  );
}

/** A skeleton shaped like the KPI row, so the layout does not jump. */
export function SkeletonStats({ count = 6 }: { count?: number }) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      {Array.from({ length: count }).map((_, index) => (
        <Card key={index} className="p-4">
          <Skeleton className="h-3 w-20" />
          <Skeleton className="mt-3 h-7 w-14" />
          <Skeleton className="mt-3 h-3 w-full" />
        </Card>
      ))}
    </div>
  );
}

/** A skeleton shaped like a table, with the right column count. */
export function SkeletonTable({
  rows = 6,
  columns = 6,
}: {
  rows?: number;
  columns?: number;
}) {
  return (
    <div className="overflow-hidden rounded-card border border-border">
      <div className="flex gap-4 border-b border-border bg-surface px-4 py-2.5">
        {Array.from({ length: columns }).map((_, index) => (
          <Skeleton key={index} className="h-3 flex-1" />
        ))}
      </div>
      {Array.from({ length: rows }).map((_, rowIndex) => (
        <div
          key={rowIndex}
          className="flex gap-4 border-b border-border px-4 py-3 last:border-0"
        >
          {Array.from({ length: columns }).map((_, colIndex) => (
            <Skeleton
              key={colIndex}
              className={cn("h-3.5 flex-1", colIndex === 0 && "flex-[2]")}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

export function SkeletonCards({ count = 4 }: { count?: number }) {
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: count }).map((_, index) => (
        <Card key={index} className="p-5">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="mt-2 h-3 w-1/3" />
          <div className="mt-5 grid grid-cols-4 gap-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-6" />
            ))}
          </div>
          <Skeleton className="mt-4 h-1.5 w-full" />
        </Card>
      ))}
    </div>
  );
}

export function SkeletonChart({ className }: { className?: string }) {
  return (
    <Card className={cn("p-5", className)}>
      <Skeleton className="h-4 w-40" />
      <Skeleton className="mt-1.5 h-3 w-56" />
      <div className="mt-6 flex h-[200px] items-end gap-2">
        {[42, 68, 55, 80, 62, 91, 74, 58, 83, 66, 49, 77].map((height, index) => (
          <Skeleton key={index} className="flex-1" style={{ height: `${height}%` }} />
        ))}
      </div>
    </Card>
  );
}

/**
 * A live indicator with a sentence next to it. The caller passes the plain
 * description of what is happening -- "Finding businesses", not a step name.
 */
export function LiveStatus({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2.5 text-meta text-secondary">
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
      </span>
      {label}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Empty
// --------------------------------------------------------------------------- //

export function EmptyState({
  icon: Icon,
  title,
  body,
  action,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  body: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-card border border-dashed border-border px-6 py-14 text-center",
        className,
      )}
    >
      {Icon ? (
        <span className="mb-4 flex h-10 w-10 items-center justify-center rounded-card border border-border bg-surface">
          <Icon size={18} className="text-secondary" />
        </span>
      ) : null}
      <h3 className="text-body font-medium text-primary">{title}</h3>
      <p className="mt-1.5 max-w-md text-meta text-secondary">{body}</p>
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Error
// --------------------------------------------------------------------------- //

export function ErrorState({
  title = "Something went wrong",
  message,
  remedy,
  onRetry,
  className,
}: {
  title?: string;
  message: string;
  /** What to do about it. Shown prominently, not as fine print. */
  remedy?: string;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <Card
      weight="standard"
      className={cn("border-danger/25 bg-danger-muted/40 p-6", className)}
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-control border border-danger/30 bg-danger-muted">
          <AlertTriangle size={16} className="text-danger" />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-body font-medium text-primary">{title}</h3>
          <p className="mt-1 text-meta text-secondary">{message}</p>
          {remedy ? (
            <p className="mt-3 rounded-control border border-border bg-surface px-3 py-2 text-meta text-primary">
              {remedy}
            </p>
          ) : null}
          {onRetry ? (
            <Button variant="secondary" size="sm" className="mt-4" onClick={onRetry}>
              <RefreshCw size={14} />
              Try again
            </Button>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

/** Inline variant for a widget that failed inside an otherwise fine page. */
export function InlineError({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-control border border-danger/25 bg-danger-muted px-3 py-2">
      <span className="text-meta text-danger">{message}</span>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="text-micro text-danger underline decoration-danger/40 hover:decoration-danger"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
