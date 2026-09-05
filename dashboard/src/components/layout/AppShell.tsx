/**
 * AppShell.tsx -- sidebar + scrolling page area.
 *
 * There is no top bar. With four destinations in the rail and a title at the
 * top of every page, a second bar repeating the page name is a band of chrome
 * that earns nothing. Below `lg` the rail becomes an overlay drawer, and only
 * then does a slim bar appear -- to hold the one control that opens it.
 *
 * Home gets a barely-there white-to-slate gradient; every other screen sits
 * flat on slate-50. That difference is what makes the composer screen feel
 * like a starting point rather than another list.
 */

import { Menu } from "lucide-react";
import * as React from "react";
import { Link, Outlet, useLocation } from "react-router-dom";

import { LogoMark } from "@/components/ui/LogoMark";
import { Sidebar } from "./Sidebar";
import { cn } from "@/lib/utils";

const STORAGE_KEY = "leadflow.sidebar.collapsed";

export function AppShell() {
  const { pathname } = useLocation();
  const [collapsed, setCollapsed] = React.useState(() => {
    try {
      return window.localStorage.getItem(STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [drawerOpen, setDrawerOpen] = React.useState(false);

  const toggle = React.useCallback(() => {
    setCollapsed((value) => {
      try {
        window.localStorage.setItem(STORAGE_KEY, value ? "0" : "1");
      } catch {
        // Storage can be blocked; the session still works, it just does not
        // remember the choice.
      }
      return !value;
    });
  }, []);

  React.useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  const isHome = pathname === "/";

  return (
    <div className="flex h-full overflow-hidden bg-bg">
      <div className="hidden lg:block">
        <Sidebar collapsed={collapsed} onToggle={toggle} />
      </div>

      {drawerOpen ? (
        <div className="fixed inset-0 z-50 lg:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            className="absolute inset-0 bg-[var(--scrim)]"
            onClick={() => setDrawerOpen(false)}
          />
          <div className="absolute left-0 top-0 h-full animate-fade-in shadow-raised">
            <Sidebar
              collapsed={false}
              onToggle={() => setDrawerOpen(false)}
              onNavigate={() => setDrawerOpen(false)}
            />
          </div>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-2.5 border-b border-border bg-bg px-4 lg:hidden">
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open navigation"
            className="-ml-1 flex h-9 w-9 items-center justify-center rounded-control text-secondary transition-colors duration-150 hover:bg-surface-raised hover:text-primary"
          >
            <Menu size={19} />
          </button>
          <Link
            to="/"
            aria-label="LeadFlow home"
            className="flex items-center gap-2 transition-opacity duration-150 hover:opacity-80"
          >
            <LogoMark size={22} />
            <span className="text-[17px] font-bold tracking-tight">
              <span className="text-primary">Lead</span>
              <span className="text-accent-text">Flow</span>
            </span>
          </Link>
        </header>

        <main
          className={cn(
            "min-h-0 flex-1 overflow-y-auto",
            isHome
              ? "bg-gradient-to-b from-bg via-bg-home-mid to-bg-page"
              : "bg-bg-page",
          )}
        >
          {isHome ? (
            <Outlet />
          ) : (
            <div className="mx-auto w-full max-w-[1280px] px-4 pb-16 pt-7 lg:px-9 lg:pt-9">
              <Outlet />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

/**
 * Page header.
 *
 * Title, one muted line under it, and the screen's controls on the right. No
 * eyebrow above the heading: the rail already says where you are.
 */
export function PageHeader({
  title,
  subtitle,
  actions,
  className,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "mb-6 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between",
        className,
      )}
    >
      <div className="min-w-0">
        <h1 className="text-title font-bold text-primary">{title}</h1>
        {subtitle ? (
          <p className="mt-1 max-w-2xl text-body text-tertiary">{subtitle}</p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex shrink-0 flex-wrap items-center gap-2.5">{actions}</div>
      ) : null}
    </div>
  );
}
