/**
 * Tailwind theme. Every colour is driven by a CSS variable declared in
 * src/index.css, so a token can be changed in exactly one place.
 *
 * The radius scale is deliberately small and non-uniform (6/10/16) rather than
 * one value reused everywhere -- importance is expressed through shape here,
 * not only through size. 16px is the composer; 10px is a card; 6px is a
 * control.
 */
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        "bg-page": "var(--bg-page)",
        "bg-home-mid": "var(--bg-home-mid)",
        surface: "var(--surface)",
        "surface-raised": "var(--surface-raised)",
        "surface-sunken": "var(--surface-sunken)",
        border: "var(--border)",
        "border-strong": "var(--border-strong)",
        primary: "var(--text-primary)",
        secondary: "var(--text-secondary)",
        tertiary: "var(--text-muted)",
        accent: {
          DEFAULT: "var(--accent)",
          hover: "var(--accent-hover)",
          active: "var(--accent-active)",
          // The fill and the text are separate tokens: in dark the fill must
          // stay dark enough for white to sit on it, while teal text must get
          // lighter to clear the near-black page. See index.css.
          text: "var(--accent-text)",
          contrast: "var(--accent-contrast)",
          light: "var(--accent-light-bg)",
          soft: "var(--accent-soft-bg)",
          lt: "var(--accent-logo-lt)",
        },
        success: { DEFAULT: "var(--success)", muted: "var(--success-muted)" },
        warning: { DEFAULT: "var(--warning)", muted: "var(--warning-muted)" },
        danger: { DEFAULT: "var(--danger)", muted: "var(--danger-muted)" },
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "-apple-system", "sans-serif"],
      },
      fontSize: {
        micro: ["11px", { lineHeight: "16px", letterSpacing: "0.01em" }],
        meta: ["13px", { lineHeight: "18px" }],
        body: ["15px", { lineHeight: "22px" }],
        section: ["20px", { lineHeight: "28px", letterSpacing: "-0.01em" }],
        title: ["30px", { lineHeight: "38px", letterSpacing: "-0.02em" }],
        // The KPI number, and the Home greeting, which is bigger again.
        kpi: ["30px", { lineHeight: "38px", letterSpacing: "-0.02em" }],
        greeting: ["44px", { lineHeight: "54px", letterSpacing: "-0.03em" }],
      },
      borderRadius: {
        control: "6px",
        card: "10px",
        panel: "16px",
      },
      boxShadow: {
        // Soft and low-contrast. The composer and popovers only; a flat card
        // gets a border instead.
        soft: "0 1px 2px rgba(15,23,42,.04), 0 8px 24px -16px rgba(15,23,42,.12)",
        raised: "0 1px 2px rgba(15,23,42,.06), 0 12px 32px -12px rgba(15,23,42,.16)",
      },
      spacing: {
        gutter: "24px",
        section: "32px",
      },
      transitionTimingFunction: {
        out: "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "fade-in": "fade-in 200ms cubic-bezier(0.16, 1, 0.3, 1) both",
        shimmer: "shimmer 1.6s infinite",
      },
    },
  },
  plugins: [],
};
