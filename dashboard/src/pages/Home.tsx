/**
 * Home.tsx -- reachable from "+ New". The screen the product opens on.
 *
 * One composer, centred, on a barely-there gradient. Submitting runs the
 * search: there is no plan-confirmation step between typing and running, so
 * the only thing between the user and a result is the sentence they wrote.
 *
 * The greeting uses the user's first name and nothing else. If no name has
 * been entered it is left out entirely rather than filled with a placeholder
 * -- the product does not invent an identity and then greet somebody with it.
 */

import { ArrowUp, Globe, Loader2, RefreshCw, Search, ChevronRight } from "lucide-react";
import { motion } from "framer-motion";
import * as React from "react";

import { RunProgress } from "@/components/find/RunProgress";
import { Button } from "@/components/ui/primitives";
import { InlineError } from "@/components/ui/states";
import { useWorkspace } from "@/hooks/useWorkspace";
import { api } from "@/services";
import { cn } from "@/lib/utils";
import type { AgentRun } from "@/types/agent";

/**
 * The suggestion pool.
 *
 * Deliberately unrelated trades and continents. Nothing in this product
 * assumes an industry, and a pool that quietly assumed one would tell every
 * other kind of buyer it was not for them.
 */
const SUGGESTIONS = [
  "Find businesses in Texas without a website",
  "Find e-commerce brands in the UK with outdated websites",
  "Find retail businesses in Sydney without a mobile app",
  "Find logistics companies in Toronto relying on manual processes",
  "Find businesses in Dubai ready to automate repetitive workflows",
  "Find businesses in Germany without AI-powered customer support",
] as const;

/** Fisher-Yates. The order changes on load and on every refresh. */
function shuffled<T>(items: readonly T[]): T[] {
  const copy = [...items];
  for (let i = copy.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [copy[i], copy[j]] = [copy[j], copy[i]];
  }
  return copy;
}

function firstNameOf(fullName: string): string {
  return fullName.trim().split(/\s+/)[0] ?? "";
}

export function HomePage() {
  const { scope, profile, testMode, setTestMode } = useWorkspace();

  const [prompt, setPrompt] = React.useState("");
  const [order, setOrder] = React.useState(() => shuffled(SUGGESTIONS));
  const [starting, setStarting] = React.useState(false);
  const [run, setRun] = React.useState<AgentRun | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);

  const firstName = firstNameOf(profile.sender_name);

  const start = async () => {
    const text = prompt.trim();
    if (!text || starting) return;
    setStarting(true);
    setError(null);
    try {
      const plan = await api.parseRequest(text, scope);
      const started = await api.startRun({ ...plan, dry_run: testMode }, text, scope);
      setRun(started);
      setTestMode(started.dry_run);
    } catch (err) {
      setError(err instanceof Error ? err.message : "That search could not be started.");
    } finally {
      setStarting(false);
    }
  };

  if (run) {
    return (
      <div className="mx-auto w-full max-w-[900px] px-4 pb-16 pt-10 lg:px-8">
        <div className="mb-6 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-section font-bold text-primary">Searching</h1>
            <p className="mt-1 truncate text-body text-tertiary">{run.prompt}</p>
          </div>
          <Button
            variant="secondary"
            onClick={() => {
              setRun(null);
              setPrompt("");
            }}
          >
            New search
          </Button>
        </div>
        <RunProgress run={run} />
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-[1180px] px-4 pb-20 pt-10 lg:px-8 lg:pt-16">
      <div className="text-center">
        <h1 className="text-[32px] font-bold leading-tight tracking-tight text-primary sm:text-greeting">
          {firstName ? (
            <>
              Hey, <span className="text-accent">{firstName}</span>
            </>
          ) : (
            "Hey there"
          )}
        </h1>
        <p className="mt-3 text-body text-tertiary sm:text-[17px]">
          Find the right leads. Start a new search.
        </p>
      </div>

      {error ? (
        <div className="mx-auto mt-6 max-w-[1050px]">
          <InlineError message={error} />
        </div>
      ) : null}

      {/* The composer. The one object on this page that is not text. */}
      <div className="mx-auto mt-9 max-w-[1050px]">
        <div className="relative rounded-panel border border-border bg-bg p-5 shadow-soft transition-colors duration-150 focus-within:border-border-strong">
          <label htmlFor="home-composer" className="sr-only">
            Describe the businesses you want to find
          </label>
          <textarea
            id="home-composer"
            ref={textareaRef}
            rows={4}
            value={prompt}
            disabled={starting}
            onChange={(event) => setPrompt(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void start();
              }
            }}
            placeholder="Find businesses across the USA that are likely to need a new website…"
            className="w-full resize-none bg-transparent pr-14 text-[17px] leading-relaxed text-primary placeholder:text-tertiary focus:outline-none"
          />

          <motion.button
            type="button"
            onClick={() => void start()}
            disabled={!prompt.trim() || starting}
            aria-label="Start this search"
            whileTap={prompt.trim() && !starting ? { scale: 0.92 } : undefined}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className={cn(
              "absolute bottom-4 right-4 flex h-10 w-10 items-center justify-center rounded-full",
              "transition-colors duration-150",
              prompt.trim() && !starting
                ? "bg-accent text-accent-contrast hover:bg-accent-hover"
                : "cursor-not-allowed border border-border bg-surface-raised text-tertiary",
            )}
          >
            {starting ? (
              <Loader2 size={17} className="animate-spin" />
            ) : (
              <ArrowUp size={17} />
            )}
          </motion.button>
        </div>

        <p className="mt-5 flex items-center justify-center gap-2 text-meta text-tertiary">
          <Globe size={15} />
          Searching across <span className="font-semibold text-primary">84M+</span>{" "}
          businesses worldwide
        </p>
      </div>

      <div className="mx-auto mt-12 max-w-[1050px]">
        <h2 className="text-center text-body font-bold text-primary">
          Try something like
        </h2>

        <div className="mt-5 grid gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
          {order.map((suggestion) => (
            <motion.button
              key={suggestion}
              type="button"
              onClick={() => {
                setPrompt(suggestion);
                textareaRef.current?.focus();
              }}
              whileHover={{ y: -2 }}
              transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
              className={cn(
                "flex items-center gap-3 rounded-card border border-border bg-bg px-4 py-3.5 text-left",
                "transition-colors duration-150 hover:border-border-strong",
              )}
            >
              <Search size={16} className="shrink-0 text-secondary" />
              <span className="min-w-0 flex-1 text-meta text-primary">{suggestion}</span>
              <ChevronRight size={15} className="shrink-0 text-tertiary" />
            </motion.button>
          ))}
        </div>

        <div className="mt-7 flex justify-center">
          <button
            type="button"
            onClick={() => setOrder(shuffled(SUGGESTIONS))}
            className="inline-flex items-center gap-2 text-meta text-tertiary transition-colors duration-150 hover:text-primary"
          >
            <RefreshCw size={15} />
            Refresh suggestions
          </button>
        </div>
      </div>
    </div>
  );
}
