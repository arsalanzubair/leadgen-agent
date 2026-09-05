/**
 * outcome.ts -- derived views of a lead.
 *
 * `leadOutcome()` reimplements the backend's own outcome derivation with the
 * identical precedence order. It is derived there too rather than stored, so
 * recomputing it here mirrors the contract instead of duplicating state -- but
 * the ORDER matters, and the comments marking why are load-bearing.
 */

import type { Lead, LeadOutcome, PipelineStage } from "@/types/lead";
import { APPROVED_STATUSES } from "@/types/lead";
import type { RunStage } from "@/types/agent";
import { archiveReasonLabel } from "@/lib/statusLabels";
import {
  hasDraft,
  hasReplied,
  isAwaitingReview,
  isContacted,
  isUnreachable,
} from "@/lib/leadFields";

export function leadOutcome(lead: Lead): LeadOutcome {
  if (lead.needs_manual_review) return "needs_manual_review";
  if (lead.reply_category === "interested") return "interested_manual_handoff";
  if (lead.unreachable) return "unreachable";
  if (lead.suppression_status === "blocked_optout") return "suppressed";
  if (lead.suppression_status === "blocked_compliance") return "blocked_compliance";
  if (lead.archived) return lead.archive_reason || "archived";
  if (lead.send_status === "rate_limited") return "queued_for_next_run";
  if (lead.send_status === "pending_manual_send") return "awaiting_manual_linkedin_send";

  // The backend resets approval and send status when it schedules the next
  // follow-up, so a lead part-way through a sequence looks untouched if you
  // only read those two fields. `last_touch_at` is the one that says whether
  // anything has actually gone out.
  if (lead.next_touch_due && lead.last_touch_at) {
    return `in_sequence_touch_${lead.sequence_step + 1}_scheduled`;
  }
  if (lead.send_status === "sent") return "sent_awaiting_reply";
  if (lead.approval_status === "pending" && !lead.last_touch_at) return "awaiting_approval";
  return lead.send_status || "in_progress";
}

// --------------------------------------------------------------------------- //
// The funnel on Home
// --------------------------------------------------------------------------- //

/**
 * The furthest point a lead reached. Counts are cumulative: a contacted lead
 * also counts as found, reachable and a good match.
 */
export function reachedStage(lead: Lead, stage: PipelineStage, threshold: number): boolean {
  switch (stage) {
    case "discovered":
      return true;
    case "qualified":
      return lead.fit_score >= threshold && !isUnreachable(lead);
    case "approved":
      return APPROVED_STATUSES.includes(lead.approval_status) || !!lead.last_touch_at;
    case "contacted":
      return isContacted(lead);
    case "replied":
      return hasReplied(lead);
    case "booked":
      // The furthest this product can see. A reply classified as interested is
      // handed to the user, and what happens next happens off this system.
      return lead.reply_category === "interested";
  }
}

// --------------------------------------------------------------------------- //
// What a lead's history looks like, told as events
// --------------------------------------------------------------------------- //

export interface TimelineEntry {
  stage: RunStage;
  status: "done" | "current" | "waiting" | "skipped" | "problem";
  /** One sentence about what happened, in the user's terms. */
  summary: string;
  at: string | null;
}

/**
 * Reconstruct a lead's history from its own fields.
 *
 * This is inference, not a stored trace: the backend keeps the lead's current
 * state rather than an event log, so each entry is derived from the fields
 * that particular piece of work writes. Where something cannot have happened,
 * the entry says so rather than being dropped -- a gap is information, and
 * silently omitting it makes the history look shorter than it was.
 */
export function leadTimeline(lead: Lead, threshold: number): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  const reachable = !isUnreachable(lead);
  const qualified = lead.fit_score >= threshold;
  const drafted = hasDraft(lead);
  const contacted = isContacted(lead);

  entries.push({
    stage: "finding",
    status: "done",
    summary: "Found and checked against everything you have already seen",
    at: lead.discovered_at,
  });

  entries.push({
    stage: "researching",
    status: reachable ? "done" : "problem",
    summary: reachable
      ? lead.signals.length
        ? `Found a way to contact them, and ${lead.signals.length === 1 ? "one thing" : `${lead.signals.length} things`} worth mentioning`
        : "Found a way to contact them, but nothing specific to mention"
      : "No email address and no LinkedIn profile could be found",
    at: lead.discovered_at,
  });

  entries.push({
    stage: "matching",
    status: !reachable ? "skipped" : qualified ? "done" : "problem",
    summary: !reachable
      ? "Not scored - there was no way to reach them"
      : qualified
        ? `Scored ${lead.fit_score} out of 100, above your bar of ${threshold}`
        : `Scored ${lead.fit_score} out of 100, below your bar of ${threshold}`,
    at: lead.discovered_at,
  });

  const routed = reachable && qualified;
  entries.push({
    stage: "choosing_channel",
    status: routed ? "done" : "skipped",
    summary: routed
      ? lead.channel === "both"
        ? "Reachable by email and on LinkedIn"
        : lead.channel === "linkedin"
          ? "Best reached on LinkedIn"
          : "Best reached by email"
      : "Skipped - they did not get this far",
    at: null,
  });

  entries.push({
    stage: "drafting",
    status: drafted ? "done" : routed ? "problem" : "skipped",
    summary: drafted
      ? lead.draft_message.signal_referenced
        ? `Message written about "${lead.draft_message.signal_referenced}"`
        : "Message written"
      : routed
        ? "No message written - nothing specific enough to say"
        : "Skipped - they did not get this far",
    at: null,
  });

  entries.push({
    stage: "reviewing",
    status:
      lead.approval_status === "rejected"
        ? "problem"
        : APPROVED_STATUSES.includes(lead.approval_status)
          ? "done"
          : drafted
            ? "current"
            : "skipped",
    summary:
      lead.approval_status === "rejected"
        ? "You rejected it, so nothing was sent"
        : lead.approval_status === "edited"
          ? "You edited it, then approved it"
          : lead.approval_status === "approved"
            ? "You approved it"
            : drafted
              ? "Waiting for you to review it"
              : "Nothing to review",
    at: null,
  });

  const cleared = lead.suppression_status === "clear";
  entries.push({
    stage: "checking_rules",
    status: !drafted ? "skipped" : cleared ? "done" : "problem",
    summary: !drafted
      ? "Nothing to check"
      : cleared
        ? "Checked against local rules and your do-not-contact list"
        : lead.suppression_status === "blocked_optout"
          ? "They have opted out, so they will not be contacted"
          : "Local rules do not allow contacting them",
    at: null,
  });

  entries.push({
    stage: lead.channel === "linkedin" ? "linkedin_prep" : "sending_email",
    status: contacted ? "done" : cleared && drafted ? "current" : "skipped",
    summary: !contacted
      ? cleared && drafted
        ? "Ready to go out"
        : "Never went out"
      : lead.dry_run
        ? lead.channel === "linkedin"
          ? "Test Mode - this would have been queued for you to send"
          : "Test Mode - this would have been sent, nothing actually went out"
        : lead.send_status === "pending_manual_send"
          ? "Waiting for you to send it on LinkedIn"
          : lead.channel === "both"
            ? "Email sent; the LinkedIn message is waiting for you to send"
            : "Sent",
    at: lead.sent_at,
  });

  entries.push({
    stage: "watching_replies",
    status: hasReplied(lead) ? "done" : contacted ? "current" : "skipped",
    summary: hasReplied(lead)
      ? lead.reply_category === "interested"
        ? "They replied and are interested"
        : lead.reply_category === "not_interested"
          ? "They replied to say no thanks"
          : "They replied with questions"
      : contacted
        ? "Watching for a reply"
        : "Nothing was sent, so there is nothing to watch for",
    at: null,
  });

  entries.push({
    stage: "following_up",
    status: lead.next_touch_due ? "done" : contacted ? "done" : "skipped",
    summary: lead.next_touch_due
      ? `Follow-up ${lead.sequence_step + 1} scheduled`
      : contacted
        ? "No more follow-ups planned"
        : "No follow-ups planned yet",
    at: lead.last_touch_at,
  });

  return entries;
}

// --------------------------------------------------------------------------- //
// The three questions the lead detail page answers
// --------------------------------------------------------------------------- //

export interface CriterionCheck {
  label: string;
  met: boolean;
  /** Why it counts as met or not, when that is not self-evident. */
  note: string;
}

export interface NextStep {
  headline: string;
  detail: string;
  action_label: string;
  action_href: string | null;
}

export interface LeadStory {
  /** Why this business is worth contacting, in a sentence. */
  interesting: string;
  /** Whether the signal is specific enough to lead a message with. */
  has_specific_signal: boolean;
  /** How they measure up against what the user asked for. */
  criteria: CriterionCheck[];
  next: NextStep;
}

/**
 * Why this lead is interesting, why it matches, and what to do next -- in that
 * order, because that is the order somebody looking at a stranger's business
 * actually wants it.
 */
export function leadStory(
  lead: Lead,
  threshold: number,
  targeting?: { label: string; good_signals: string[]; must_have: string[] },
): LeadStory {
  const signal = lead.draft_message?.signal_referenced || lead.signals[0] || "";

  const interesting = signal
    ? `${lead.company_name} ${signalSentence(signal)}`
    : lead.fit_reason ||
      `Nothing specific stood out about ${lead.company_name || "this business"} yet.`;

  // Compare against what the user asked for where we know it, and against the
  // lead's own fields where we do not. Never claim a criterion was checked
  // that was not.
  const criteria: CriterionCheck[] = [];

  criteria.push({
    label: `Scores at or above your bar of ${threshold}`,
    met: lead.fit_score >= threshold,
    note: lead.fit_reason || "",
  });

  criteria.push({
    label: "There is a way to contact them",
    met: !isUnreachable(lead),
    note: lead.contact_email
      ? `Email: ${lead.contact_email}`
      : lead.linkedin_url
        ? "LinkedIn profile found"
        : "No email and no LinkedIn profile",
  });

  if (targeting?.good_signals?.length) {
    const matched = targeting.good_signals.filter((wanted) =>
      lead.signals.some((found) => overlaps(found, wanted)),
    );
    criteria.push({
      label: "Shows a signal you said you were looking for",
      met: matched.length > 0,
      note: matched.length
        ? matched.join("; ")
        : lead.signals.length
          ? `Found other signals instead: ${lead.signals.join("; ")}`
          : "No signals found",
    });
  } else if (lead.signals.length) {
    criteria.push({
      label: "Shows a signal worth mentioning",
      met: true,
      note: lead.signals.join("; "),
    });
  }

  if (targeting?.must_have?.length) {
    criteria.push({
      label: targeting.must_have[0],
      met: Boolean(lead.website || lead.linkedin_url),
      note: lead.website || lead.linkedin_url || "Nothing found",
    });
  }

  return {
    interesting,
    has_specific_signal: Boolean(signal),
    criteria,
    next: nextStepFor(lead, threshold),
  };
}

/** Turns a raw signal phrase into something that reads as a sentence. */
function signalSentence(signal: string): string {
  const clean = signal.trim().replace(/\.$/, "");
  if (/^(has|is|are|was|were|takes|posts|runs|shows|does|uses|hiring)\b/i.test(clean)) {
    return `${clean.charAt(0).toLowerCase()}${clean.slice(1)}.`;
  }
  return `has one thing worth mentioning: ${clean.charAt(0).toLowerCase()}${clean.slice(1)}.`;
}

/** Loose word overlap -- enough to tell "no online booking" from "hiring". */
function overlaps(a: string, b: string): boolean {
  const words = (text: string) =>
    new Set(
      text
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, " ")
        .split(/\s+/)
        .filter((word) => word.length > 3),
    );
  const left = words(a);
  const right = words(b);
  let shared = 0;
  for (const word of right) if (left.has(word)) shared += 1;
  return shared >= 2 || (right.size === 1 && shared === 1);
}

/** One recommended action, never a restatement of the status. */
export function nextStepFor(lead: Lead, threshold: number): NextStep {
  if (lead.needs_manual_review) {
    return {
      headline: "Take a look at this one yourself",
      detail:
        lead.manual_review_reason ||
        "Something did not go through cleanly, so it was set aside rather than sent.",
      action_label: "Review it",
      action_href: "/outreach/approvals",
    };
  }
  if (lead.reply_category === "interested") {
    return {
      headline: "Reply to them yourself",
      detail:
        "They are interested. Follow-ups have stopped so nothing goes out on top of a real conversation.",
      action_label: lead.contact_email ? `Email ${lead.contact_name || "them"}` : "Open LinkedIn",
      action_href: lead.contact_email
        ? `mailto:${lead.contact_email}`
        : lead.linkedin_url || null,
    };
  }
  if (lead.suppression_status !== "clear") {
    return {
      headline: "Nothing to do",
      detail:
        lead.suppression_status === "blocked_optout"
          ? "They asked not to be contacted, so they never will be."
          : "Local rules do not allow contacting them.",
      action_label: "",
      action_href: null,
    };
  }
  if (isUnreachable(lead)) {
    return {
      headline: "Add a contact if you have one",
      detail:
        "No email address or LinkedIn profile was found, so there is nowhere to send anything.",
      action_label: lead.website ? "Open their website" : "",
      action_href: lead.website || null,
    };
  }
  if (lead.send_status === "pending_manual_send") {
    return {
      headline: "Send this one on LinkedIn yourself",
      detail:
        "The message is written and waiting. Copy it, open their profile, and send it from your own account.",
      action_label: "Open the LinkedIn list",
      action_href: "/outreach/linkedin",
    };
  }
  if (isAwaitingReview(lead)) {
    return {
      headline: "Review the message",
      detail: "It is written and waiting for your approval before anything goes out.",
      action_label: "Review it",
      action_href: "/outreach/approvals",
    };
  }
  if (lead.next_touch_due) {
    return {
      headline: "Nothing to do - a follow-up is scheduled",
      detail: `Follow-up ${lead.sequence_step + 1} goes out automatically unless they reply first.`,
      action_label: "See follow-ups",
      action_href: "/outreach/follow-ups",
    };
  }
  if (lead.send_status === "sent") {
    return {
      headline: "Nothing to do - waiting on them",
      detail: "The message has gone out. Replies are watched for automatically.",
      action_label: "",
      action_href: null,
    };
  }
  if (lead.archived && lead.archive_reason === "below_threshold") {
    return {
      headline: "Not worth contacting",
      detail: `Scored ${lead.fit_score} against your bar of ${threshold}. Lower your bar in Settings if you want businesses like this one included.`,
      action_label: "Adjust your bar",
      action_href: "/settings/targeting",
    };
  }
  if (lead.approval_status === "rejected") {
    return {
      headline: "You rejected this message",
      detail: "Nothing was sent. It will not come back unless you run this audience again.",
      action_label: "",
      action_href: null,
    };
  }
  if (lead.archived) {
    return {
      headline: "Closed",
      detail: archiveReasonLabel(lead.archive_reason) || "This one is finished with.",
      action_label: "",
      action_href: null,
    };
  }
  return {
    headline: "Waiting its turn",
    detail: "It is queued and will be picked up on the next run.",
    action_label: "",
    action_href: null,
  };
}

/** Does this lead need a human decision right now? Drives every badge count. */
export function needsHumanDecision(lead: Lead): boolean {
  if (lead.archived) return false;
  if (lead.needs_manual_review) return true;
  if (lead.reply_category === "interested") return true;
  if (lead.send_status === "pending_manual_send") return true;
  return isAwaitingReview(lead);
}
