/**
 * controls.tsx -- the interactive primitives, kept apart from primitives.tsx
 * so that file stays about surfaces and this one about things you click.
 *
 * Radix underneath wherever real behaviour matters (a switch has to be a
 * switch to a screen reader, not a styled div that happens to respond to
 * clicks). Motion is 150ms and only ever in response to an interaction.
 */

import * as SwitchPrimitive from "@radix-ui/react-switch";
import { ChevronDown } from "lucide-react";
import * as React from "react";

import { initials } from "@/lib/format";
import type { StatusTone } from "@/lib/statusLabels";
import { cn } from "@/lib/utils";
import { Num } from "./primitives";

// --------------------------------------------------------------------------- //
// Switch
// --------------------------------------------------------------------------- //

export function Switch({
  checked,
  onChange,
  disabled,
  tone = "accent",
  ariaLabel,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  tone?: "accent" | "attention";
  ariaLabel?: string;
}) {
  return (
    <SwitchPrimitive.Root
      checked={checked}
      onCheckedChange={onChange}
      disabled={disabled}
      aria-label={ariaLabel}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full border transition-colors duration-150 ease-out",
        "focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2",
        "disabled:cursor-not-allowed disabled:opacity-50",
        checked
          ? tone === "attention"
            ? "border-warning/40 bg-warning/70"
            : "border-accent/40 bg-accent"
          : "border-border bg-surface-sunken",
      )}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          "block h-3.5 w-3.5 translate-x-[3px] rounded-full bg-white",
          "transition-transform duration-150 ease-out data-[state=checked]:translate-x-[19px]",
        )}
      />
    </SwitchPrimitive.Root>
  );
}

/** A switch with its label and one line of explanation, as a single row. */
export function ToggleRow({
  checked,
  onChange,
  label,
  hint,
  disabled,
  tone = "accent",
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: React.ReactNode;
  hint?: string;
  disabled?: boolean;
  tone?: "accent" | "attention";
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-1">
      <div className="min-w-0">
        <p className="text-meta text-primary">{label}</p>
        {hint ? <p className="mt-0.5 text-micro text-secondary">{hint}</p> : null}
      </div>
      {/*
        The row's own label names the switch. Without this the control is a
        toggle a screen reader announces as nothing at all -- the label beside
        it is a separate element and is not associated automatically.
      */}
      <Switch
        checked={checked}
        onChange={onChange}
        disabled={disabled}
        tone={tone}
        ariaLabel={typeof label === "string" ? label : undefined}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Segmented control -- the tab pattern used across Outreach and Results
// --------------------------------------------------------------------------- //

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  size = "md",
  className,
}: {
  value: T;
  options: { value: T; label: string; count?: number }[];
  onChange: (value: T) => void;
  size?: "sm" | "md";
  className?: string;
}) {
  return (
    <div
      role="tablist"
      className={cn(
        "inline-flex items-center gap-1 rounded-control border border-border bg-surface p-1",
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            role="tab"
            type="button"
            aria-selected={active}
            onClick={() => onChange(option.value)}
            className={cn(
              "rounded-[4px] font-medium transition-colors duration-150",
              size === "sm" ? "px-2 py-1 text-micro" : "px-3 py-1.5 text-meta",
              active ? "bg-surface-raised text-primary" : "text-secondary hover:text-primary",
            )}
          >
            {option.label}
            {typeof option.count === "number" ? (
              <Num className={cn("ml-1.5", active ? "text-accent-text" : "text-tertiary")}>
                {option.count}
              </Num>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Key/value rows, and the technical-details disclosure
// --------------------------------------------------------------------------- //

export function KeyValue({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: React.ReactNode;
  mono?: boolean;
}) {
  return (
    // Label in a fixed column, value left-aligned beside it. Values used to be
    // pushed to the right edge, which put a two-word answer and a four-line
    // answer at different starting points and made a seven-row summary read as
    // a ragged column rather than a list of answers.
    <div className="flex items-baseline gap-4 border-b border-border py-2 last:border-0">
      <span className="w-40 shrink-0 text-meta text-tertiary">{label}</span>
      <span className={cn("min-w-0 flex-1 break-words text-meta text-secondary", mono && "num")}>
        {children}
      </span>
    </div>
  );
}

/**
 * A collapsed panel for the things a curious person might want and nobody else
 * needs: internal ids, timestamps, raw values. Present, never primary.
 */
export function Disclosure({
  label,
  children,
  defaultOpen = false,
}: {
  label: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = React.useState(defaultOpen);
  return (
    <div className="rounded-card border border-border">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className={cn(
          "flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left",
          "transition-colors duration-150 hover:bg-surface-raised",
        )}
      >
        <span className="text-meta text-secondary">{label}</span>
        <ChevronDown
          size={15}
          className={cn(
            "shrink-0 text-tertiary transition-transform duration-150",
            open && "rotate-180",
          )}
        />
      </button>
      {open ? <div className="border-t border-border px-4 py-3">{children}</div> : null}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Status dot -- shape and position carry meaning, not colour alone
// --------------------------------------------------------------------------- //

export function StatusDot({
  tone,
  pulse = false,
  className,
}: {
  tone: StatusTone;
  pulse?: boolean;
  className?: string;
}) {
  const fill =
    tone === "accent"
      ? "bg-accent"
      : tone === "positive"
        ? "bg-success"
        : tone === "attention"
          ? "bg-warning"
          : tone === "negative"
            ? "bg-danger"
            : "bg-tertiary";
  return (
    <span className={cn("relative flex h-2 w-2 shrink-0", className)}>
      {pulse ? (
        <span
          className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-60", fill)}
        />
      ) : null}
      <span className={cn("relative inline-flex h-2 w-2 rounded-full", fill)} />
    </span>
  );
}

/**
 * Initials in a square. Used for a business, and never as a stand-in for a
 * person the product invented.
 */
export function Monogram({
  text,
  size = 32,
  className,
}: {
  text: string;
  size?: number;
  className?: string;
}) {
  return (
    <span
      style={{ width: size, height: size }}
      className={cn(
        "flex shrink-0 items-center justify-center rounded-control border border-border",
        "bg-surface-raised text-micro font-medium text-secondary",
        className,
      )}
    >
      {initials(text)}
    </span>
  );
}

// --------------------------------------------------------------------------- //
// Copy button -- used wherever the user has to move text somewhere by hand
// --------------------------------------------------------------------------- //

export function useCopy(timeout = 1600) {
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef<number | undefined>(undefined);

  React.useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = React.useCallback(
    async (text: string) => {
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        // Clipboard access can be refused. Fall back to a selection the user
        // can copy themselves rather than silently doing nothing.
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        document.body.removeChild(area);
      }
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), timeout);
    },
    [timeout],
  );

  return { copied, copy };
}
