/**
 * useAsync.ts -- one fetch pattern, so every screen gets the same
 * loading/empty/error behaviour without repeating it.
 *
 * Deliberately small: no cache, no dedupe, no query library. The dashboard
 * reads a handful of endpoints per screen and a real deployment would swap
 * this for TanStack Query in one place if it needed to.
 */

import * as React from "react";

import { ApiError } from "@/services";

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  /** Message safe to show a user. */
  error: string | null;
  /** What to do about it, when the service said. */
  remedy: string | null;
  reload: () => void;
}

export function useAsync<T>(
  fetcher: () => Promise<T>,
  deps: React.DependencyList,
  options: { skip?: boolean } = {},
): AsyncState<T> {
  const { skip = false } = options;
  const [data, setData] = React.useState<T | null>(null);
  const [loading, setLoading] = React.useState(!skip);
  const [error, setError] = React.useState<string | null>(null);
  const [remedy, setRemedy] = React.useState<string | null>(null);
  const [nonce, setNonce] = React.useState(0);

  // The fetcher is a fresh closure each render; keep it in a ref so it is not
  // itself a dependency, and let the caller's `deps` decide when to refetch.
  const fetcherRef = React.useRef(fetcher);
  fetcherRef.current = fetcher;

  React.useEffect(() => {
    if (skip) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setRemedy(null);

    fetcherRef
      .current()
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError) {
          setError(err.message);
          setRemedy(err.remedy ?? null);
        } else {
          setError(err instanceof Error ? err.message : "Something went wrong");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, skip]);

  const reload = React.useCallback(() => setNonce((n) => n + 1), []);

  return { data, loading, error, remedy, reload };
}

/** A mutation with pending/error state, for approve / mark-sent style actions. */
export function useMutation<TArgs extends unknown[], TResult>(
  action: (...args: TArgs) => Promise<TResult>,
) {
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const actionRef = React.useRef(action);
  actionRef.current = action;

  const run = React.useCallback(async (...args: TArgs): Promise<TResult | null> => {
    setPending(true);
    setError(null);
    try {
      return await actionRef.current(...args);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Action failed");
      return null;
    } finally {
      setPending(false);
    }
  }, []);

  return { run, pending, error };
}

/** Media query hook, for the tables-become-cards breakpoint. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = React.useState(() =>
    typeof window !== "undefined" ? window.matchMedia(query).matches : false,
  );

  React.useEffect(() => {
    const list = window.matchMedia(query);
    const listener = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(list.matches);
    list.addEventListener("change", listener);
    return () => list.removeEventListener("change", listener);
  }, [query]);

  return matches;
}

/** Keyboard shortcuts, used by the approvals queue (A / E / R). */
export function useHotkeys(
  bindings: Record<string, (event: KeyboardEvent) => void>,
  enabled = true,
) {
  const bindingsRef = React.useRef(bindings);
  bindingsRef.current = bindings;

  React.useEffect(() => {
    if (!enabled) return;
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      // Never hijack a key the user is typing into a field.
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const fn = bindingsRef.current[event.key.toLowerCase()];
      if (fn) {
        event.preventDefault();
        fn(event);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled]);
}
