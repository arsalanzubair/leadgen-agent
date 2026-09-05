/**
 * LogoMark.tsx -- the mark beside the wordmark.
 *
 * The only place in the product where two teals appear together: the gradient
 * runs from the accent to a lighter tint declared as `--accent-logo-lt`. That
 * tint is reserved for this file and must not be used as a UI colour, or the
 * single-accent system stops being single.
 *
 * The gradient id is derived from a React id so two marks on one page (the
 * rail and the mobile drawer) cannot collide in the SVG namespace.
 */

import * as React from "react";

export function LogoMark({
  size = 26,
  className,
}: {
  size?: number;
  className?: string;
}) {
  const gradientId = `logo-${React.useId().replace(/:/g, "")}`;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
      aria-hidden
      focusable="false"
    >
      <defs>
        <linearGradient id={gradientId} x1="4" y1="28" x2="28" y2="4">
          <stop offset="0%" stopColor="var(--accent)" />
          <stop offset="100%" stopColor="var(--accent-logo-lt)" />
        </linearGradient>
      </defs>
      <path
        d="M3 21.5c3.6-4.8 6.9-4.8 10.5 0s6.9 4.8 10.5 0"
        stroke={`url(#${gradientId})`}
        strokeWidth="3.2"
        strokeLinecap="round"
      />
      <path
        d="M6 12.5c3.4-4.6 6.6-4.6 10 0s6.6 4.6 10 0"
        stroke={`url(#${gradientId})`}
        strokeWidth="3.2"
        strokeLinecap="round"
        opacity="0.55"
      />
    </svg>
  );
}
