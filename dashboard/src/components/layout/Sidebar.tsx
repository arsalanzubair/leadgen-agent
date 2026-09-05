/**
 * Sidebar.tsx -- the navigation rail.
 *
 * White, one hairline on the right, no shadow. An active item is a soft grey
 * fill and nothing else: the teal is spent on the one primary action per
 * screen, and a nav rail with four coloured items has no primary action.
 *
 * The wordmark is the buyer's own business name once they have entered one,
 * and falls back to the product name until then. That is the white-label
 * behaviour -- nothing here is anybody else's brand.
 */

import { ChevronDown, PanelLeft, Search, X } from "lucide-react";
import * as React from "react";
import { NavLink, useLocation } from "react-router-dom";

import { AccountMenu } from "./AccountMenu";
import { NAV, SETTINGS_ITEM, isActive } from "./nav";
import { LogoMark } from "@/components/ui/LogoMark";
import { useAsync } from "@/hooks/useAsync";
import { useWorkspace } from "@/hooks/useWorkspace";
import { relativeTime, truncate } from "@/lib/format";
import { api } from "@/services";
import { cn } from "@/lib/utils";

/** The product's own name, used until the buyer sets theirs. */
export const PRODUCT_NAME = "LeadFlow";

export function Sidebar({
  collapsed,
  onToggle,
  onNavigate,
}: {
  collapsed: boolean;
  onToggle: () => void;
  onNavigate?: () => void;
}) {
  const { pathname } = useLocation();
  const { profile, scope } = useWorkspace();
  const [searching, setSearching] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [sessionsOpen, setSessionsOpen] = React.useState(true);

  const custom = profile.business_name.trim();

  return (
    <nav
      aria-label="Main"
      className={cn(
        "flex h-full flex-col border-r border-border bg-bg transition-[width] duration-200 ease-out",
        collapsed ? "w-[72px]" : "w-[260px]",
      )}
    >
      {/* Wordmark, with the two rail-level controls inline beside it. */}
      <div
        className={cn(
          "flex h-16 shrink-0 items-center",
          collapsed ? "justify-center px-2" : "gap-2 pl-5 pr-3",
        )}
      >
        <LogoMark size={26} />
        {!collapsed ? (
          <>
            <span className="min-w-0 flex-1 truncate text-[19px] font-bold tracking-tight">
              {custom ? (
                <span className="text-primary">{truncate(custom, 16)}</span>
              ) : (
                <>
                  <span className="text-primary">Lead</span>
                  <span className="text-accent">Flow</span>
                </>
              )}
            </span>
            <button
              type="button"
              onClick={() => {
                setSearching((value) => !value);
                setSessionsOpen(true);
              }}
              aria-label="Search your past searches"
              aria-expanded={searching}
              className="flex h-7 w-7 items-center justify-center rounded-control text-secondary transition-colors duration-150 hover:bg-surface-raised hover:text-primary"
            >
              <Search size={17} />
            </button>
            <button
              type="button"
              onClick={onToggle}
              aria-label="Collapse sidebar"
              className="flex h-7 w-7 items-center justify-center rounded-control text-secondary transition-colors duration-150 hover:bg-surface-raised hover:text-primary"
            >
              <PanelLeft size={17} />
            </button>
          </>
        ) : null}
      </div>

      {collapsed ? (
        <button
          type="button"
          onClick={onToggle}
          aria-label="Expand sidebar"
          className="mx-auto mb-2 flex h-8 w-8 items-center justify-center rounded-control text-secondary transition-colors duration-150 hover:bg-surface-raised hover:text-primary"
        >
          <PanelLeft size={17} />
        </button>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        <ul className="space-y-0.5">
          {NAV.map((item) => {
            const active = isActive(item, pathname);
            const Icon = item.icon;
            const isNew = item.to === "/";

            return (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  onClick={onNavigate}
                  title={collapsed ? item.label : undefined}
                  className={cn(
                    "group flex items-center rounded-card transition-colors duration-150",
                    collapsed ? "h-11 justify-center" : "h-11 gap-3 px-3",
                    active
                      ? "bg-surface-sunken text-primary"
                      : "text-secondary hover:bg-surface-raised hover:text-primary",
                  )}
                >
                  {/* "+ New" carries a bordered circle so it reads as an
                      action, not a fifth place to go. */}
                  <span
                    className={cn(
                      "flex shrink-0 items-center justify-center",
                      isNew
                        ? "h-8 w-8 rounded-full border border-border bg-bg"
                        : "h-8 w-8",
                    )}
                  >
                    <Icon
                      size={isNew ? 16 : 19}
                      className={active ? "text-primary" : "text-secondary"}
                    />
                  </span>
                  {!collapsed ? (
                    <span className="min-w-0 flex-1 truncate text-[15px] font-medium">
                      {item.label}
                    </span>
                  ) : null}
                </NavLink>
              </li>
            );
          })}
        </ul>

        {!collapsed ? (
          <Sessions
            open={sessionsOpen}
            onToggle={() => setSessionsOpen((value) => !value)}
            searching={searching}
            query={query}
            onQuery={setQuery}
            onCloseSearch={() => {
              setSearching(false);
              setQuery("");
            }}
            tenantId={scope.tenant_id}
            onNavigate={onNavigate}
          />
        ) : null}
      </div>

      <div className="shrink-0 px-3 pb-3">
        <NavLink
          to={SETTINGS_ITEM.to}
          onClick={onNavigate}
          title={collapsed ? SETTINGS_ITEM.label : undefined}
          className={cn(
            "group mb-2 flex items-center rounded-card transition-colors duration-150",
            collapsed ? "h-11 justify-center" : "h-11 gap-3 px-3",
            isActive(SETTINGS_ITEM, pathname)
              ? "bg-surface-sunken text-primary"
              : "text-secondary hover:bg-surface-raised hover:text-primary",
          )}
        >
          <span className="flex h-8 w-8 shrink-0 items-center justify-center">
            <SETTINGS_ITEM.icon size={19} />
          </span>
          {!collapsed ? (
            <span className="truncate text-[15px] font-medium">
              {SETTINGS_ITEM.label}
            </span>
          ) : null}
        </NavLink>

        <AccountMenu collapsed={collapsed} />
      </div>
    </nav>
  );
}

/**
 * Past searches.
 *
 * Live data, never a placeholder list: an empty account says so in one muted
 * line rather than showing invented history.
 */
function Sessions({
  open,
  onToggle,
  searching,
  query,
  onQuery,
  onCloseSearch,
  tenantId,
  onNavigate,
}: {
  open: boolean;
  onToggle: () => void;
  searching: boolean;
  query: string;
  onQuery: (value: string) => void;
  onCloseSearch: () => void;
  tenantId: string;
  onNavigate?: () => void;
}) {
  const runs = useAsync(() => api.getAgentRuns({ tenant_id: tenantId }), [tenantId], {
    skip: !tenantId,
  });

  const items = React.useMemo(() => {
    const all = runs.data ?? [];
    const needle = query.trim().toLowerCase();
    if (!needle) return all;
    return all.filter((run) => run.prompt.toLowerCase().includes(needle));
  }, [runs.data, query]);

  return (
    <div className="mt-6">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 rounded-control px-3 py-1.5 text-left text-meta font-medium text-secondary transition-colors duration-150 hover:text-primary"
      >
        Sessions
        <ChevronDown
          size={16}
          className={cn("shrink-0 transition-transform duration-150", !open && "-rotate-90")}
        />
      </button>

      {open ? (
        <div className="mt-1">
          {searching ? (
            <div className="relative mb-2 px-1">
              <Search
                size={13}
                className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-tertiary"
              />
              <input
                autoFocus
                value={query}
                onChange={(event) => onQuery(event.target.value)}
                placeholder="Search sessions"
                aria-label="Search sessions"
                className="h-8 w-full rounded-control border border-border bg-bg pl-8 pr-7 text-meta text-primary placeholder:text-tertiary focus:border-accent focus:outline-none"
              />
              <button
                type="button"
                onClick={onCloseSearch}
                aria-label="Close search"
                className="absolute right-2.5 top-1/2 flex h-5 w-5 -translate-y-1/2 items-center justify-center rounded text-tertiary hover:text-primary"
              >
                <X size={13} />
              </button>
            </div>
          ) : null}

          {items.length === 0 ? (
            <p className="px-3 py-1 text-meta italic text-tertiary">
              {query.trim() ? "Nothing matches that." : "No saved chats yet."}
            </p>
          ) : (
            <ul className="space-y-0.5">
              {items.slice(0, 12).map((run) => (
                <li key={run.run_id}>
                  <NavLink
                    to={`/leads?list=${encodeURIComponent(run.run_id)}`}
                    onClick={onNavigate}
                    className="block rounded-control px-3 py-1.5 text-meta text-secondary transition-colors duration-150 hover:bg-surface-raised hover:text-primary"
                  >
                    <span className="block truncate">
                      {run.prompt.trim() || "Untitled search"}
                    </span>
                    <span className="block truncate text-micro text-tertiary">
                      {relativeTime(run.started_at)}
                    </span>
                  </NavLink>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
