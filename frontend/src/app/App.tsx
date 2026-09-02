/**
 * Application root: providers, router and route table.
 *
 * The route table is the map of what MARS will become. Routes for phases that
 * do not exist yet are deliberately absent rather than stubbed - a navigation
 * item leading to a fabricated dashboard would be worse than one that is not
 * there.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { ApiError } from "../api/client";
import { AuthProvider } from "../auth/AuthProvider";
import { useAuth } from "../auth/context";
import { RedirectIfAuthenticated, RequireAuth } from "../auth/RouteGuard";
import { AppShell } from "./AppShell";
import { ErrorBoundary } from "./ErrorBoundary";
import { NotFoundView } from "./NotFoundView";
import { SignInView } from "../features/auth/SignInView";
import { AccessProfileView } from "../features/profile/AccessProfileView";
import {
  FacilitiesView,
  GeographyView,
  GovernanceView,
  OrganisationView,
} from "../features/reference/ReferenceViews";
import { SystemStatusView } from "../features/status/SystemStatusView";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Authorisation failures are decisions, not transient faults. Retrying
      // one wastes time and floods the audit trail with repeated denials.
      retry: (failureCount, error) => {
        if (error instanceof ApiError) {
          if (error.status >= 400 && error.status < 500) return false;
          return failureCount < 2;
        }
        return failureCount < 2;
      },
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
});

export function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AuthProvider>
            <AppRoutes />
          </AuthProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}

function AppRoutes() {
  return (
    <Routes>
      <Route
        path="/sign-in"
        element={
          <RedirectIfAuthenticated>
            <SignInView />
          </RedirectIfAuthenticated>
        }
      />

      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<LandingRedirect />} />

        <Route path="status" element={<SystemStatusView />} />
        <Route path="profile" element={<AccessProfileView />} />

        <Route
          path="geography"
          element={
            <RequireAuth permissions={["geography:view"]}>
              <GeographyView />
            </RequireAuth>
          }
        />
        <Route
          path="organisation"
          element={
            <RequireAuth permissions={["organisation:view"]}>
              <OrganisationView />
            </RequireAuth>
          }
        />
        <Route
          path="facilities"
          element={
            <RequireAuth permissions={["facility:view"]}>
              <FacilitiesView />
            </RequireAuth>
          }
        />
        <Route
          path="governance"
          element={
            <RequireAuth permissions={["configuration:view"]}>
              <GovernanceView />
            </RequireAuth>
          }
        />

        {/*
          Surveillance workspaces resolve to the reference views they can
          actually populate. The national command centre, district and facility
          workspaces, signal register and action centre arrive with the phases
          that give them something to show.
        */}
        <Route path="national" element={<Navigate to="/geography" replace />} />
        <Route path="districts/:districtCode" element={<Navigate to="/geography" replace />} />
        <Route path="facilities/:facilityId" element={<Navigate to="/facilities" replace />} />

        <Route path="*" element={<NotFoundView />} />
      </Route>
    </Routes>
  );
}

/** Send the signed-in user to the highest geography they are scoped to. */
function LandingRedirect() {
  const { landingPath } = useAuth();
  const target = landingPath === "/national" ? "/status" : landingPath;
  return <Navigate to={target} replace />;
}
