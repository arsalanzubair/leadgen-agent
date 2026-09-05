/**
 * nav.ts -- the information architecture, in one place.
 *
 * Four destinations and Settings. That is the whole product.
 *
 * There are no groups any more. A "Outreach" or "Results" heading above two
 * items is a label for a category the user never asked about; with a list this
 * short, the items name themselves.
 *
 * Sessions is not in here on purpose: it is a list of the user's own past
 * searches, rendered by the sidebar from live data, not a fixed route.
 */

import {
  Linkedin,
  type LucideIcon,
  Mail,
  Plus,
  Settings,
  Users,
} from "lucide-react";

export interface NavItem {
  label: string;
  to: string;
  icon: LucideIcon;
  /** Matches nested routes. */
  match?: (pathname: string) => boolean;
}

/** The main rail, in order. */
export const NAV: NavItem[] = [
  { label: "New", to: "/", icon: Plus, match: (path) => path === "/" },
  { label: "Leads", to: "/leads", icon: Users },
  { label: "Email", to: "/email", icon: Mail },
  { label: "LinkedIn", to: "/linkedin", icon: Linkedin },
];

/** Pinned above the user row, away from the four. */
export const SETTINGS_ITEM: NavItem = {
  label: "Settings",
  to: "/settings",
  icon: Settings,
};

export function isActive(item: NavItem, pathname: string): boolean {
  if (item.match) return item.match(pathname);
  return pathname === item.to || pathname.startsWith(`${item.to}/`);
}
