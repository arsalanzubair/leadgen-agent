/**
 * DateRangeMenu.tsx -- the window a screen is looking at.
 *
 * A real menu rather than a native `<select>`, for two reasons the previous
 * version got wrong: a native select cannot carry a tick beside the chosen row
 * or tint it, and its arrow is drawn by the operating system, which meant the
 * calendar had to be faked as a CSS background image and drifted out of
 * alignment with every other icon in the product. Both icons here are the same
 * lucide set at the same size as everything else.
 *
 * "Today" and "Yesterday" are calendar days, not rolling 24-hour windows.
 * Somebody asking what came in yesterday means yesterday, not the period
 * between this time yesterday and this time the day before.
 */

import * as Popover from "@radix-ui/react-popover";
import { Calendar, Check, ChevronDown, ChevronUp } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

export type RangeKey = "today" | "yesterday" | "7d" | "30d" | "all";

export const RANGES: { value: RangeKey; label: string }[] = [
  { value: "today", label: "Today" },
  { value: "yesterday", label: "Yesterday" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "all", label: "All time" },
];

export const DEFAULT_RANGE: RangeKey = "7d";

export function rangeLabel(value: RangeKey): string {
  return RANGES.find((range) => range.value === value)?.label ?? "All time";
}

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** Is this timestamp inside the chosen window? */
export function withinRange(
  iso: string | null,
  range: RangeKey,
  now = new Date(),
): boolean {
  if (range === "all") return true;
  if (!iso) return false;

  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return false;

  const today = startOfDay(now);
  const day = 86_400_000;

  switch (range) {
    case "today":
      return at >= today && at < today + day;
    case "yesterday":
      return at >= today - day && at < today;
    case "7d":
      return at >= now.getTime() - 7 * day;
    case "30d":
      return at >= now.getTime() - 30 * day;
  }
}

export function DateRangeMenu({
  value,
  onChange,
  className,
}: {
  value: RangeKey;
  onChange: (value: RangeKey) => void;
  className?: string;
}) {
  const [open, setOpen] = React.useState(false);

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          aria-label="Date range"
          className={cn(
            "flex h-10 items-center gap-2.5 rounded-control border border-border bg-bg px-3.5",
            "text-meta text-primary transition-colors duration-150 hover:border-border-strong",
            className,
          )}
        >
          <Calendar size={16} className="shrink-0 text-secondary" strokeWidth={1.75} />
          <span className="whitespace-nowrap">{rangeLabel(value)}</span>
          {/* The chevron points at where the menu is, not at a fixed
              direction: down when it will open below, up while it is open. */}
          {open ? (
            <ChevronUp size={15} className="shrink-0 text-secondary" />
          ) : (
            <ChevronDown size={15} className="shrink-0 text-secondary" />
          )}
        </button>
      </Popover.Trigger>

      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={6}
          className="z-50 min-w-[190px] rounded-card border border-border bg-bg p-1.5 shadow-raised"
        >
          <div role="radiogroup" aria-label="Date range">
            {RANGES.map((range) => {
              const active = range.value === value;
              return (
                <button
                  key={range.value}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  onClick={() => {
                    onChange(range.value);
                    setOpen(false);
                  }}
                  className={cn(
                    "flex w-full items-center justify-between gap-6 rounded-control px-3 py-2 text-left text-meta",
                    "transition-colors duration-150",
                    active
                      ? "bg-accent-soft font-medium text-accent-text"
                      : "text-primary hover:bg-surface-raised",
                  )}
                >
                  {range.label}
                  {active ? <Check size={15} className="shrink-0" /> : null}
                </button>
              );
            })}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
