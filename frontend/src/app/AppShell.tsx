/**
 * Application shell: dark-navy sidebar and independently scrolling workspace.
 */

import { useMemo } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import { useAuth } from "../auth/context";
import type { AuthContextValue } from "../auth/context";
import { monthPeriod } from "../design-system/period";
import "./app-shell.css";

interface NavigationItem {
  to: string;
  label: string;
  icon: string;
  permission?: string;
  end?: boolean;
  badge?: "signals";
}

const PRIMARY_NAVIGATION: NavigationItem[] = [
  { to: "/command-centre", label: "Overview", icon: "overview", permission: "surveillance:view_aggregate", end: true },
  { to: "/signals", label: "Signals", icon: "signals", permission: "surveillance:view_aggregate", badge: "signals" },
  { to: "/action-centre", label: "Investigations", icon: "investigations", permission: "surveillance:view_aggregate" },
  { to: "/national", label: "Map Explorer", icon: "map", permission: "geography:view" },
  { to: "/analytics", label: "Analytics", icon: "analytics", permission: "surveillance:view_aggregate" },
  { to: "/commodities", label: "Commodities", icon: "commodities", permission: "surveillance:view_aggregate" },
  { to: "/data-quality", label: "Data Quality", icon: "quality", permission: "surveillance:view_aggregate" },
  { to: "/facilities", label: "Facilities", icon: "facilities", permission: "facility:view" },
  { to: "/reports", label: "Reports", icon: "reports", permission: "report:generate" },
  { to: "/administration", label: "Administration", icon: "admin", permission: "configuration:view" },
];

export function AppShell() {
  const { user, signOut, can } = useAuth();
  const range = useMemo(() => {
    const period = monthPeriod(-1);
    return { period_start: period.start, period_end: period.end };
  }, []);

  const overview = useQuery({
    queryKey: ["surveillance", "overview", range],
    queryFn: () => api.overview(range),
    enabled: can("surveillance:view_aggregate"),
    retry: false,
  });

  const visibleItems = PRIMARY_NAVIGATION.filter(
    (item) => !item.permission || (user?.permissions.includes(item.permission) ?? false),
  );
  const signalCount = highPrioritySignalCount(overview.data?.signals_by_priority);

  return (
    <div className="shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>

      <aside className="shell__sidebar">
        <div className="shell__brand">
          <p className="shell__wordmark">MARS</p>
          <p className="shell__product">Malaria Antimalarial Resistance Surveillance</p>
        </div>

        <nav className="shell__nav" aria-label="Primary">
          <ul>
            {visibleItems.map((item) => {
              const badge = item.badge === "signals" ? signalCount : null;
              return (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) =>
                      isActive ? "shell__nav-link shell__nav-link--active" : "shell__nav-link"
                    }
                    aria-label={
                      badge != null
                        ? `${item.label}, ${badge} high-priority signals`
                        : undefined
                    }
                  >
                    <NavIcon name={item.icon} />
                    <span>{item.label}</span>
                    {badge != null ? (
                      <span className="shell__nav-badge" aria-hidden="true">
                        {badge}
                      </span>
                    ) : null}
                  </NavLink>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="shell__ministry">
          <p className="shell__ministry-name">Ministry of Health Uganda</p>
          <p className="shell__ministry-note">Text attribution · no crest on file</p>
        </div>
      </aside>

      <div className="shell__workspace">
        <header className="shell__toolbar">
          <SourceStatusChip user={user} />
          <div className="shell__toolbar-end">
            <button type="button" className="shell__icon-button" aria-label="Notifications">
              <span aria-hidden="true">●</span>
            </button>
            {user ? (
              <div className="shell__account">
                <div className="shell__account-name">{user.display_name}</div>
                <div className="shell__account-scope mono">{formatScope(user)}</div>
              </div>
            ) : null}
            <button type="button" className="button" onClick={() => void signOut()}>
              Sign out
            </button>
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

function SourceStatusChip({
  user,
}: {
  user: AuthContextValue["user"];
}) {
  if (user?.is_synthetic) {
    return (
      <span className="chip chip--attention" title="Synthetic development session">
        Development session
      </span>
    );
  }
  const source = user?.source_status;
  if (!source || source.mode !== "live") {
    return <span className="shell__toolbar-spacer" />;
  }
  if (source.authentication !== "connected") {
    return <span className="chip chip--priority">CONNECTION ISSUE — eRegisters unavailable</span>;
  }
  if (source.mapping === "pending") {
    return (
      <span className="chip chip--attention">
        LIVE — authentication succeeded; malaria mapping pending
      </span>
    );
  }
  return <span className="chip">LIVE — eRegisters connected</span>;
}

function highPrioritySignalCount(
  section:
    | { availability: string; items: { code: string; count: number | null }[] }
    | undefined,
): number | null {
  if (!section || section.availability !== "available") return null;
  const total = section.items
    .filter((item) => item.code === "urgent" || item.code === "high")
    .reduce((sum, item) => sum + (item.count ?? 0), 0);
  return total > 0 ? total : null;
}

function formatScope(user: {
  has_national_scope: boolean;
  geography_scopes: { level: string; name: string }[];
  facility_scope_ids: string[];
}): string {
  if (user.facility_scope_ids.length > 0) {
    const count = user.facility_scope_ids.length;
    return count === 1 ? "1 facility" : `${count} facilities`;
  }
  if (user.has_national_scope) return "Uganda — national";
  if (user.geography_scopes.length === 0) return "No geography scope";
  return user.geography_scopes.map((scope) => scope.name).join(", ");
}

function NavIcon({ name }: { name: string }) {
  return (
    <svg className="shell__nav-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      {iconPath(name)}
    </svg>
  );
}

function iconPath(name: string) {
  switch (name) {
    case "overview":
      return <rect x="2" y="2" width="5" height="5" rx="1" />;
    case "signals":
      return <path d="M3 12V8h2v4H3zm4 0V4h2v8H7zm4 0V6h2v6h-2z" />;
    case "investigations":
      return <path d="M6.5 2h3l.5 2H14v10H2V4h4l.5-2zM8 7a2 2 0 1 1 0 4 2 2 0 0 1 0-4z" />;
    case "map":
      return <path d="M2 3.5 6 2l4 1.5L14 2v11.5L10 15 6 13.5 2 15z" />;
    case "analytics":
      return <path d="M2 13h12v1H2zm1-3h2v3H3zm4-4h2v7H7zm4-3h2v10h-2z" />;
    case "commodities":
      return <path d="M3 5h10l-1 8H4L3 5zm2-2h6l1 2H4l1-2z" />;
    case "quality":
      return <path d="M8 2 3 4.5v4c0 3 2.2 4.8 5 5.5 2.8-.7 5-2.5 5-5.5v-4z" />;
    case "facilities":
      return <path d="M3 14V6l5-3 5 3v8H9V9H7v5z" />;
    case "reports":
      return <path d="M4 2h6l4 4v8H4zm6 0v4h4" />;
    default:
      return <circle cx="8" cy="8" r="3" />;
  }
}
