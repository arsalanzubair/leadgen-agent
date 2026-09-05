/**
 * patterns.tsx -- the three compositions Leads, Email and LinkedIn all share.
 *
 * A KPI row, a row of tab pills, and the bordered empty-state card. They live
 * here rather than being written three times so the three screens cannot drift
 * into looking like three products.
 *
 * `PanelEmpty` is deliberately distinct from `EmptyState` in states.tsx: that
 * one is the small dashed placeholder used inside a widget, this one is the
 * full-width bordered card that IS the page when the page has nothing in it.
 */

import { motion } from "framer-motion";
import type { LucideIcon } from "lucide-react";
import type * as React from "react";

import { cn } from "@/lib/utils";

// --------------------------------------------------------------------------- //
// KPI
// --------------------------------------------------------------------------- //

/**
 * One number.
 *
 * `value` takes a string so the caller decides between `0` and `--`. A metric
 * with no meaningful zero -- an average rating across no leads -- shows `--`,
 * because printing 0.0 there states something false.
 */
export function KpiCard({
  icon: Icon,
  label,
  value,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center gap-3.5 rounded-card border border-border bg-bg px-4 py-4">
      <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-accent-light">
        <Icon size={19} className="text-accent-text" strokeWidth={1.75} />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-meta text-secondary">{label}</span>
        <span className="mt-0.5 block text-kpi font-bold leading-none text-primary">
          {value}
        </span>
      </span>
    </div>
  );
}

export function KpiRow({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-6 grid gap-3.5 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5">
      {children}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Tab pills
// --------------------------------------------------------------------------- //

/**
 * Filled pills, not underline tabs.
 *
 * The active one is the only solid accent object in the region, which is what
 * lets it be read without a label saying "selected".
 */
export function TabPills<T extends string>({
  value,
  options,
  onChange,
  className,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  className?: string;
}) {
  return (
    <div role="tablist" className={cn("flex flex-wrap items-center gap-2.5", className)}>
      {options.map((option) => {
        const active = option.value === value;
        return (
          <motion.button
            key={option.value}
            role="tab"
            type="button"
            aria-selected={active}
            onClick={() => onChange(option.value)}
            whileTap={{ scale: 0.97 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className={cn(
              "rounded-card px-4 py-2 text-meta font-medium transition-colors duration-150",
              active
                ? "bg-accent text-accent-contrast"
                : "border border-border bg-bg text-primary hover:border-border-strong",
            )}
          >
            {option.label}
          </motion.button>
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Empty state
// --------------------------------------------------------------------------- //

/** The bordered card that stands in for a list with nothing in it. */
export function PanelEmpty({
  icon: Icon,
  title,
  body,
  action,
  className,
}: {
  icon: LucideIcon;
  title: string;
  body: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-card border border-border bg-bg px-6 py-20 text-center",
        className,
      )}
    >
      <span className="mb-6 flex h-14 w-14 items-center justify-center rounded-full bg-accent-soft">
        <Icon size={24} className="text-accent-text" strokeWidth={1.75} />
      </span>
      <h3 className="text-section font-bold text-primary">{title}</h3>
      <p className="mt-2 max-w-md text-body text-tertiary">{body}</p>
      {action ? <div className="mt-7">{action}</div> : null}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Section heading with the search/sort row beside it
// --------------------------------------------------------------------------- //

export function ListHeader({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
      <div className="min-w-0">
        <h2 className="text-section font-bold text-primary">{title}</h2>
        <p className="mt-1 text-meta text-tertiary">{subtitle}</p>
      </div>
      {children ? (
        <div className="flex flex-wrap items-center gap-2.5">{children}</div>
      ) : null}
    </div>
  );
}
