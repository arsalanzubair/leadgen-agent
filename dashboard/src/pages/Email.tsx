/**
 * Email.tsx -- the email channel, end to end, on one screen.
 *
 * There is no separate approvals queue and no follow-ups page any more. A
 * message that needs reading, a follow-up that is due, and a reply that came
 * back are three states of the same thing, and splitting them across three
 * routes meant the same lead appeared in three places with no way to tell it
 * was one business.
 *
 * Every action happens on the row. Approving does not send from this screen --
 * it clears the message to go, and in Test Mode it records that it would have.
 */

import {
  Check,
  ChevronDown,
  Clock,
  Inbox,
  Loader2,
  Mail,
  MessageSquare,
  Pencil,
  Plug,
  Search,
  Send,
  X,
} from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "@/components/layout/AppShell";
import { ModeChoice, ModeTag } from "@/components/layout/ModeIndicator";
import { Button, Card, Input, Label, Textarea } from "@/components/ui/primitives";
import { Disclosure, Monogram } from "@/components/ui/controls";
import { KpiCard, KpiRow, ListHeader, PanelEmpty, TabPills } from "@/components/ui/patterns";
import { ErrorState, InlineError, SkeletonTable } from "@/components/ui/states";
import { useAsync } from "@/hooks/useAsync";
import { useWorkspace } from "@/hooks/useWorkspace";
import { dueBucket, formatDate, relativeTime } from "@/lib/format";
import { replyLabel } from "@/lib/statusLabels";
import { api } from "@/services";
import { cn } from "@/lib/utils";
import { APPROVED_STATUSES, type Lead } from "@/types/lead";

type Tab = "attention" | "all";
type RowState = "review" | "scheduled" | "sent" | "replied";

/** Which of the four states this lead's email is in right now. */
function stateOf(lead: Lead): RowState {
  if (lead.reply_category && lead.reply_category !== "no_reply") return "replied";
  if (lead.send_status === "sent") return "sent";
  if (lead.approval_status === "pending") return "review";
  return "scheduled";
}

function isEmailLead(lead: Lead): boolean {
  return lead.channel === "email" || lead.channel === "both";
}

export function EmailPage() {
  const { scope, connections, testMode, setTestMode } = useWorkspace();
  const leads = useAsync(() => api.getLeads({ ...scope }), [scope.tenant_id]);

  const [tab, setTab] = React.useState<Tab>("attention");
  const [search, setSearch] = React.useState("");
  const [openId, setOpenId] = React.useState<string | null>(null);
  const [busyId, setBusyId] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const mailbox = connections.find(
    (c) => c.category === "email_sender" && c.state === "connected",
  );

  const rows = React.useMemo(
    () => (leads.data ?? []).filter(isEmailLead).filter((lead) => !lead.archived),
    [leads.data],
  );

  const counts = React.useMemo(() => {
    let review = 0;
    let due = 0;
    let sent = 0;
    let replied = 0;
    for (const lead of rows) {
      const state = stateOf(lead);
      if (state === "review") review += 1;
      if (state === "sent") sent += 1;
      if (state === "replied") replied += 1;
      if (
        state === "scheduled" &&
        (dueBucket(lead.next_touch_due) === "today" ||
          dueBucket(lead.next_touch_due) === "overdue")
      ) {
        due += 1;
      }
    }
    return { review, due, sent, replied };
  }, [rows]);

  const visible = React.useMemo(() => {
    const needle = search.trim().toLowerCase();
    return rows
      .filter((lead) => {
        if (tab === "all") return true;
        const state = stateOf(lead);
        if (state === "review") return true;
        return (
          state === "scheduled" &&
          (dueBucket(lead.next_touch_due) === "today" ||
            dueBucket(lead.next_touch_due) === "overdue")
        );
      })
      .filter(
        (lead) =>
          !needle ||
          lead.company_name.toLowerCase().includes(needle) ||
          lead.contact_email.toLowerCase().includes(needle),
      );
  }, [rows, tab, search]);

  const decide = async (
    lead: Lead,
    action: "approve" | "reject",
    edited?: { subject: string; body: string },
  ) => {
    setBusyId(lead.lead_id);
    setError(null);
    try {
      await api.submitApproval(
        lead.lead_id,
        edited
          ? { action: "edit", email: { subject: edited.subject, body: edited.body } }
          : { action },
        scope,
      );
      setOpenId(null);
      leads.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "That could not be saved.");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div>
      <PageHeader
        title="Email"
        subtitle="Every message written, reviewed and sent from your own mailbox."
      />

      {!mailbox ? (
        <Card weight="standard" className="mb-6 border-warning/40 bg-warning-muted p-4">
          <p className="flex items-start gap-2.5 text-meta text-primary">
            <Plug size={16} className="mt-0.5 shrink-0 text-warning" />
            <span>
              No mailbox is connected, so nothing can actually be emailed yet.
              Messages are still written and held here for your review.
            </span>
          </p>
          <Button asChild variant="secondary" size="sm" className="mt-3">
            <Link to="/settings">Connect a mailbox</Link>
          </Button>
        </Card>
      ) : null}

      <KpiRow>
        <KpiCard icon={Inbox} label="Waiting for Review" value={String(counts.review)} />
        <KpiCard icon={Clock} label="Follow-ups Due" value={String(counts.due)} />
        <KpiCard icon={Send} label="Sent" value={String(counts.sent)} />
        <KpiCard icon={MessageSquare} label="Replied" value={String(counts.replied)} />
      </KpiRow>

      <TabPills
        className="mb-6"
        value={tab}
        onChange={setTab}
        options={[
          { value: "attention" as Tab, label: "Needs attention" },
          { value: "all" as Tab, label: "All email" },
        ]}
      />

      {error ? (
        <div className="mb-4">
          <InlineError message={error} />
        </div>
      ) : null}

      <ListHeader
        title={tab === "attention" ? "Needs attention" : "All email"}
        subtitle={
          tab === "attention"
            ? "Messages waiting on you, and follow-ups due today."
            : "Every email written for a business you have found."
        }
      >
        <div className="relative">
          <Search
            size={15}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-tertiary"
          />
          <Input
            className="h-10 w-full pl-9 sm:w-[280px]"
            placeholder="Search by business or address"
            aria-label="Search email"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
      </ListHeader>

      {leads.error ? (
        <ErrorState
          message={leads.error}
          remedy={leads.remedy ?? undefined}
          onRetry={leads.reload}
        />
      ) : leads.loading ? (
        <SkeletonTable rows={5} columns={4} />
      ) : visible.length === 0 ? (
        <PanelEmpty
          icon={Mail}
          title={tab === "attention" ? "Nothing needs you right now" : "No email activity yet"}
          body={
            tab === "attention"
              ? "Messages appear here as soon as one is written or a follow-up falls due."
              : "Start a search and the messages written for each business will show up here."
          }
          action={
            <Button asChild variant="primary" size="lg">
              <Link to="/">Start a new search</Link>
            </Button>
          }
        />
      ) : (
        <ul className="space-y-3">
          {visible.map((lead) => (
            <li key={lead.lead_id}>
              <EmailRow
                lead={lead}
                open={openId === lead.lead_id}
                busy={busyId === lead.lead_id}
                onToggle={() =>
                  setOpenId((current) =>
                    current === lead.lead_id ? null : lead.lead_id,
                  )
                }
                onApprove={() => void decide(lead, "approve")}
                onReject={() => void decide(lead, "reject")}
                onSaveEdit={(edited) => void decide(lead, "approve", edited)}
              />
            </li>
          ))}
        </ul>
      )}

      {/*
        The one place Test Mode can be changed. It governs sending, so it lives
        on the sending channel rather than on the screen where searches start.
      */}
      <div className="mt-8">
        <Disclosure label="Sending mode">
          <ModeChoice testMode={testMode} onChange={setTestMode} />
        </Disclosure>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// One row
// --------------------------------------------------------------------------- //

function EmailRow({
  lead,
  open,
  busy,
  onToggle,
  onApprove,
  onReject,
  onSaveEdit,
}: {
  lead: Lead;
  open: boolean;
  busy: boolean;
  onToggle: () => void;
  onApprove: () => void;
  onReject: () => void;
  onSaveEdit: (edited: { subject: string; body: string }) => void;
}) {
  const state = stateOf(lead);
  const draft = lead.draft_message?.email ?? {};
  const [editing, setEditing] = React.useState(false);
  const [subject, setSubject] = React.useState(draft.subject ?? "");
  const [body, setBody] = React.useState(draft.body ?? "");

  React.useEffect(() => {
    if (!open) setEditing(false);
  }, [open]);

  const startEdit = () => {
    setSubject(draft.subject ?? "");
    setBody(draft.body ?? "");
    setEditing(true);
  };

  return (
    <Card weight="standard" className="overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-4 py-3.5 text-left transition-colors duration-150 hover:bg-surface-raised"
      >
        <Monogram text={lead.company_name} size={34} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-meta font-semibold text-primary">
            {lead.company_name}
          </span>
          <span className="block truncate text-micro text-tertiary">
            {lead.contact_email || "No address found yet"}
            {draft.subject ? ` · ${draft.subject}` : ""}
          </span>
        </span>
        <StateTag lead={lead} state={state} />
        <ModeTag testMode={lead.dry_run} />
        <ChevronDown
          size={16}
          className={cn(
            "shrink-0 text-tertiary transition-transform duration-150",
            !open && "-rotate-90",
          )}
        />
      </button>

      {open ? (
        <div className="border-t border-border px-4 py-4">
          {editing ? (
            <div className="space-y-3">
              <div>
                <Label htmlFor={`subject-${lead.lead_id}`}>Subject</Label>
                <Input
                  id={`subject-${lead.lead_id}`}
                  className="mt-1.5"
                  value={subject}
                  onChange={(event) => setSubject(event.target.value)}
                />
              </div>
              <div>
                <Label htmlFor={`body-${lead.lead_id}`}>Message</Label>
                <Textarea
                  id={`body-${lead.lead_id}`}
                  rows={9}
                  className="mt-1.5"
                  value={body}
                  onChange={(event) => setBody(event.target.value)}
                />
                <p className="mt-1 text-micro text-tertiary">
                  Your signature and the required footer are added automatically
                  — do not paste them in here.
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="primary"
                  onClick={() => onSaveEdit({ subject, body })}
                  disabled={busy}
                >
                  {busy ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />}
                  Save and approve
                </Button>
                <Button variant="ghost" onClick={() => setEditing(false)} disabled={busy}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <>
              {draft.subject || draft.body ? (
                <div>
                  {draft.subject ? (
                    <p className="text-meta font-semibold text-primary">
                      {draft.subject}
                    </p>
                  ) : null}
                  <p className="mt-2 whitespace-pre-wrap text-meta leading-relaxed text-secondary">
                    {draft.body || "Nothing written yet."}
                  </p>
                </div>
              ) : (
                <p className="text-meta text-tertiary">
                  No message has been written for this business yet.
                </p>
              )}

              {lead.reply_text ? (
                <div className="mt-4 rounded-card border border-border bg-surface-raised p-3">
                  <p className="text-micro font-semibold text-secondary">
                    They replied · {replyLabel(lead.reply_category)}
                  </p>
                  <p className="mt-1 whitespace-pre-wrap text-meta text-primary">
                    {lead.reply_text}
                  </p>
                </div>
              ) : null}

              {/* Actions, on the row, only where there is a decision to make. */}
              {state === "review" ? (
                <div className="mt-4 flex flex-wrap gap-2">
                  <Button variant="primary" onClick={onApprove} disabled={busy}>
                    {busy ? (
                      <Loader2 size={15} className="animate-spin" />
                    ) : (
                      <Check size={15} />
                    )}
                    Approve
                  </Button>
                  <Button variant="secondary" onClick={startEdit} disabled={busy}>
                    <Pencil size={15} />
                    Edit
                  </Button>
                  <Button variant="ghost" onClick={onReject} disabled={busy}>
                    <X size={15} />
                    Reject
                  </Button>
                </div>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </Card>
  );
}

/** The state of a row, said in words rather than by colour alone. */
function StateTag({ lead, state }: { lead: Lead; state: RowState }) {
  const label =
    state === "review"
      ? "Waiting for review"
      : state === "replied"
        ? "Replied"
        : state === "sent"
          ? `Sent${lead.sent_at ? ` ${relativeTime(lead.sent_at)}` : ""}`
          : lead.next_touch_due
            ? `Follow-up ${formatDate(lead.next_touch_due)}`
            : APPROVED_STATUSES.includes(lead.approval_status)
              ? "Approved"
              : "Not started";

  const tone =
    state === "review"
      ? "border-accent/30 bg-accent-soft text-accent"
      : state === "replied"
        ? "border-success/30 bg-success-muted text-success"
        : "border-border bg-surface-raised text-secondary";

  return (
    <span
      className={cn(
        "hidden shrink-0 whitespace-nowrap rounded-control border px-2.5 py-1 text-micro font-medium sm:inline-flex",
        tone,
      )}
    >
      {label}
    </span>
  );
}
