/**
 * LeadTable.tsx -- the leads table, and its card fallback below tablet width.
 *
 * Six columns, named the way somebody would name them out loud: Company,
 * Contact, Match, Channel, Status, Next step. No raw value from the data model
 * reaches this table -- everything goes through `lib/statusLabels.ts` first.
 *
 * There is no detail page to link to. A row expands in place instead, which is
 * why the company cell is a button rather than a link: everything there is to
 * know about a lead is one click away and does not cost the user their scroll
 * position or their filters.
 */

import { ArrowDown, ArrowUp, ChevronDown, ExternalLink } from "lucide-react";
import * as React from "react";

import {
  ChannelBadge,
  CompanyCell,
  ContactCell,
  MatchBadge,
  MatchScore,
  RegionPill,
  StatusBadge,
} from "@/components/leads/LeadBits";
import { Card, Num } from "@/components/ui/primitives";
import { leadStory, leadTimeline, nextStepFor } from "@/lib/outcome";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Lead } from "@/types/lead";

export type SortKey = "company_name" | "fit_score" | "last_updated";

/**
 * Widths are tuned so the whole table fits a 1,240px content column without
 * horizontal scrolling, which is the common desktop case. Narrower than that
 * it scrolls inside its own card; below `md` it becomes cards entirely.
 */
const COLUMNS: { key: SortKey | null; label: string; className?: string }[] = [
  { key: "company_name", label: "Company", className: "w-[24%] min-w-[190px]" },
  { key: null, label: "Contact", className: "w-[18%] min-w-[150px]" },
  { key: "fit_score", label: "Match", className: "w-[13%] min-w-[110px]" },
  { key: null, label: "Channel", className: "w-[9%] min-w-[76px]" },
  { key: null, label: "Status", className: "w-[18%] min-w-[140px]" },
  { key: null, label: "Next step", className: "w-[18%] min-w-[140px]" },
];

export function LeadTable({
  leads,
  threshold,
  sortKey,
  sortDir,
  onSort,
}: {
  leads: Lead[];
  threshold: number;
  sortKey: SortKey;
  sortDir: "asc" | "desc";
  onSort: (key: SortKey) => void;
}) {
  const [openId, setOpenId] = React.useState<string | null>(null);

  const toggle = (leadId: string) =>
    setOpenId((current) => (current === leadId ? null : leadId));

  return (
    <>
      {/* Table, from md up. */}
      <Card weight="standard" className="hidden overflow-hidden md:block">
        <div className="overflow-x-auto">
          <table className="w-full table-fixed border-collapse">
            <thead>
              <tr className="border-b border-border">
                {COLUMNS.map((column) => (
                  <th
                    key={column.label}
                    scope="col"
                    className={cn(
                      "bg-surface-raised px-3 py-3 text-left text-micro font-semibold uppercase tracking-wide text-secondary first:pl-4 last:pr-4",
                      column.className,
                    )}
                  >
                    {column.key ? (
                      <button
                        type="button"
                        onClick={() => onSort(column.key!)}
                        className="inline-flex items-center gap-1 transition-colors duration-150 hover:text-primary"
                      >
                        {column.label}
                        {sortKey === column.key ? (
                          sortDir === "asc" ? (
                            <ArrowUp size={11} />
                          ) : (
                            <ArrowDown size={11} />
                          )
                        ) : null}
                      </button>
                    ) : (
                      column.label
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {leads.map((lead) => {
                const open = openId === lead.lead_id;
                return (
                  <React.Fragment key={lead.lead_id}>
                    <tr
                      className={cn(
                        "border-b border-border transition-colors duration-150 hover:bg-surface-raised",
                        open && "bg-surface-raised",
                        lead.archived && "opacity-60",
                      )}
                    >
                      <td className="px-3 py-3 pl-4">
                        <button
                          type="button"
                          onClick={() => toggle(lead.lead_id)}
                          aria-expanded={open}
                          className="flex w-full min-w-0 items-center gap-2 text-left"
                        >
                          <ChevronDown
                            size={15}
                            className={cn(
                              "shrink-0 text-tertiary transition-transform duration-150",
                              !open && "-rotate-90",
                            )}
                          />
                          <CompanyCell lead={lead} />
                        </button>
                      </td>
                      <td className="px-3 py-3">
                        <ContactCell
                          name={lead.contact_name}
                          email={lead.contact_email}
                          linkedinUrl={lead.linkedin_url}
                        />
                      </td>
                      <td className="px-3 py-3">
                        {lead.fit_score > 0 ? (
                          <MatchScore score={lead.fit_score} threshold={threshold} />
                        ) : (
                          <span className="text-meta text-tertiary">Not scored yet</span>
                        )}
                      </td>
                      <td className="px-3 py-3">
                        <ChannelBadge channel={lead.channel} />
                      </td>
                      <td className="px-3 py-3">
                        <StatusBadge lead={lead} />
                      </td>
                      <td className="px-3 py-3">
                        {/* The recommended action, not the backend's own
                            `next_action` string -- that one is an operator's
                            debug line and has no business on this screen. */}
                        <span
                          className="block truncate text-meta text-secondary"
                          title={nextStepFor(lead, threshold).headline}
                        >
                          {nextStepFor(lead, threshold).headline}
                        </span>
                      </td>
                    </tr>

                    {open ? (
                      <tr className="border-b border-border bg-surface-raised">
                        <td colSpan={COLUMNS.length} className="px-4 pb-5 pt-1">
                          <LeadDetails lead={lead} threshold={threshold} />
                        </td>
                      </tr>
                    ) : null}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Cards, below md. Same data, reordered so what matters for a decision
          comes first on a narrow screen. */}
      <ul className="space-y-2.5 md:hidden">
        {leads.map((lead) => {
          const open = openId === lead.lead_id;
          return (
            <li key={lead.lead_id}>
              <Card
                weight="standard"
                className={cn("p-4", lead.archived && "opacity-70")}
              >
                <button
                  type="button"
                  onClick={() => toggle(lead.lead_id)}
                  aria-expanded={open}
                  className="flex w-full items-start justify-between gap-3 text-left"
                >
                  <div className="min-w-0">
                    <h3 className="truncate text-meta font-semibold text-primary">
                      {lead.company_name}
                    </h3>
                    <p className="mt-0.5 truncate text-micro text-tertiary">
                      {lead.contact_name || "Nobody found yet"}
                      {lead.contact_email ? ` · ${lead.contact_email}` : ""}
                    </p>
                  </div>
                  <ChevronDown
                    size={16}
                    className={cn(
                      "mt-0.5 shrink-0 text-tertiary transition-transform duration-150",
                      !open && "-rotate-90",
                    )}
                  />
                </button>

                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <MatchBadge lead={lead} threshold={threshold} />
                  <RegionPill region={lead.region} />
                  <ChannelBadge channel={lead.channel} showLabel />
                </div>

                {lead.fit_score > 0 ? (
                  <div className="mt-3 flex items-center gap-3">
                    <MatchScore score={lead.fit_score} threshold={threshold} width={64} />
                    <span className="text-micro text-tertiary">
                      your bar is <Num>{threshold}</Num>
                    </span>
                  </div>
                ) : null}

                <div className="mt-3 border-t border-border pt-3">
                  <StatusBadge lead={lead} />
                  <p className="mt-2 text-micro text-secondary">
                    {nextStepFor(lead, threshold).headline}
                  </p>
                </div>

                {open ? (
                  <div className="mt-4 border-t border-border pt-4">
                    <LeadDetails lead={lead} threshold={threshold} />
                  </div>
                ) : null}
              </Card>
            </li>
          );
        })}
      </ul>
    </>
  );
}

/**
 * What is known about one lead, inline.
 *
 * This is everything the old detail page carried that a person actually read:
 * why it scored what it scored, what was found about the business, the message
 * written for it, and what has happened so far.
 */
function LeadDetails({ lead, threshold }: { lead: Lead; threshold: number }) {
  const story = leadStory(lead, threshold);
  const timeline = leadTimeline(lead, threshold);
  const email = lead.draft_message?.email;
  const linkedin = lead.draft_message?.linkedin;

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <div className="space-y-4">
        <div>
          <p className="text-micro font-semibold text-secondary">Why this one</p>
          <p className="mt-1 text-meta text-primary">
            {lead.fit_reason || story.interesting}
          </p>
        </div>

        {lead.signals.length ? (
          <div>
            <p className="text-micro font-semibold text-secondary">What was found</p>
            <ul className="mt-1.5 space-y-1">
              {lead.signals.map((signal) => (
                <li key={signal} className="text-meta text-secondary">
                  · {signal}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-4 text-meta text-tertiary">
          {lead.website ? (
            <a
              href={lead.website}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex items-center gap-1.5 text-accent-text hover:underline"
            >
              Visit website
              <ExternalLink size={12} />
            </a>
          ) : null}
          {lead.location ? <span>{lead.location}</span> : null}
          {lead.discovered_at ? (
            <span>Found {formatDate(lead.discovered_at)}</span>
          ) : null}
        </div>
      </div>

      <div className="space-y-4">
        {email?.subject || email?.body ? (
          <div>
            <p className="text-micro font-semibold text-secondary">Email written</p>
            {email.subject ? (
              <p className="mt-1 text-meta font-medium text-primary">{email.subject}</p>
            ) : null}
            <p className="mt-1 whitespace-pre-wrap text-meta leading-relaxed text-secondary">
              {email.body}
            </p>
          </div>
        ) : null}

        {linkedin?.connection_note ? (
          <div>
            <p className="text-micro font-semibold text-secondary">
              LinkedIn note written
            </p>
            <p className="mt-1 whitespace-pre-wrap text-meta leading-relaxed text-secondary">
              {linkedin.connection_note}
            </p>
          </div>
        ) : null}

        {timeline.length ? (
          <div>
            <p className="text-micro font-semibold text-secondary">What has happened</p>
            <ol className="mt-1.5 space-y-1">
              {timeline.map((entry) => (
                <li key={entry.stage} className="text-meta text-secondary">
                  {entry.summary}
                  {entry.at ? (
                    <span className="text-tertiary"> · {formatDate(entry.at)}</span>
                  ) : null}
                </li>
              ))}
            </ol>
          </div>
        ) : null}
      </div>
    </div>
  );
}
