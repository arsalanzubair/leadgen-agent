/**
 * graphSpec.ts -- the N0-N9 topology, transcribed from src/graph.py.
 *
 * `graphNode` on each entry is the exact string src/graph.py registers with
 * StateGraph, and the edges below are the exact edges that module wires,
 * including the branches the FSD calls out specifically: N3's below-threshold
 * route to archive, and N5.5's split to N6a/N6b.
 *
 * Verified against `run_batch --graph` output, which renders the compiled
 * graph rather than a hand-drawn diagram.
 */

import type { GraphNodeSpec, NodeId } from "@/types/agent";

export const GRAPH_NODES: GraphNodeSpec[] = [
  {
    id: "N0",
    graphNode: "config_load",
    module: "n0_config_load.py",
    label: "Config load",
    description:
      "Loads and validates the tenant config, resolving the (niche, region) targets for this batch. A missing required field halts the batch before anything touches the outside world.",
    outputs: ["language", "batch targets"],
    usesLlm: false,
    humanInLoop: false,
  },
  {
    id: "N1",
    graphNode: "discovery",
    module: "n1_discovery.py",
    label: "Discovery",
    description:
      "Finds companies via Google Places (OpenStreetMap fallback) for local niches, or Apollo and hand-exported CSVs for B2B. Deduplicates against everything this tenant has seen before.",
    outputs: ["company_name", "website", "linkedin_url", "location", "dedupe_key"],
    usesLlm: false,
    humanInLoop: false,
  },
  {
    id: "N2",
    graphNode: "enrichment",
    module: "n2_enrichment.py",
    label: "Enrichment",
    description:
      "Scrapes the site for contact details and niche-appropriate signals, honouring robots.txt. Spends a Hunter lookup only on the batch's highest-priority leads. No email and no LinkedIn URL marks the lead unreachable.",
    outputs: ["contact_email", "contact_name", "signals", "unreachable"],
    usesLlm: false,
    humanInLoop: false,
  },
  {
    id: "N3",
    graphNode: "qualification",
    module: "n3_qualification.py",
    label: "Qualification",
    description:
      "Scores the lead against the niche's ideal-customer profile and explains the score in one sentence. Below the tenant threshold the lead is archived here.",
    outputs: ["fit_score", "fit_reason"],
    usesLlm: true,
    humanInLoop: false,
  },
  {
    id: "N3.5",
    graphNode: "channel_selection",
    module: "n3_5_channel_selection.py",
    label: "Channel selection",
    description:
      "Rule-based, no model call. The niche default (local is email-first, B2B is LinkedIn-first) is overridden by what contact data actually exists, then narrowed by the region's address rules.",
    outputs: ["channel"],
    usesLlm: false,
    humanInLoop: false,
  },
  {
    id: "N4",
    graphNode: "personalization",
    module: "n4_personalization.py",
    label: "Personalization",
    description:
      "Drafts the touch, citing one specific signal. A self-check verifies the draft actually references it; two failures flag the lead for manual drafting instead of sending generic copy. LinkedIn notes are capped at 300 characters by re-drafting, never truncation.",
    outputs: ["draft_message", "signal_referenced", "translated"],
    usesLlm: true,
    humanInLoop: false,
  },
  {
    id: "N5",
    graphNode: "human_approval",
    module: "n5_human_approval.py",
    label: "Human approval",
    description:
      "Pauses the lead's thread and waits for a person. The pause is checkpointed, so the queue survives the process exiting and stays reviewable days later.",
    outputs: ["approval_status"],
    usesLlm: false,
    humanInLoop: true,
  },
  {
    id: "N5.5",
    graphNode: "suppression_gate",
    module: "n5_5_suppression_gate.py",
    label: "Suppression & compliance",
    description:
      "Runs before every send, on every channel, on every run - never cached. Checks the tenant suppression list, then the region's machine-checkable compliance rules (opt-out line, sender identification, postal address, role-based address, legal basis, touch ceiling).",
    outputs: ["suppression_status"],
    usesLlm: false,
    humanInLoop: false,
  },
  {
    id: "N6a",
    graphNode: "email_outreach",
    module: "n6a_email_outreach.py",
    label: "Email outreach",
    description:
      "Sends through Gmail SMTP or Brevo inside the region's business-hours window. A spent daily budget queues the rest for the next run rather than failing the batch.",
    outputs: ["send_status", "sent_at"],
    usesLlm: false,
    humanInLoop: false,
  },
  {
    id: "N6b",
    graphNode: "linkedin_outreach",
    module: "n6b_linkedin_outreach.py",
    label: "LinkedIn outreach",
    description:
      "Queues the approved message for a person to send through LinkedIn themselves. Nothing here automates LinkedIn - no browser, no session replay, by design.",
    outputs: ["send_status = pending_manual_send"],
    usesLlm: false,
    humanInLoop: true,
  },
  {
    id: "N7",
    graphNode: "reply_monitoring",
    module: "n7_reply_monitoring.py",
    label: "Reply monitoring",
    description:
      "Polls the mailbox for a reply from this contact after the send. Out-of-office autoreplies are detected and excluded before classification, so they never count as a real reply.",
    outputs: ["reply_text", "reply_category"],
    usesLlm: true,
    humanInLoop: false,
  },
  {
    id: "N8",
    graphNode: "sequencer",
    module: "n8_followup_sequencer.py",
    label: "Follow-up sequencer",
    description:
      "Advances the cadence and schedules the next touch. An interested reply exits the sequence immediately with a same-day manual follow-up; the region's touch ceiling beats a longer cadence.",
    outputs: ["sequence_step", "next_action", "next_touch_due"],
    usesLlm: false,
    humanInLoop: false,
  },
  {
    id: "N9",
    graphNode: "crm",
    module: "n9_crm_analytics.py",
    label: "CRM & analytics",
    description:
      "Upserts one row per lead into the tenant's sheet, ready for a Looker report. This is the graph's only exit, so no lead can leave the system without a row explaining what happened to it.",
    outputs: ["CRM row"],
    usesLlm: false,
    humanInLoop: false,
  },
];

export const NODE_BY_ID: Record<NodeId, GraphNodeSpec> = Object.fromEntries(
  GRAPH_NODES.map((node) => [node.id, node]),
) as Record<NodeId, GraphNodeSpec>;

export type EdgeKind = "main" | "branch";

export interface GraphEdgeSpec {
  from: NodeId;
  to: NodeId;
  kind: EdgeKind;
  /** The condition, in the operator's language. Empty for unconditional edges. */
  label: string;
  /**
   * Whether this edge is DRAWN in the graph view.
   *
   * Every edge in src/graph.py is listed here, because this file documents the
   * real topology. But seven separate dashed lines converging on N9 from every
   * node that can archive a lead is unreadable spaghetti, and an unreadable
   * diagram communicates less than a simpler one. So the view draws the spine,
   * the N5.5 channel split, the N8 -> N4 cadence loop and the N3
   * below-threshold branch (the two branches the FSD calls out specifically),
   * and the remaining archive routes are surfaced per-node in the inspector
   * instead. Nothing is hidden -- it is relocated to where it reads.
   */
  layoutable: boolean;
}

/**
 * Every edge in src/graph.py. `branch` edges are the conditional ones that
 * route a lead off the happy path -- drawn dashed and dimmer, so the main
 * spine of the pipeline reads first.
 */
export const GRAPH_EDGES: GraphEdgeSpec[] = [
  { from: "N0", to: "N1", kind: "main", label: "", layoutable: true },
  { from: "N1", to: "N2", kind: "main", label: "", layoutable: true },
  { from: "N2", to: "N3", kind: "main", label: "", layoutable: true },
  { from: "N2", to: "N9", kind: "branch", label: "unreachable", layoutable: false },
  { from: "N3", to: "N3.5", kind: "main", label: "", layoutable: true },
  { from: "N3", to: "N9", kind: "branch", label: "below threshold", layoutable: true },
  { from: "N3.5", to: "N4", kind: "main", label: "", layoutable: true },
  { from: "N3.5", to: "N9", kind: "branch", label: "no channel", layoutable: false },
  { from: "N4", to: "N5", kind: "main", label: "", layoutable: true },
  { from: "N4", to: "N9", kind: "branch", label: "manual drafting", layoutable: false },
  { from: "N5", to: "N5.5", kind: "main", label: "approved", layoutable: true },
  { from: "N5", to: "N9", kind: "branch", label: "rejected", layoutable: false },
  { from: "N5.5", to: "N6a", kind: "main", label: "email", layoutable: true },
  { from: "N5.5", to: "N6b", kind: "main", label: "linkedin", layoutable: true },
  { from: "N5.5", to: "N9", kind: "branch", label: "blocked", layoutable: false },
  { from: "N6a", to: "N6b", kind: "branch", label: "both", layoutable: true },
  { from: "N6a", to: "N7", kind: "main", label: "", layoutable: true },
  { from: "N6b", to: "N7", kind: "main", label: "", layoutable: true },
  { from: "N7", to: "N8", kind: "main", label: "no reply", layoutable: true },
  { from: "N7", to: "N9", kind: "branch", label: "interested", layoutable: false },
  { from: "N8", to: "N4", kind: "branch", label: "next touch", layoutable: true },
  { from: "N8", to: "N9", kind: "main", label: "cadence end", layoutable: true },
];

/**
 * Layout positions for React Flow. Hand-placed, not auto-laid-out.
 *
 * A single left-to-right row of 13 nodes is ~2,400px wide, which fit-to-view
 * shrinks to about 40% -- legible as a shape, useless as information. So the
 * pipeline snakes: N0 to N5 left-to-right along the top, then N5.5 onward
 * right-to-left along the bottom, with the two send channels stacked where
 * N5.5 splits. That halves the width, and the whole graph fits at close to
 * 1:1 with every node label readable.
 *
 * The snake also happens to read correctly: the top row is everything that
 * happens before a human looks at it, and the bottom row is everything that
 * happens after.
 */
export const NODE_POSITIONS: Record<NodeId, { x: number; y: number }> = {
  // top row: discovery through approval
  N0: { x: 0, y: 0 },
  N1: { x: 200, y: 0 },
  N2: { x: 400, y: 0 },
  N3: { x: 600, y: 0 },
  "N3.5": { x: 800, y: 0 },
  N4: { x: 1000, y: 0 },
  N5: { x: 1200, y: 0 },
  // the turn, and the channel split
  "N5.5": { x: 1200, y: 235 },
  N6a: { x: 1000, y: 172 },
  N6b: { x: 1000, y: 306 },
  // bottom row, running back leftwards to the CRM
  N7: { x: 800, y: 235 },
  N8: { x: 600, y: 235 },
  N9: { x: 400, y: 235 },
};

/** The order a lead traverses, for the Lead Detail journey checklist. */
export const JOURNEY_ORDER: NodeId[] = [
  "N1",
  "N2",
  "N3",
  "N3.5",
  "N4",
  "N5",
  "N5.5",
  "N6a",
  "N7",
  "N8",
  "N9",
];
