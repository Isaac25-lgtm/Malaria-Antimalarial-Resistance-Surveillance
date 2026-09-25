/**
 * Application shell: navy sidebar, sticky top bar and the page outlet.
 *
 * The layout follows the MARS concept prototype. Two workspaces share it: a
 * national user sees the national navigation and title, a district or facility
 * user sees their own place. Which one is decided by the authenticated scope,
 * never by a username.
 *
 * Every figure in the shell - navigation counts, notifications, "data
 * through" - is read from the same governed endpoints the pages use. In live
 * mode the shell reads the cached live snapshot; otherwise it reads the
 * governed overview read model.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api, type Schemas } from "../api/client";
import { useAuth } from "../auth/context";
import type { AuthContextValue } from "../auth/context";
import type { PeriodSelection } from "../design-system/period";
import { useReportingPeriod } from "../features/operations/useReportingPeriod";
import "./app-shell.css";

type User = NonNullable<AuthContextValue["user"]>;
type Workspace = "national" | "district" | "facility";
type CountKey = "signals" | "investigations";

interface NavigationItem {
  to: string;
  label: string;
  icon: IconName;
  permission?: string;
  end?: boolean;
  count?: CountKey;
}

interface Notification {
  id: string;
  title: string;
  detail: string;
  tone: "red" | "amber" | "green" | "blue";
  to?: string;
}

/* ------------------------------------------------------------------------ */
/* Navigation                                                                */
/* ------------------------------------------------------------------------ */

function navigationFor(workspace: Workspace, overviewPath: string): NavigationItem[][] {
  const national = workspace === "national";
  return [
    [
      {
        to: overviewPath,
        label: "Overview",
        icon: "overview",
        end: true,
      },
      {
        to: "/signals",
        label: national ? "National Signals" : "Signals",
        icon: "signals",
        permission: "surveillance:view_aggregate",
        count: "signals",
      },
      {
        to: "/action-centre",
        label: national ? "Escalations & Investigations" : "Investigations",
        icon: "investigations",
        permission: "surveillance:view_aggregate",
        count: "investigations",
      },
      { to: "/national", label: "Map Explorer", icon: "map", permission: "geography:view" },
    ],
    [
      {
        to: "/analytics",
        label: national ? "Surveillance Analytics" : "Analytics",
        icon: "analytics",
        permission: "surveillance:view_aggregate",
      },
      {
        to: "/patients",
        label: national ? "Repeat-positive Surveillance" : "Patient Surveillance",
        icon: "patient",
        permission: "case:view_pseudonymous_evidence",
      },
      {
        to: "/commodities",
        label: "Commodities",
        icon: "commodities",
        permission: "surveillance:view_aggregate",
      },
      {
        to: "/data-quality",
        label: national ? "Data Quality & Coverage" : "Data Quality",
        icon: "quality",
        permission: "surveillance:view_aggregate",
      },
    ],
    [
      national
        ? { to: "/geography", label: "Districts", icon: "facilities", permission: "geography:view" }
        : { to: "/facilities", label: "Facilities", icon: "facilities", permission: "facility:view" },
      { to: "/reports", label: "Reports", icon: "reports", permission: "report:generate" },
      national
        ? {
            to: "/governance",
            label: "Governance",
            icon: "admin",
            permission: "configuration:view",
          }
        : {
            to: "/administration",
            label: workspace === "district" ? "District Administration" : "Administration",
            icon: "admin",
            permission: "configuration:view",
          },
    ],
  ];
}

/* ------------------------------------------------------------------------ */
/* Shell                                                                     */
/* ------------------------------------------------------------------------ */

export function AppShell() {
  const { user, signOut, can } = useAuth();
  const [period] = useReportingPeriod();
  const range = useMemo(
    () => ({ period_start: period.start, period_end: period.end }),
    [period],
  );
  const liveMode = user?.source_status?.mode === "live";

  const overview = useQuery({
    queryKey: ["surveillance", "overview", range],
    queryFn: () => api.overview(range),
    // Live navigation reads the live snapshot, never the governed read model.
    enabled: can("surveillance:view_aggregate") && !liveMode,
    retry: false,
  });

  // Read-only view of the snapshot the pages load. `enabled: false` means the
  // shell never starts a synchronisation or a request of its own.
  const liveSnapshot = useQuery<Schemas["LiveDashboardSnapshot"] | null>({
    queryKey: ["live", "dashboard", range],
    queryFn: () => api.latestLiveDashboard(range),
    enabled: false,
  });

  const workspace = workspaceOf(user);
  const pendingLiveWorkspace =
    user?.workspace?.authorization_status === "resolved" && user.mapping?.status !== "resolved";
  const overviewPath = pendingLiveWorkspace
    ? user?.landing_path || "/"
    : "/command-centre";

  const groups = navigationFor(workspace, overviewPath)
    .map((group) =>
      group.filter(
        (item) => !item.permission || (user?.permissions.includes(item.permission) ?? false),
      ),
    )
    .filter((group) => group.length > 0);

  const counts: Record<CountKey, number | null> = {
    signals: liveMode ? null : highPrioritySignalCount(overview.data?.signals_by_priority),
    investigations: liveMode ? null : openInvestigationCount(overview.data?.investigations_by_status),
  };

  const notifications = liveMode
    ? liveNotifications(liveSnapshot.data)
    : governedNotifications(overview.data);

  return (
    <div className="shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>

      <aside className="shell__sidebar">
        <div className="shell__brand">
          <MarsMark />
          <div>
            <p className="shell__wordmark">MARS</p>
            <p className="shell__product">{workspaceLine(workspace, user)}</p>
          </div>
        </div>

        <nav className="shell__nav" aria-label="Primary">
          {groups.map((group, index) => (
            <ul className="shell__nav-group" key={index}>
              {group.map((item) => {
                const count = item.count ? counts[item.count] : null;
                const urgent = item.count === "signals" && count != null && count > 0;
                return (
                  <li key={item.to}>
                    <NavLink
                      to={item.to}
                      end={item.end}
                      className={({ isActive }) =>
                        [
                          "shell__nav-link",
                          isActive ? "shell__nav-link--active" : "",
                          urgent ? "shell__nav-link--alert" : "",
                        ]
                          .filter(Boolean)
                          .join(" ")
                      }
                      aria-label={count != null ? countLabel(item, count) : undefined}
                    >
                      <Icon name={item.icon} />
                      <span className="shell__nav-label">{item.label}</span>
                      {count != null ? (
                        <span className="shell__nav-badge" aria-hidden="true">
                          {count}
                        </span>
                      ) : null}
                    </NavLink>
                  </li>
                );
              })}
            </ul>
          ))}
        </nav>

        <div className="shell__ministry">
          <CrestMark />
          <p>
            <b className="shell__ministry-name">Ministry of Health Uganda</b>
            <span className="shell__ministry-note">Malaria surveillance</span>
          </p>
        </div>
      </aside>

      <div className="shell__workspace">
        <header className="shell__toolbar">
          <div className="shell__scope">
            <h2 className="shell__scope-title">{scopeTitle(workspace, user)}</h2>
            <SourceStatusChip user={user} />
          </div>

          <div className="shell__toolbar-end">
            <div className="shell__meta shell__meta--optional">
              <span>Reporting period</span>
              <b>{formatPeriodRange(period)}</b>
            </div>
            <div className="shell__meta shell__meta--optional">
              <span>Data through</span>
              <b>
                <DataThrough
                  liveMode={liveMode}
                  snapshot={liveSnapshot.data}
                  refreshedAt={overview.data?.provenance?.analytics_refreshed_at ?? null}
                />
              </b>
            </div>
            <NotificationsButton items={notifications} />
            {user ? <AccountMenu user={user} workspace={workspace} onSignOut={signOut} /> : null}
          </div>
        </header>

        <main id="main-content" className="shell__main" tabIndex={-1}>
          <Outlet />
        </main>

        <footer className="shell__boundary" role="contentinfo">
          MARS signals indicate patterns requiring investigation. They do not confirm
          antimalarial resistance.
        </footer>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Top bar parts                                                             */
/* ------------------------------------------------------------------------ */

function SourceStatusChip({ user }: { user: AuthContextValue["user"] }) {
  const source = user?.source_status;
  if (!source || source.mode !== "live") return null;
  if (source.authentication !== "connected") {
    return <span className="shell__badge shell__badge--error">CONNECTION ERROR</span>;
  }
  const readiness = user?.data_readiness;
  const mapping = user?.mapping?.status ?? source.mapping;
  if (mapping === "pending" || mapping === "ambiguous") {
    return <span className="shell__badge shell__badge--pending">AUTHORIZED — MAPPING PENDING</span>;
  }
  if (readiness?.aggregate_sync !== "ready") {
    const place = placeName(user);
    return (
      <span className="shell__badge shell__badge--live">
        {place ? `LIVE — ${place.toUpperCase()} AUTHORIZED` : "LIVE — AUTHORIZED"}
      </span>
    );
  }
  return <span className="shell__badge shell__badge--live">AUTHORIZED — LIVE DATA AVAILABLE</span>;
}

function DataThrough({
  liveMode,
  snapshot,
  refreshedAt,
}: {
  liveMode: boolean;
  snapshot: Schemas["LiveDashboardSnapshot"] | null | undefined;
  refreshedAt: string | null;
}) {
  const stamp = liveMode ? (snapshot?.source_updated_at ?? snapshot?.synchronized_at) : refreshedAt;
  if (!stamp) return <span className="shell__meta-empty">Not yet synchronised</span>;
  return (
    <>
      <i className="shell__dot-ok" aria-hidden="true" />
      {formatTimestamp(stamp)}
    </>
  );
}

function NotificationsButton({ items }: { items: Notification[] }) {
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const location = useLocation();

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="shell__notifications" ref={container}>
      <button
        type="button"
        className="shell__icon-button"
        aria-label={`Notifications, ${items.length} open`}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.7" aria-hidden="true">
          <path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.7 21a2 2 0 0 1-3.4 0" />
        </svg>
        {items.length > 0 ? (
          <span className="shell__icon-count" aria-hidden="true">
            {items.length}
          </span>
        ) : null}
      </button>
      {open ? (
        <div className="shell__notif-panel" role="dialog" aria-label="Notifications">
          <div className="shell__notif-head">
            Notifications
            <span>{items.length} open</span>
          </div>
          {items.length === 0 ? (
            <p className="shell__notif-empty">Nothing needs attention for this period.</p>
          ) : (
            <ul>
              {items.map((item) => (
                <li key={item.id}>
                  <NotificationRow item={item} />
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}

function NotificationRow({ item }: { item: Notification }) {
  const body = (
    <>
      <span className={`shell__dot shell__dot--${item.tone}`} aria-hidden="true" />
      <span>
        <span className="shell__notif-title">{item.title}</span>
        <span className="shell__notif-detail">{item.detail}</span>
      </span>
    </>
  );
  return item.to ? (
    <Link className="shell__notif-item" to={item.to}>
      {body}
    </Link>
  ) : (
    <div className="shell__notif-item">{body}</div>
  );
}

function AccountMenu({
  user,
  workspace,
  onSignOut,
}: {
  user: User;
  workspace: Workspace;
  onSignOut: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const location = useLocation();

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    return () => document.removeEventListener("pointerdown", onPointer);
  }, [open]);

  return (
    <div className="shell__account" ref={container}>
      <button
        type="button"
        className="shell__account-button"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((value) => !value)}
      >
        <span className="shell__avatar" aria-hidden="true">
          {initials(user.display_name)}
        </span>
        <span className="shell__account-text">
          <b className="shell__account-name">{user.display_name}</b>
          <span className="shell__account-scope">{roleLine(user, workspace)}</span>
        </span>
      </button>
      {open ? (
        <div className="shell__account-menu" role="menu">
          <Link role="menuitem" to="/profile">
            Access profile
          </Link>
          <Link role="menuitem" to="/status">
            System status
          </Link>
          <button role="menuitem" type="button" onClick={() => void onSignOut()}>
            Sign out
          </button>
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Derivations                                                               */
/* ------------------------------------------------------------------------ */

function workspaceOf(user: AuthContextValue["user"]): Workspace {
  if (!user) return "district";
  if (user.has_national_scope || user.workspace?.scope_type === "national") return "national";
  if (user.facility_scope_ids.length > 0 || user.workspace?.scope_type === "facility") {
    return "facility";
  }
  return "district";
}

function placeName(user: AuthContextValue["user"]): string | null {
  if (!user) return null;
  const workspaceName = user.workspace?.authorization_status === "resolved" ? user.workspace.name : null;
  const raw =
    workspaceName ??
    user.geography_scopes.find((scope) => scope.level === "district")?.name ??
    user.geography_scopes[0]?.name ??
    null;
  if (!raw) return null;
  const trimmed = raw.replace(/\s+(district|facility)$/i, "").trim();
  return trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
}

function scopeTitle(workspace: Workspace, user: AuthContextValue["user"]): string {
  if (workspace === "national") return "Uganda, national surveillance";
  const place = placeName(user);
  const districts = (user?.geography_scopes ?? []).filter((scope) => scope.level === "district");
  if (workspace === "district" && districts.length > 1 && !user?.workspace?.name) {
    return "Authorised districts, Uganda";
  }
  if (!place) return "MARS workspace";
  return workspace === "facility" ? `${place}, Uganda` : `${place} district, Uganda`;
}

function workspaceLine(workspace: Workspace, user: AuthContextValue["user"]): string {
  if (workspace === "national") return "National surveillance workspace";
  const place = placeName(user);
  if (workspace === "facility") return "Facility workspace";
  return place ? `${place} district workspace` : "District workspace";
}

const ROLE_LABELS: Record<string, string> = {
  national_programme: "National Programme Officer",
  district_hsd: "District Health Officer",
  facility: "Facility Records Officer",
  analyst: "Surveillance Analyst",
  administrator: "System Administrator",
};

function roleLine(user: User, workspace: Workspace): string {
  const role = user.roles.map((code) => ROLE_LABELS[code]).find(Boolean);
  if (workspace === "national") return role ?? "National access";
  const place = placeName(user);
  const label = role ?? (workspace === "facility" ? "Facility access" : "District access");
  return place ? `${label}, ${place}` : label;
}

function initials(name: string): string {
  const parts = name
    .replace(/\(.*?\)/g, "")
    .split(/\s+/)
    .filter(Boolean);
  const first = parts.at(0) ?? "";
  const last = parts.at(-1) ?? "";
  const letters = parts.length > 1 ? first.charAt(0) + last.charAt(0) : first.slice(0, 2);
  return (letters || "?").toUpperCase();
}

function countLabel(item: NavigationItem, count: number): string {
  return item.count === "signals"
    ? `${item.label}, ${count} high-priority signals`
    : `${item.label}, ${count} open`;
}

type Bucket = { availability: string; items: { code: string; count: number | null }[] };

function highPrioritySignalCount(section: Bucket | undefined): number | null {
  if (!section || section.availability !== "available") return null;
  const total = section.items
    .filter((item) => item.code === "urgent" || item.code === "high")
    .reduce((sum, item) => sum + (item.count ?? 0), 0);
  return total > 0 ? total : null;
}

function openInvestigationCount(section: Bucket | undefined): number | null {
  if (!section || section.availability !== "available") return null;
  const total = section.items
    .filter((item) => item.code !== "closed")
    .reduce((sum, item) => sum + (item.count ?? 0), 0);
  return total > 0 ? total : null;
}

const PRIORITY_TONE: Record<string, Notification["tone"]> = {
  urgent: "red",
  high: "red",
  attention: "amber",
};

function governedNotifications(overview: Schemas["OverviewSnapshot"] | undefined): Notification[] {
  if (!overview) return [];
  const out: Notification[] = [];
  const signals = overview.recent_signals?.availability === "available"
    ? overview.recent_signals.items
    : [];
  for (const signal of signals) {
    const tone = PRIORITY_TONE[signal.priority];
    if (!tone || tone === "amber") continue;
    out.push({
      id: `signal:${signal.id}`,
      title: `${signal.title}, ${signal.priority} priority`,
      detail: `Period ${signal.period_start} to ${signal.period_end}`,
      tone,
      to: `/signals/${signal.id}`,
    });
  }
  for (const item of overview.needs_attention?.items ?? []) {
    if (!item.count || item.code === "high_priority_signals") continue;
    out.push({
      id: `attention:${item.code}`,
      title: `${item.count} ${item.label.toLowerCase()}`,
      detail: item.detail ?? "Open the section for detail.",
      tone: "amber",
      to: item.code === "commodity_alerts" ? "/commodities" : "/action-centre",
    });
  }
  return out.slice(0, 8);
}

function liveNotifications(snapshot: Schemas["LiveDashboardSnapshot"] | null | undefined): Notification[] {
  if (!snapshot) return [];
  const out: Notification[] = snapshot.operational_alerts.map((alert) => ({
    id: `live:${alert.id}`,
    title: `${alert.title} at ${alert.facility_name}`,
    detail: alert.detail,
    tone: alert.status === "action_required" ? "red" : "amber",
    to: alert.kind === "commodity" ? "/commodities" : "/data-quality",
  }));
  for (const warning of snapshot.warnings) {
    out.push({ id: `warning:${warning}`, title: "Synchronisation note", detail: warning, tone: "blue" });
  }
  return out.slice(0, 8);
}

/* ------------------------------------------------------------------------ */
/* Formatting                                                                */
/* ------------------------------------------------------------------------ */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function formatPeriodRange(period: PeriodSelection): string {
  const start = new Date(`${period.start}T00:00:00Z`);
  const end = new Date(`${period.end}T00:00:00Z`);
  const day = (value: Date) => String(value.getUTCDate()).padStart(2, "0");
  const month = (value: Date) => MONTHS[value.getUTCMonth()];
  if (start.getUTCFullYear() === end.getUTCFullYear() && start.getUTCMonth() === end.getUTCMonth()) {
    return `${day(start)} to ${day(end)} ${month(end)} ${end.getUTCFullYear()}`;
  }
  return `${day(start)} ${month(start)} ${start.getUTCFullYear()} to ${day(end)} ${month(end)} ${end.getUTCFullYear()}`;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const day = String(date.getDate()).padStart(2, "0");
  const time = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  return `${day} ${MONTHS[date.getMonth()]} ${date.getFullYear()}, ${time}`;
}

/* ------------------------------------------------------------------------ */
/* Marks and icons                                                           */
/* ------------------------------------------------------------------------ */

function MarsMark() {
  return (
    <svg className="shell__mark" width="32" height="32" viewBox="0 0 40 40" aria-hidden="true">
      <rect x="1.2" y="1.2" width="37.6" height="37.6" rx="6" fill="none" stroke="#2d74b6"
        strokeWidth="1.4" opacity=".65" />
      <path d="M11 29V13l9 9 9-9v16" fill="none" stroke="#7fb6e6" strokeWidth="2.4"
        strokeLinejoin="round" strokeLinecap="round" />
      <circle cx="20" cy="22" r="2.6" fill="#2d74b6" />
    </svg>
  );
}

function CrestMark() {
  return (
    <svg className="shell__crest" viewBox="0 0 32 32" aria-hidden="true">
      <circle cx="16" cy="16" r="15" fill="none" stroke="#4d7ba5" strokeWidth="1.2" />
      <path d="M16 6 L22 12 L22 22 L10 22 L10 12 Z" fill="none" stroke="#7ea9cd" strokeWidth="1.2" />
      <path d="M13 22 L13 16 L19 16 L19 22" fill="none" stroke="#7ea9cd" strokeWidth="1.2" />
    </svg>
  );
}

type IconName =
  | "overview"
  | "signals"
  | "investigations"
  | "map"
  | "analytics"
  | "patient"
  | "commodities"
  | "quality"
  | "facilities"
  | "reports"
  | "admin";

const ICON_PATHS: Record<IconName, string[]> = {
  overview: ["M3 13h8V3H3zM13 21h8V11h-8zM13 7h8V3h-8zM3 21h8v-4H3z"],
  signals: ["M3 12h3l2-6 4 14 3-9 2 3h4"],
  investigations: ["M18 11a7 7 0 1 1-14 0 7 7 0 0 1 14 0z", "M20 20l-3.5-3.5"],
  map: ["M9 3 3 5.6v15L9 18l6 3 6-2.6v-15L15 6z", "M9 3v15M15 6v15"],
  analytics: ["M4 19V9M10 19V4M16 19v-7M22 19h-20"],
  patient: ["M15.6 8a3.6 3.6 0 1 1-7.2 0 3.6 3.6 0 0 1 7.2 0z", "M5 20c0-3.6 3.2-6 7-6s7 2.4 7 6"],
  commodities: ["M3 8.5 12 4l9 4.5v7L12 20l-9-4.5z", "M3 8.5 12 13l9-4.5M12 13v7"],
  quality: ["M12 3l7.5 3v6c0 4.2-3 7.6-7.5 9-4.5-1.4-7.5-4.8-7.5-9V6z", "M9 12l2.2 2.2L15.5 10"],
  facilities: ["M4 21V8l8-5 8 5v13", "M9.5 21v-6h5v6M4 12h16"],
  reports: ["M6 3h8l4 4v14H6z", "M14 3v4h4M9 13h6M9 17h6M9 9h2"],
  admin: [
    "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
    "M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 7.5 19l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0-1.1-2.7H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 7.5l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 2.7-1.1V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 2.7 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0 1.1 2.7H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1.3z",
  ],
};

function Icon({ name }: { name: IconName }) {
  return (
    <svg className="shell__nav-icon" width="16" height="16" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden="true" focusable="false">
      {ICON_PATHS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
