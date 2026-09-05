/**
 * LinkedIn.tsx -- the messages you send by hand.
 *
 * This product never touches a LinkedIn account. It writes the message; you
 * send it from your own profile and then tell the product you did. That is a
 * deliberate design decision, not a missing feature: automated LinkedIn
 * activity is what gets accounts restricted, and it would be your account.
 *
 * So every verb on this screen describes something a PERSON did -- "Copy
 * Message", "Open LinkedIn", "Mark as Sent", "Mark as Connected". There is no
 * Send button anywhere on this page and there never will be.
 *
 * The screen merges two things the user experiences as one queue: messages
 * still waiting on their approval, and approved ones waiting on them to send.
 */

import {
  ArrowUpRight,
  Check,
  ChevronDown,
  Clock,
  Copy,
  Handshake,
  Info,
  Linkedin,
  Loader2,
  Pencil,
  Search,
  Send,
  X,
} from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "@/components/layout/AppShell";
import { ModeTag } from "@/components/layout/ModeIndicator";
import { Button, Card, Input, Label, Textarea } from "@/components/ui/primitives";
import { Monogram, useCopy } from "@/components/ui/controls";
import { KpiCard, KpiRow, ListHeader, PanelEmpty, TabPills } from "@/components/ui/patterns";
import { ErrorState, InlineError, SkeletonTable } from "@/components/ui/states";
import { useAsync } from "@/hooks/useAsync";
import { useWorkspace } from "@/hooks/useWorkspace";
import { dueBucket, formatDate, relativeTime } from "@/lib/format";
import { api } from "@/services";
import { cn } from "@/lib/utils";
import { MAX_LINKEDIN_CONNECTION_NOTE_CHARS, type Lead } from "@/types/lead";
import type { LinkedInQueueItem } from "@/types/agent";

type Tab = "attention" | "all";

/**
 * One row, from either source.
 *
 * `review` rows come from a lead still awaiting approval; everything else
 * comes from the queue, which only ever holds approved messages.
 */
type Row =
  | { kind: "review"; id: string; lead: Lead }
  | { kind: "queue"; id: string; item: LinkedInQueueItem };

function isLinkedInLead(lead: Lead): boolean {
  return lead.channel === "linkedin" || lead.channel === "both";
}

export function LinkedInPage() {
  const { scope, profile } = useWorkspace();
  const leads = useAsync(() => api.getLeads({ ...scope }), [scope.tenant_id]);
  const queue = useAsync(() => api.getLinkedInQueue(scope), [scope.tenant_id]);

  const [tab, setTab] = React.useState<Tab>("attention");
  const [search, setSearch] = React.useState("");
  const [openId, setOpenId] = React.useState<string | null>(null);
  const [busyId, setBusyId] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const awaiting = React.useMemo(
    () =>
      (leads.data ?? [])
        .filter(isLinkedInLead)
        .filter((lead) => !lead.archived && lead.approval_status === "pending"),
    [leads.data],
  );

  const items = React.useMemo(
    () => (queue.data ?? []).filter((item) => item.status !== "skipped"),
    [queue.data],
  );

  const counts = React.useMemo(() => {
    const due = (leads.data ?? [])
      .filter(isLinkedInLead)
      .filter(
        (lead) =>
          !lead.archived &&
          (dueBucket(lead.next_touch_due) === "today" ||
            dueBucket(lead.next_touch_due) === "overdue"),
      ).length;

    return {
      review: awaiting.length,
      ready: items.filter((item) => item.status === "pending").length,
      connecting: items.filter((item) => item.status === "sent").length,
      due,
    };
  }, [awaiting, items, leads.data]);

  const rows = React.useMemo<Row[]>(() => {
    const all: Row[] = [
      ...awaiting.map((lead) => ({ kind: "review" as const, id: lead.lead_id, lead })),
      ...items.map((item) => ({ kind: "queue" as const, id: item.lead_id, item })),
    ];

    const needle = search.trim().toLowerCase();
    return all
      .filter((row) => {
        if (tab === "all") return true;
        // Needs attention: anything the user has to read, send, or record.
        if (row.kind === "review") return true;
        return row.item.status === "pending" || row.item.status === "sent";
      })
      .filter((row) => {
        if (!needle) return true;
        const name =
          row.kind === "review" ? row.lead.company_name : row.item.company_name;
        return name.toLowerCase().includes(needle);
      });
  }, [awaiting, items, tab, search]);

  const decide = async (
    lead: Lead,
    action: "approve" | "reject",
    edited?: { note: string; dm: string },
  ) => {
    setBusyId(lead.lead_id);
    setError(null);
    try {
      await api.submitApproval(
        lead.lead_id,
        edited
          ? {
              action: "edit",
              linkedin: { connection_note: edited.note, followup_dm: edited.dm },
            }
          : { action },
        scope,
      );
      setOpenId(null);
      leads.reload();
      queue.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "That could not be saved.");
    } finally {
      setBusyId(null);
    }
  };

  const mark = async (item: LinkedInQueueItem, status: "sent" | "connected") => {
    setBusyId(item.lead_id);
    setError(null);
    try {
      await api.markLinkedInItem(item.lead_id, status, scope);
      queue.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "That could not be saved.");
    } finally {
      setBusyId(null);
    }
  };

  const loading = leads.loading || queue.loading;
  const failure = leads.error ?? queue.error;

  return (
    <div>
      <PageHeader
        title="LinkedIn"
        subtitle="Written for you to send from your own account, by hand."
      />

      {/* Said plainly, at the top, before anybody has to infer it. */}
      <Card weight="standard" className="mb-6 flex items-start gap-2.5 p-4">
        <Info size={16} className="mt-0.5 shrink-0 text-secondary" />
        <div className="min-w-0 text-meta text-secondary">
          <p>
            <span className="font-semibold text-primary">
              You send these, not the software.
            </span>{" "}
            Copy the message, open their profile, and send it from your own
            LinkedIn. Then mark it here so follow-ups stay on track.
          </p>
          <p className="mt-1 text-micro text-tertiary">
            Nothing here logs into LinkedIn or clicks anything on your behalf.
            {profile.linkedin_account.trim()
              ? ` Sending as ${profile.linkedin_account}.`
              : ""}
          </p>
        </div>
      </Card>

      <KpiRow>
        <KpiCard icon={Pencil} label="Waiting for Review" value={String(counts.review)} />
        <KpiCard icon={Send} label="Ready to Send" value={String(counts.ready)} />
        <KpiCard
          icon={Handshake}
          label="Waiting to Connect"
          value={String(counts.connecting)}
        />
        <KpiCard icon={Clock} label="Follow-ups Due" value={String(counts.due)} />
      </KpiRow>

      <TabPills
        className="mb-6"
        value={tab}
        onChange={setTab}
        options={[
          { value: "attention" as Tab, label: "Needs attention" },
          { value: "all" as Tab, label: "All LinkedIn" },
        ]}
      />

      {error ? (
        <div className="mb-4">
          <InlineError message={error} />
        </div>
      ) : null}

      <ListHeader
        title={tab === "attention" ? "Needs attention" : "All LinkedIn"}
        subtitle={
          tab === "attention"
            ? "Messages waiting on you to read, send or record."
            : "Every LinkedIn message written for a business you have found."
        }
      >
        <div className="relative">
          <Search
            size={15}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-tertiary"
          />
          <Input
            className="h-10 w-full pl-9 sm:w-[280px]"
            placeholder="Search by business"
            aria-label="Search LinkedIn messages"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
      </ListHeader>

      {failure ? (
        <ErrorState
          message={failure}
          onRetry={() => {
            leads.reload();
            queue.reload();
          }}
        />
      ) : loading ? (
        <SkeletonTable rows={5} columns={4} />
      ) : rows.length === 0 ? (
        <PanelEmpty
          icon={Linkedin}
          title={
            tab === "attention"
              ? "Nothing needs you right now"
              : "No LinkedIn activity yet"
          }
          body={
            tab === "attention"
              ? "Messages appear here as soon as one is written for you to review or send."
              : "Start a search and the LinkedIn messages written for each business will show up here."
          }
          action={
            <Button asChild variant="primary" size="lg">
              <Link to="/">Start a new search</Link>
            </Button>
          }
        />
      ) : (
        <ul className="space-y-3">
          {rows.map((row) => (
            <li key={`${row.kind}-${row.id}`}>
              {row.kind === "review" ? (
                <ReviewRow
                  lead={row.lead}
                  open={openId === `review-${row.id}`}
                  busy={busyId === row.id}
                  onToggle={() =>
                    setOpenId((current) =>
                      current === `review-${row.id}` ? null : `review-${row.id}`,
                    )
                  }
                  onApprove={() => void decide(row.lead, "approve")}
                  onReject={() => void decide(row.lead, "reject")}
                  onSaveEdit={(edited) => void decide(row.lead, "approve", edited)}
                />
              ) : (
                <QueueRow
                  item={row.item}
                  open={openId === `queue-${row.id}`}
                  busy={busyId === row.id}
                  onToggle={() =>
                    setOpenId((current) =>
                      current === `queue-${row.id}` ? null : `queue-${row.id}`,
                    )
                  }
                  onMark={(status) => void mark(row.item, status)}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Shared row chrome
// --------------------------------------------------------------------------- //

function RowHeader({
  name,
  detail,
  tag,
  testMode,
  open,
  onToggle,
}: {
  name: string;
  detail: string;
  tag: { label: string; tone: string };
  testMode: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className="flex w-full items-center gap-3 px-4 py-3.5 text-left transition-colors duration-150 hover:bg-surface-raised"
    >
      <Monogram text={name} size={34} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-meta font-semibold text-primary">{name}</span>
        <span className="block truncate text-micro text-tertiary">{detail}</span>
      </span>
      <span
        className={cn(
          "hidden shrink-0 whitespace-nowrap rounded-control border px-2.5 py-1 text-micro font-medium sm:inline-flex",
          tag.tone,
        )}
      >
        {tag.label}
      </span>
      <ModeTag testMode={testMode} />
      <ChevronDown
        size={16}
        className={cn(
          "shrink-0 text-tertiary transition-transform duration-150",
          !open && "-rotate-90",
        )}
      />
    </button>
  );
}

// --------------------------------------------------------------------------- //
// Awaiting approval
// --------------------------------------------------------------------------- //

function ReviewRow({
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
  onSaveEdit: (edited: { note: string; dm: string }) => void;
}) {
  const draft = lead.draft_message?.linkedin ?? {};
  const [editing, setEditing] = React.useState(false);
  const [note, setNote] = React.useState(draft.connection_note ?? "");
  const [dm, setDm] = React.useState(draft.followup_dm ?? "");

  React.useEffect(() => {
    if (!open) setEditing(false);
  }, [open]);

  const tooLong = note.length > MAX_LINKEDIN_CONNECTION_NOTE_CHARS;

  return (
    <Card weight="standard" className="overflow-hidden">
      <RowHeader
        name={lead.company_name}
        detail={lead.contact_name || "Contact unknown"}
        tag={{
          label: "Waiting for review",
          tone: "border-accent/30 bg-accent-soft text-accent",
        }}
        testMode={lead.dry_run}
        open={open}
        onToggle={onToggle}
      />

      {open ? (
        <div className="border-t border-border px-4 py-4">
          {editing ? (
            <div className="space-y-3">
              <div>
                <Label htmlFor={`note-${lead.lead_id}`}>Connection request note</Label>
                <Textarea
                  id={`note-${lead.lead_id}`}
                  rows={4}
                  className="mt-1.5"
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                />
                <p className={cn("mt-1 text-micro", tooLong ? "text-danger" : "text-tertiary")}>
                  {note.length} of {MAX_LINKEDIN_CONNECTION_NOTE_CHARS} characters
                  {tooLong ? " — LinkedIn will not accept this" : ""}
                </p>
              </div>
              <div>
                <Label htmlFor={`dm-${lead.lead_id}`}>Message once they accept</Label>
                <Textarea
                  id={`dm-${lead.lead_id}`}
                  rows={5}
                  className="mt-1.5"
                  value={dm}
                  onChange={(event) => setDm(event.target.value)}
                />
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="primary"
                  onClick={() => onSaveEdit({ note, dm })}
                  disabled={busy || tooLong}
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
              <p className="text-micro font-semibold text-secondary">
                Connection request note
              </p>
              <p className="mt-1.5 whitespace-pre-wrap text-meta leading-relaxed text-primary">
                {draft.connection_note || (
                  <span className="text-tertiary">Nothing written yet.</span>
                )}
              </p>

              {draft.followup_dm ? (
                <>
                  <p className="mt-4 text-micro font-semibold text-secondary">
                    Message once they accept
                  </p>
                  <p className="mt-1.5 whitespace-pre-wrap text-meta leading-relaxed text-secondary">
                    {draft.followup_dm}
                  </p>
                </>
              ) : null}

              <div className="mt-4 flex flex-wrap gap-2">
                <Button variant="primary" onClick={onApprove} disabled={busy}>
                  {busy ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />}
                  Approve
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => {
                    setNote(draft.connection_note ?? "");
                    setDm(draft.followup_dm ?? "");
                    setEditing(true);
                  }}
                  disabled={busy}
                >
                  <Pencil size={15} />
                  Edit
                </Button>
                <Button variant="ghost" onClick={onReject} disabled={busy}>
                  <X size={15} />
                  Reject
                </Button>
              </div>
            </>
          )}
        </div>
      ) : null}
    </Card>
  );
}

// --------------------------------------------------------------------------- //
// Approved, and in the user's hands
// --------------------------------------------------------------------------- //

function QueueRow({
  item,
  open,
  busy,
  onToggle,
  onMark,
}: {
  item: LinkedInQueueItem;
  open: boolean;
  busy: boolean;
  onToggle: () => void;
  onMark: (status: "sent" | "connected") => void;
}) {
  const { copied, copy } = useCopy();
  const connected = item.status === "connected";
  const message = connected ? item.followup_dm : item.connection_note;

  const tag =
    item.status === "pending"
      ? { label: "Ready to send", tone: "border-accent/30 bg-accent-soft text-accent" }
      : item.status === "sent"
        ? {
            label: "Waiting to connect",
            tone: "border-border bg-surface-raised text-secondary",
          }
        : {
            label: "Connected",
            tone: "border-success/30 bg-success-muted text-success",
          };

  return (
    <Card weight="standard" className="overflow-hidden">
      <RowHeader
        name={item.company_name}
        detail={`${item.contact_name || "Contact unknown"} · queued ${relativeTime(item.queued_at)}`}
        tag={tag}
        testMode={item.dry_run}
        open={open}
        onToggle={onToggle}
      />

      {open ? (
        <div className="border-t border-border px-4 py-4">
          <p className="text-micro font-semibold text-secondary">
            {connected ? "Message now that they have accepted" : "Connection request note"}
          </p>
          <p className="mt-1.5 whitespace-pre-wrap text-meta leading-relaxed text-primary">
            {message || <span className="text-tertiary">Nothing written yet.</span>}
          </p>

          {/* Not one of these is a Send button. */}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              onClick={() => void copy(message)}
              disabled={!message}
            >
              {copied ? <Check size={15} className="text-success" /> : <Copy size={15} />}
              {copied ? "Copied" : "Copy Message"}
            </Button>

            <Button asChild variant="secondary">
              <a href={item.linkedin_url} target="_blank" rel="noreferrer noopener">
                <Linkedin size={15} />
                Open LinkedIn
                <ArrowUpRight size={12} className="text-tertiary" />
              </a>
            </Button>

            {item.status === "pending" ? (
              <Button variant="primary" onClick={() => onMark("sent")} disabled={busy}>
                {busy ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />}
                Mark as Sent
              </Button>
            ) : item.status === "sent" ? (
              <Button variant="primary" onClick={() => onMark("connected")} disabled={busy}>
                {busy ? (
                  <Loader2 size={15} className="animate-spin" />
                ) : (
                  <Handshake size={15} />
                )}
                Mark as Connected
              </Button>
            ) : (
              <span className="text-micro text-tertiary">
                Connected{item.connected_at ? ` ${formatDate(item.connected_at)}` : ""} —
                send the message above when you are ready.
              </span>
            )}
          </div>
        </div>
      ) : null}
    </Card>
  );
}
