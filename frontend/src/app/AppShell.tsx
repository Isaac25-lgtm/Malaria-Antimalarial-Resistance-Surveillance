/**
 * Application shell: app bar, navigation and content region.
 *
 * Deliberately restrained. This is a workspace frame, not a dashboard - there
 * are no fabricated metrics anywhere in it, because MARS has no surveillance
 * data yet and inventing some would be the fastest way to lose a Ministry
 * audience's trust.
 */

import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../auth/context";
import "./app-shell.css";

interface NavigationItem {
  to: string;
  label: string;
  /** Permission required to see the item. Undefined means always visible. */
  permission?: string;
  /** Shown when the destination has no data yet, so the state is not a surprise. */
  note?: string;
}

const PRIMARY_NAVIGATION: NavigationItem[] = [
  {
    to: "/command-centre",
    label: "Command centre",
    permission: "surveillance:view_aggregate",
  },
  { to: "/status", label: "System status" },
  {
    to: "/national",
    label: "National map",
    permission: "geography:view",
  },
  {
    to: "/geography",
    label: "Geography",
    permission: "geography:view",
  },
  {
    to: "/organisation",
    label: "Organisation",
    permission: "organisation:view",
    note: "No units defined",
  },
  {
    to: "/facilities",
    label: "Facilities",
    permission: "facility:view",
    note: "No facility master",
  },
  {
    to: "/governance",
    label: "Governance",
    permission: "configuration:view",
    note: "Registries empty",
  },
];

export function AppShell() {
  const { user, signOut } = useAuth();

  const visibleItems = PRIMARY_NAVIGATION.filter(
    (item) => !item.permission || (user?.permissions.includes(item.permission) ?? false),
  );

  return (
    <div className="shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>

      <header className="shell__bar">
        <div className="shell__identity">
          <span className="shell__mark" aria-hidden="true">
            M
          </span>
          <span className="shell__wordmark">MARS</span>
          <span className="shell__divider" aria-hidden="true" />
          <span className="shell__context">Malaria Antimalarial Resistance Surveillance</span>
        </div>

        <div className="shell__bar-end">
          {user?.is_synthetic ? (
            <span className="chip chip--attention" title="Synthetic development session">
              Development session
            </span>
          ) : null}
          {user ? (
            <div className="shell__account">
              <div className="shell__account-name">{user.display_name}</div>
              <div className="shell__account-scope mono">
                {formatScope(user)}
              </div>
            </div>
          ) : null}
          <button type="button" className="button" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>

      <div className="shell__body">
        <nav className="shell__nav" aria-label="Primary">
          <ul>
            {visibleItems.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  className={({ isActive }) =>
                    isActive ? "shell__nav-link shell__nav-link--active" : "shell__nav-link"
                  }
                >
                  <span>{item.label}</span>
                  {item.note ? (
                    <span className="shell__nav-note">{item.note}</span>
                  ) : null}
                </NavLink>
              </li>
            ))}
          </ul>

          <div className="shell__nav-footer">
            <NavLink to="/profile" className="shell__nav-link">
              Your access
            </NavLink>
          </div>
        </nav>

        <main id="main-content" className="shell__main" tabIndex={-1}>
          <Outlet />
        </main>
      </div>

      {/*
        Permanent, non-dismissible. Blueprint section 008: the wording of the
        interface is part of the analytical safety system, and this statement
        must be present wherever surveillance output is shown.
      */}
      <footer className="shell__boundary" role="contentinfo">
        MARS signals indicate patterns requiring investigation. They do not confirm
        antimalarial resistance.
      </footer>
    </div>
  );
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
  if (user.has_national_scope) return "Uganda - national";
  if (user.geography_scopes.length === 0) return "No geography scope";
  return user.geography_scopes.map((scope) => scope.name).join(", ");
}
