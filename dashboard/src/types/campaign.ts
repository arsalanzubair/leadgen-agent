/**
 * campaign.ts -- presentation-layer groupings.
 *
 * A "campaign" is this UI's name for one audience the workspace is working
 * through. The backend has no Campaign object: it has configured audiences
 * plus the (audience, region) pairs a run resolves. So everything here is
 * derived from real lead data rather than stored, and it lives in its own file
 * so it is obvious which types mirror the backend (lead.ts) and which are
 * groupings this UI invented (this one).
 */

import type { Channel, Region } from "./lead";

export type CampaignStatus = "active" | "paused" | "completed" | "draft";

export interface Campaign {
  id: string;
  tenant_id: string;
  name: string;
  /** The audience this covers -- matches a configured niche id. */
  niche_id: string;
  niche_label: string;
  niche_type: "local_business" | "b2b";
  regions: Region[];
  channels: Channel[];
  status: CampaignStatus;
  created_at: string;
  last_run_at: string | null;
  fit_score_threshold: number;
  /** How many new businesses a run of this audience should turn up. */
  min_expected_leads: number;
  /** Who this audience is, in a sentence. */
  icp_description: string;
  good_signals: string[];
  stats: CampaignStats;
}

export interface CampaignStats {
  leads: number;
  qualified: number;
  contacted: number;
  replies: number;
  pending_approval: number;
  archived: number;
  /** Percentage of businesses found that were worth contacting. */
  qualification_rate: number;
  /** Percentage of contacted businesses that wrote back. */
  reply_rate: number;
}

export interface TimeSeriesPoint {
  date: string;
  leads: number;
  qualified: number;
  contacted: number;
  replies: number;
}

export interface BreakdownPoint {
  key: string;
  label: string;
  value: number;
  /** Secondary measure, e.g. good matches within this group. */
  secondary?: number;
}

export interface ChannelPerformance {
  channel: Channel;
  sent: number;
  replies: number;
  interested: number;
  reply_rate: number;
}

export interface FitScoreBucket {
  /** e.g. "60-69" */
  bucket: string;
  count: number;
  /** True when the whole bucket sits below the user's match threshold. */
  below_threshold: boolean;
}

/**
 * One row of the Performance table: a place, an audience and a channel.
 *
 * The three together, rather than three separate breakdowns, because the
 * question this table answers is "which combination is working" -- email to
 * dental practices in the US can be the best row on the page while email
 * overall and the US overall both look ordinary.
 */
export interface SegmentRow {
  key: string;
  region: Region;
  niche_id: string;
  niche_label: string;
  channel: Channel;
  sent: number;
  replies: number;
  /** Percentage of the sent messages in this row that got a reply. */
  reply_rate: number;
  /** Replies that said yes. See PipelineStage's note on "booked". */
  booked: number;
}

export interface AnalyticsBundle {
  timeseries: TimeSeriesPoint[];
  /** Region x audience x channel, for the Performance table. */
  by_segment: SegmentRow[];
  by_region: BreakdownPoint[];
  by_niche: BreakdownPoint[];
  channel_performance: ChannelPerformance[];
  fit_distribution: FitScoreBucket[];
  qualification_rate: number;
  reply_rate: number;
  fit_score_threshold: number;
}
