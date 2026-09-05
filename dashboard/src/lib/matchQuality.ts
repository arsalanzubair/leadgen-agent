/**
 * matchQuality.ts -- the buckets people actually filter by.
 *
 * These are defined against real backend fields, not against a number someone
 * picked for the UI. The threshold is always the workspace's own
 * `fit_score_threshold`; nothing here invents one.
 *
 *   Good match    fit_score >= threshold, and the lead is still live
 *   Needs review  a drafted message is waiting for a human decision
 *   Not a match   the backend's own `below_threshold` outcome
 *   Unreachable   no email and no LinkedIn profile was found
 *   Contacted     something has gone out, or is queued for the user to send
 *   Replied       they wrote back (an out-of-office is not a reply)
 *
 * Two details are worth stating outright, because both are places where a
 * plausible-looking shortcut would be wrong:
 *
 * 1. `approval_status` DEFAULTS to `pending` on a freshly discovered lead --
 *    it is the initial value in the backend's own state constructor, not a
 *    signal. So "needs review" additionally requires that a draft exists.
 *    Without that, every business found in the last five minutes would claim
 *    to be waiting on the user. The spec's "regardless of score" is honoured
 *    exactly: the score is not consulted, and neither is whether an earlier
 *    message already went out -- every touch needs approving, not just the
 *    first.
 *
 * 2. Not every lead has a bucket. A lead the user rejected, having scored 82,
 *    is not a good match (it is archived), is not "not a match" (it cleared
 *    the bar), and is not unreachable. Rather than stretch a bucket to cover
 *    it or invent a seventh, `matchQuality` returns null and the caller shows
 *    the lead's outcome instead -- which is the more useful thing to read
 *    there anyway.
 */

import {
  hasDraft,
  hasReplied,
  isActive,
  isAwaitingReview,
  isContacted,
  isUnreachable,
} from "@/lib/leadFields";
import { leadOutcome } from "@/lib/outcome";
import type { MatchQuality } from "@/lib/statusLabels";
import type { Lead } from "@/types/lead";

export type { MatchQuality };

// The primitives live in leadFields.ts; re-exported here so a component that
// only cares about lead buckets has one import instead of two.
export { hasDraft, hasReplied, isActive, isAwaitingReview, isContacted, isUnreachable };

/** Every bucket a lead can be in, in the order they appear. */
export const MATCH_QUALITIES: MatchQuality[] = [
  "good_match",
  "needs_review",
  "contacted",
  "replied",
  "not_a_match",
  "unreachable",
];

/**
 * The four offered as tabs above the list, plus All.
 *
 * "Not a match" and "Unreachable" are real statuses and are labelled as such
 * wherever a lead in one of them appears -- they are just not tabs, because a
 * tab is a place to go and neither of those is somewhere anybody needs to go
 * on purpose. Both are still reachable through Filters and through a URL, so
 * a link into one keeps working.
 */
export const MATCH_QUALITY_TABS: MatchQuality[] = [
  "good_match",
  "needs_review",
  "contacted",
  "replied",
];

/**
 * The one bucket a lead is shown as, or null when none of the six fits (see
 * the note at the top of this file).
 */
export function matchQuality(lead: Lead, threshold: number): MatchQuality | null {
  if (hasReplied(lead)) return "replied";
  if (isAwaitingReview(lead)) return "needs_review";
  if (isContacted(lead)) return "contacted";
  if (isUnreachable(lead)) return "unreachable";
  if (leadOutcome(lead) === "below_threshold") return "not_a_match";
  if (lead.fit_score >= threshold && !lead.archived) return "good_match";
  return null;
}

/** Does this lead match a chosen filter bucket? */
export function inQuality(
  lead: Lead,
  quality: MatchQuality | "all",
  threshold: number,
): boolean {
  if (quality === "all") return true;
  return matchQuality(lead, threshold) === quality;
}

/** Counts for the filter chips. Only ever computed over the active list. */
export function qualityCounts(
  leads: Lead[],
  threshold: number,
): Record<MatchQuality | "all", number> {
  const counts: Record<MatchQuality | "all", number> = {
    all: leads.length,
    good_match: 0,
    needs_review: 0,
    contacted: 0,
    replied: 0,
    not_a_match: 0,
    unreachable: 0,
  };
  for (const lead of leads) {
    const quality = matchQuality(lead, threshold);
    if (quality) counts[quality] += 1;
  }
  return counts;
}
