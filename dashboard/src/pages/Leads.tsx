/**
 * Leads.tsx -- every business found so far, and every search that found them.
 *
 * Two tabs over one dataset. "Filtered leads" lists the searches themselves --
 * each one is a list somebody asked for, and that is how people look for work
 * they did last week. "All leads" is the flat table across every search.
 *
 * Businesses that have opted out, or that local rules forbid contacting, are
 * not in either view. They are not shown as one status among many, because
 * presenting "opted out" alongside "good match" invites somebody to try anyway.
 */

import {
  Briefcase,
  Filter,
  Layers,
  Mail,
  Phone,
  Plus,
  Search,
  Star,
  Users,
} from "lucide-react";
import { motion } from "framer-motion";
import * as React from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { PageHeader } from "@/components/layout/AppShell";
import { LeadTable, type SortKey } from "@/components/leads/LeadTable";
import { Button, Card, Input, Select } from "@/components/ui/primitives";
import { KpiCard, KpiRow, ListHeader, PanelEmpty, TabPills } from "@/components/ui/patterns";
import { ErrorState, SkeletonTable } from "@/components/ui/states";
import { useAsync } from "@/hooks/useAsync";
import { useWorkspace } from "@/hooks/useWorkspace";
import { isActive } from "@/lib/leadFields";
import { formatDate, plural, relativeTime } from "@/lib/format";
import { channelLabel, regionLabel } from "@/lib/statusLabels";
import { api } from "@/services";
import type { AgentRun } from "@/types/agent";
import type { Channel, Lead, Region } from "@/types/lead";

type Tab = "filtered" | "all";
type RangeKey = "7d" | "30d" | "90d" | "all";
type SortOrder = "newest" | "oldest" | "largest";

const RANGES: { value: RangeKey; label: string; days: number | null }[] = [
  { value: "7d", label: "Last 7 days", days: 7 },
  { value: "30d", label: "Last 30 days", days: 30 },
  { value: "90d", label: "Last 90 days", days: 90 },
  { value: "all", label: "All time", days: null },
];

/** Sources that put a business on a map, as opposed to a contact database. */
const MAPPED_SOURCES = new Set(["google_places", "osm"]);

function withinRange(iso: string | null, days: number | null): boolean {
  if (days === null) return true;
  if (!iso) return false;
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return false;
  return Date.now() - at <= days * 86_400_000;
}

export function LeadsPage() {
  const { scope, rules, niches } = useWorkspace();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [showFilters, setShowFilters] = React.useState(false);

  const tab = (params.get("tab") ?? "filtered") as Tab;
  const range = (params.get("range") ?? "7d") as RangeKey;
  const region = (params.get("region") ?? "all") as Region | "all";
  const nicheId = params.get("niche_id") ?? "all";
  const channel = (params.get("channel") ?? "all") as Channel | "all";
  const search = params.get("q") ?? "";
  const order = (params.get("order") ?? "newest") as SortOrder;
  const listId = params.get("list") ?? "";
  const sortKey = (params.get("sort") ?? "fit_score") as SortKey;
  const sortDir = (params.get("dir") ?? "desc") as "asc" | "desc";

  /**
   * `all` is the "no filter" value for region, audience and channel, so
   * writing it to the URL says nothing and it is dropped instead.
   */
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (!value || value === "all") next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  /**
   * The tab is not a filter, and its second value happens to be spelled the
   * same as that sentinel -- so it gets its own setter. Routing it through
   * `setParam` deleted `tab=all` and bounced the user straight back to the
   * first tab.
   */
  const setTab = (value: Tab) => {
    const next = new URLSearchParams(params);
    if (value === "filtered") next.delete("tab");
    else next.set("tab", value);
    setParams(next, { replace: true });
  };

  const clearAll = () => setParams(new URLSearchParams(), { replace: true });

  const leads = useAsync(
    () =>
      api.getLeads({
        ...scope,
        region,
        niche_id: nicheId,
        channel,
        sort_by: sortKey,
        sort_dir: sortDir,
      }),
    [scope.tenant_id, region, nicheId, channel, sortKey, sortDir],
  );
  const runs = useAsync(() => api.getAgentRuns(scope), [scope.tenant_id]);

  const days = RANGES.find((r) => r.value === range)?.days ?? null;

  const active = React.useMemo(
    () => (leads.data ?? []).filter(isActive),
    [leads.data],
  );

  /** Everything the KPI row counts, over the chosen window. */
  const inWindow = React.useMemo(
    () => active.filter((lead) => withinRange(lead.discovered_at, days)),
    [active, days],
  );

  const stats = React.useMemo(() => summarise(inWindow), [inWindow]);

  // The lead table respects the window, the free-text search, and a session
  // picked from the sidebar.
  const visibleLeads = React.useMemo(() => {
    const needle = search.trim().toLowerCase();
    const run = listId ? (runs.data ?? []).find((r) => r.run_id === listId) : null;
    const allowed = run ? new Set(run.lead_ids) : null;
    return inWindow.filter((lead) => {
      if (allowed && !allowed.has(lead.lead_id)) return false;
      if (!needle) return true;
      return (
        lead.company_name.toLowerCase().includes(needle) ||
        lead.contact_name.toLowerCase().includes(needle) ||
        lead.contact_email.toLowerCase().includes(needle) ||
        lead.location.toLowerCase().includes(needle)
      );
    });
  }, [inWindow, search, listId, runs.data]);

  const visibleRuns = React.useMemo(() => {
    const needle = search.trim().toLowerCase();
    const list = (runs.data ?? [])
      .filter((run) => withinRange(run.started_at, days))
      .filter((run) => !needle || run.prompt.toLowerCase().includes(needle));

    const sorted = [...list];
    sorted.sort((a, b) => {
      if (order === "largest") return b.leads_discovered - a.leads_discovered;
      const at = new Date(a.started_at).getTime();
      const bt = new Date(b.started_at).getTime();
      return order === "oldest" ? at - bt : bt - at;
    });
    return sorted;
  }, [runs.data, days, search, order]);

  const onSort = (key: SortKey) => {
    const next = new URLSearchParams(params);
    if (key === sortKey) {
      next.set("dir", sortDir === "asc" ? "desc" : "asc");
    } else {
      next.set("sort", key);
      next.set("dir", key === "company_name" ? "asc" : "desc");
    }
    setParams(next, { replace: true });
  };

  const filtersOn = region !== "all" || nicheId !== "all" || channel !== "all";

  return (
    <div>
      <PageHeader
        title="Leads"
        subtitle="View and manage all the leads you've discovered."
        actions={
          <>
            <Select
              aria-label="Date range"
              value={range}
              onChange={(event) => setParam("range", event.target.value)}
              className="h-10 pl-9 bg-[url('data:image/svg+xml;utf8,<svg%20xmlns=%22http://www.w3.org/2000/svg%22%20width=%2216%22%20height=%2216%22%20fill=%22none%22%20stroke=%22%2364748B%22%20stroke-width=%221.6%22><rect%20x=%222.5%22%20y=%223.5%22%20width=%2211%22%20height=%2210%22%20rx=%222%22/><path%20d=%22M2.5%206.5h11M5.5%202v3M10.5%202v3%22/></svg>')] bg-[left_10px_center,right_8px_center] bg-no-repeat"
            >
              {RANGES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>

            <Button
              variant={showFilters ? "outline" : "secondary"}
              className="h-10"
              onClick={() => setShowFilters((value) => !value)}
            >
              <Filter size={16} />
              Filter
            </Button>

            <Button variant="primary" className="h-10" onClick={() => navigate("/")}>
              <Plus size={16} />
              New Lead
            </Button>
          </>
        }
      />

      <KpiRow>
        <KpiCard icon={Users} label="Total Leads" value={String(stats.total)} />
        <KpiCard icon={Mail} label="Total Emails" value={String(stats.emails)} />
        <KpiCard icon={Phone} label="Total Phones" value={String(stats.phones)} />
        <KpiCard icon={Briefcase} label="Mapped Places" value={String(stats.mapped)} />
        <KpiCard icon={Star} label="Avg. Rating" value={stats.rating} />
      </KpiRow>

      <TabPills
        className="mb-6"
        value={tab}
        onChange={setTab}
        options={[
          { value: "filtered" as Tab, label: "Filtered leads" },
          { value: "all" as Tab, label: "All leads" },
        ]}
      />

      {showFilters ? (
        <Card weight="standard" className="mb-5 animate-fade-in p-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Select
              aria-label="Where"
              value={region}
              onChange={(event) => setParam("region", event.target.value)}
            >
              <option value="all">Anywhere</option>
              {rules.regions.map((value) => (
                <option key={value} value={value}>
                  {regionLabel(value)}
                </option>
              ))}
            </Select>

            <Select
              aria-label="Audience"
              value={nicheId}
              onChange={(event) => setParam("niche_id", event.target.value)}
            >
              <option value="all">Every audience</option>
              {niches.map((niche) => (
                <option key={niche.id} value={niche.id}>
                  {niche.label}
                </option>
              ))}
            </Select>

            <Select
              aria-label="How they are reached"
              value={channel}
              onChange={(event) => setParam("channel", event.target.value)}
            >
              <option value="all">Any channel</option>
              {(["email", "linkedin", "both"] as Channel[]).map((value) => (
                <option key={value} value={value}>
                  {channelLabel(value)}
                </option>
              ))}
            </Select>

            {filtersOn ? (
              <Button variant="ghost" onClick={clearAll}>
                Clear filters
              </Button>
            ) : null}
          </div>
        </Card>
      ) : null}

      {listId ? (
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-card border border-border bg-bg px-4 py-3">
          <span className="text-meta text-secondary">
            Showing one saved search only.
          </span>
          <button
            type="button"
            onClick={() => setParam("list", "")}
            className="text-meta text-accent hover:underline"
          >
            Show everything
          </button>
        </div>
      ) : null}

      {tab === "filtered" ? (
        <FilteredLeads
          runs={visibleRuns}
          loading={runs.loading}
          error={runs.error}
          onRetry={runs.reload}
          search={search}
          onSearch={(value) => setParam("q", value)}
          order={order}
          onOrder={(value) => setParam("order", value)}
        />
      ) : (
        <AllLeads
          leads={visibleLeads}
          hidden={(leads.data?.length ?? 0) - active.length}
          threshold={rules.fit_score_threshold}
          loading={leads.loading}
          error={leads.error}
          remedy={leads.remedy}
          onRetry={leads.reload}
          search={search}
          onSearch={(value) => setParam("q", value)}
          sortKey={sortKey}
          sortDir={sortDir}
          onSort={onSort}
        />
      )}
    </div>
  );
}

/**
 * The KPI row's numbers.
 *
 * Phone and rating have no field on the lead record, so they are computed from
 * nothing and stay at their empty value. That is the honest result, and it is
 * why rating shows `--` rather than 0.0.
 */
function summarise(leads: Lead[]) {
  const emails = leads.filter((lead) => !!lead.contact_email.trim()).length;
  const mapped = leads.filter((lead) => MAPPED_SOURCES.has(lead.source)).length;
  return {
    total: leads.length,
    emails,
    phones: 0,
    mapped,
    rating: "--",
  };
}

// --------------------------------------------------------------------------- //
// Tab one: the searches
// --------------------------------------------------------------------------- //

function FilteredLeads({
  runs,
  loading,
  error,
  onRetry,
  search,
  onSearch,
  order,
  onOrder,
}: {
  runs: AgentRun[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  search: string;
  onSearch: (value: string) => void;
  order: SortOrder;
  onOrder: (value: SortOrder) => void;
}) {
  return (
    <div>
      <ListHeader
        title="Filtered Leads"
        subtitle="Lead lists tailored to the criteria you requested."
      >
        <div className="relative">
          <Search
            size={15}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-tertiary"
          />
          <Input
            className="h-10 w-full pl-9 sm:w-[280px]"
            placeholder="Search lead lists…"
            aria-label="Search lead lists"
            value={search}
            onChange={(event) => onSearch(event.target.value)}
          />
        </div>
        <Select
          className="h-10"
          aria-label="Sort lead lists"
          value={order}
          onChange={(event) => onOrder(event.target.value as SortOrder)}
        >
          <option value="newest">Newest First</option>
          <option value="oldest">Oldest First</option>
          <option value="largest">Most leads</option>
        </Select>
      </ListHeader>

      {error ? (
        <ErrorState message={error} onRetry={onRetry} />
      ) : loading ? (
        <SkeletonTable rows={4} columns={4} />
      ) : runs.length === 0 ? (
        <PanelEmpty
          icon={Layers}
          title="No Lead Searches Found"
          body="There are no filtered lead lists yet."
          action={
            <Button asChild variant="primary" size="lg">
              <Link to="/">Start New Lead Extraction</Link>
            </Button>
          }
        />
      ) : (
        <ul className="space-y-3">
          {runs.map((run) => (
            <li key={run.run_id}>
              <motion.div
                whileHover={{ y: -2 }}
                transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
              >
                <Link
                  to={`/leads?tab=all&list=${encodeURIComponent(run.run_id)}`}
                  className="block rounded-card border border-border bg-bg px-5 py-4 transition-colors duration-150 hover:border-border-strong"
                >
                  <div className="flex flex-wrap items-start justify-between gap-4">
                    <div className="min-w-0">
                      <h3 className="truncate text-body font-semibold text-primary">
                        {run.prompt.trim() || "Untitled search"}
                      </h3>
                      <p className="mt-1 text-meta text-tertiary">
                        {formatDate(run.started_at)} · {relativeTime(run.started_at)}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-6">
                      <span className="text-right">
                        <span className="block text-body font-semibold text-primary">
                          {run.leads_discovered}
                        </span>
                        <span className="block text-micro text-tertiary">found</span>
                      </span>
                      <span className="text-right">
                        <span className="block text-body font-semibold text-primary">
                          {run.leads_qualified}
                        </span>
                        <span className="block text-micro text-tertiary">qualified</span>
                      </span>
                    </div>
                  </div>
                </Link>
              </motion.div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Tab two: the flat table
// --------------------------------------------------------------------------- //

function AllLeads({
  leads,
  hidden,
  threshold,
  loading,
  error,
  remedy,
  onRetry,
  search,
  onSearch,
  sortKey,
  sortDir,
  onSort,
}: {
  leads: Lead[];
  hidden: number;
  threshold: number;
  loading: boolean;
  error: string | null;
  remedy: string | null;
  onRetry: () => void;
  search: string;
  onSearch: (value: string) => void;
  sortKey: SortKey;
  sortDir: "asc" | "desc";
  onSort: (key: SortKey) => void;
}) {
  return (
    <div>
      <ListHeader
        title="All Leads"
        subtitle="Every business found so far, across all your searches."
      >
        <div className="relative">
          <Search
            size={15}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-tertiary"
          />
          <Input
            className="h-10 w-full pl-9 sm:w-[280px]"
            placeholder="Search by name, place or contact"
            aria-label="Search leads"
            value={search}
            onChange={(event) => onSearch(event.target.value)}
          />
        </div>
      </ListHeader>

      {error ? (
        <ErrorState message={error} remedy={remedy ?? undefined} onRetry={onRetry} />
      ) : loading ? (
        <SkeletonTable rows={8} columns={6} />
      ) : leads.length === 0 ? (
        <PanelEmpty
          icon={Users}
          title="No leads here yet"
          body="Describe the businesses you want to reach and the search will go and find your first prospects."
          action={
            <Button asChild variant="primary" size="lg">
              <Link to="/">Start New Lead Extraction</Link>
            </Button>
          }
        />
      ) : (
        <div className="space-y-4">
          <LeadTable
            leads={leads}
            threshold={threshold}
            sortKey={sortKey}
            sortDir={sortDir}
            onSort={onSort}
          />
          {hidden > 0 ? (
            <p className="text-micro text-tertiary">
              {plural(hidden, "business", "businesses")} not shown because they
              opted out or local rules do not allow contacting them. They are
              never contacted again.
            </p>
          ) : null}
        </div>
      )}
    </div>
  );
}
