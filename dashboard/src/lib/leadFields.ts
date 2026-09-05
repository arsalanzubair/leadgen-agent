/**
 * leadFields.ts -- the primitive questions you can ask about one lead.
 *
 * These read backend fields directly and answer yes or no. They live in their
 * own module because both `outcome.ts` (what happened to this lead) and
 * `matchQuality.ts` (which bucket it belongs in) need them, and having either
 * of those import the other would put a cycle in the graph that works right
 * up until module evaluation order changes.
 */

import type { Lead } from "@/types/lead";
import { APPROVED_STATUSES } from "@/types/lead";

/** Does this lead have a drafted message at all? */
export function hasDraft(lead: Lead): boolean {
  const draft = lead.draft_message ?? {};
  return Boolean(
    draft.email?.subject ||
      draft.email?.body ||
      draft.linkedin?.connection_note ||
      draft.linkedin?.followup_dm,
  );
}

/** Has anything gone out, or been queued for the user to send by hand? */
export function isContacted(lead: Lead): boolean {
  return Boolean(
    lead.last_touch_at ||
      lead.send_status === "sent" ||
      lead.send_status === "pending_manual_send",
  );
}

/**
 * Did they write back? An out-of-office is an autoresponder, not a person, and
 * counting it as a reply would flatter every report in the product.
 */
export function hasReplied(lead: Lead): boolean {
  return lead.reply_category !== "no_reply" && lead.reply_category !== "out_of_office";
}

/** No email and no LinkedIn profile -- there is no way to reach them. */
export function isUnreachable(lead: Lead): boolean {
  return lead.unreachable || (!lead.contact_email && !lead.linkedin_url);
}

/**
 * Is a drafted message waiting on the user?
 *
 * `approval_status` DEFAULTS to `pending` on a freshly discovered lead -- that
 * is the backend's initial value, not a signal -- so a draft has to exist too.
 * Without that condition, every business found in the last five minutes would
 * claim to be waiting on the user.
 *
 * What this deliberately does NOT check is whether anything has gone out
 * already. Approval is required for EVERY touch, not just the first: a lead
 * part-way through a sequence, with a second message written and waiting, is
 * exactly as much a decision for the user as a brand-new one. An earlier
 * version of this excluded anything with a `last_touch_at`, which emptied the
 * approvals queue of precisely the follow-ups somebody needed to action, and
 * did it silently -- the screen just said "you are all caught up".
 */
export function isAwaitingReview(lead: Lead): boolean {
  if (lead.archived || isUnreachable(lead)) return false;
  if (APPROVED_STATUSES.includes(lead.approval_status)) return false;
  if (lead.approval_status === "rejected") return false;
  return lead.approval_status === "pending" && hasDraft(lead);
}

/**
 * Whether a lead belongs in normal browsing.
 *
 * A lead that has opted out, or that local rules forbid contacting, is not
 * shown as a status in the active list -- it simply is not in it. Presenting
 * "opted out" as one lead state among many invites someone to try anyway.
 */
export function isActive(lead: Lead): boolean {
  return (
    lead.suppression_status !== "blocked_optout" &&
    lead.suppression_status !== "blocked_compliance"
  );
}
