/**
 * useTheme.tsx -- light, dark, or whatever the machine is set to.
 *
 * Three states, not two. "System" is the default because a person who has
 * already told their operating system they want dark should not have to tell
 * every application separately -- and because it is the only setting that
 * stays right when they change their mind at sunset.
 *
 * The choice is written to the document element as `data-theme`, and for
 * "system" the attribute is REMOVED rather than set to a third value. That is
 * what lets `index.css` express the whole dark theme as a media query plus one
 * explicit override, with no third branch to keep in sync.
 *
 * The same key is read by the inline script in `index.html` before React
 * mounts. Without it the page paints light for one frame and then flips, which
 * is worse on a dark theme than never having offered one.
 */

import * as React from "react";

export type Theme = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

/** Shared with the bootstrap script in index.html. Change both or neither. */
export const THEME_KEY = "leadflow.theme";

const THEMES: Theme[] = ["light", "dark", "system"];

function readStored(): Theme {
  try {
    const value = window.localStorage.getItem(THEME_KEY);
    return THEMES.includes(value as Theme) ? (value as Theme) : "system";
  } catch {
    // Storage can be blocked. The session still works, it just starts from the
    // machine's own preference every time.
    return "system";
  }
}

function systemPrefersDark(): boolean {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

function apply(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

interface ThemeValue {
  /** What the user chose, including "system". */
  theme: Theme;
  /** What that actually renders as right now. For labels and icons. */
  resolved: ResolvedTheme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = React.createContext<ThemeValue | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = React.useState<Theme>(readStored);
  const [systemDark, setSystemDark] = React.useState(systemPrefersDark);

  // Follow the machine while the choice is "system". The listener is always
  // attached rather than conditionally: it is cheap, and detaching it on a
  // switch to light means a later switch back to system shows a stale value.
  React.useEffect(() => {
    const query = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!query) return;
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  React.useEffect(() => {
    apply(theme);
  }, [theme]);

  const setTheme = React.useCallback((next: Theme) => {
    setThemeState(next);
    try {
      window.localStorage.setItem(THEME_KEY, next);
    } catch {
      // Same as above: the choice holds for this session and no longer.
    }
  }, []);

  const resolved: ResolvedTheme =
    theme === "system" ? (systemDark ? "dark" : "light") : theme;

  const value = React.useMemo<ThemeValue>(
    () => ({ theme, resolved, setTheme }),
    [theme, resolved, setTheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeValue {
  const context = React.useContext(ThemeContext);
  if (!context) {
    throw new Error("useTheme must be used inside a ThemeProvider");
  }
  return context;
}
