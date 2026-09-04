/**
 * Typed HTTP client.
 *
 * Every response type comes from `schema.d.ts`, which is generated from the
 * backend's OpenAPI document. CI regenerates it and fails on any difference, so
 * a backend field rename breaks the build rather than the running interface.
 *
 * The frontend never computes an analytical value. It formats and renders what
 * the API returns; the API owns the statistic (blueprint appendix 161).
 */

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type ApiPaths = paths;

/** RFC 7807 problem document, as returned by every MARS error path. */
export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  code: string;
  detail?: string | null;
  instance?: string | null;
  request_id?: string | null;
  errors?: { field: string; message: string; code?: string | null }[] | null;
  documentation?: string | null;
}

/** An error carrying the server's problem document and request identifier. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly problem: ProblemDetail | null;
  readonly requestId: string | null;

  constructor(status: number, problem: ProblemDetail | null, fallback: string) {
    super(problem?.detail ?? problem?.title ?? fallback);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
    this.code = problem?.code ?? "unknown_error";
    this.requestId = problem?.request_id ?? null;
  }

  /** The caller is not authenticated, or the session has expired. */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  /** Authenticated, but lacking a permission, geography scope or sensitivity tier. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  /** A backing dependency is unavailable. Distinct from an empty result. */
  get isUnavailable(): boolean {
    return this.status === 503;
  }

  /** The permission or scope the server said was missing, when it named one. */
  get requirement(): string | null {
    if (!this.isForbidden) return null;
    const detail = this.problem?.detail ?? "";
    const match = detail.match(/requires:?\s*(.+?)\.?$/i);
    return match?.[1] ?? this.code;
  }
}

// Relative by default, so one build artefact runs in every environment.
const API_BASE: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "/api/v1";

/** In-memory only. A token is never written to localStorage or a cookie. */
let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  query?: Record<string, string | number | boolean | undefined | null>;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = `${API_BASE}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  }
  const serialised = params.toString();
  return serialised ? `${url}?${serialised}` : url;
}

/**
 * Perform a request and decode the response.
 *
 * A non-2xx response is turned into an {@link ApiError} carrying the problem
 * document, so callers branch on a stable machine code rather than parsing
 * prose.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (accessToken) {
    headers.Authorization = `Bearer ${accessToken}`;
  }

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch {
    // A network failure is not an empty result. Surface it as unavailable so
    // the interface shows the dependency state rather than an empty view.
    throw new ApiError(
      0,
      {
        type: "about:blank",
        title: "Network error",
        status: 0,
        code: "network_error",
        detail: "The MARS API could not be reached.",
      },
      "network error",
    );
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  const payload: unknown = text ? JSON.parse(text) : null;

  if (!response.ok) {
    const problem = isProblem(payload) ? payload : null;
    throw new ApiError(response.status, problem, response.statusText);
  }

  return payload as T;
}

function isProblem(value: unknown): value is ProblemDetail {
  return (
    typeof value === "object" &&
    value !== null &&
    "code" in value &&
    "status" in value &&
    "title" in value
  );
}

// ---------------------------------------------------------------------------
// Endpoint wrappers.
//
// One function per endpoint the shell uses, so a path string appears exactly
// once in the codebase.
// ---------------------------------------------------------------------------
export const api = {
  liveness: () => request<Schemas["LivenessResponse"]>("/health/live"),

  readiness: () => request<Schemas["ReadinessResponse"]>("/health/ready"),

  schemaState: () => request<Record<string, unknown>>("/health/schema"),

  version: () => request<Schemas["VersionResponse"]>("/meta/version"),

  permissionCatalogue: () => request<Record<string, unknown>>("/meta/permissions"),

  evidenceLanes: () => request<Record<string, unknown>>("/meta/evidence-lanes"),

  currentUser: () => request<Schemas["CurrentUserResponse"]>("/auth/me"),

  logout: () => request<void>("/auth/logout", { method: "POST" }),

  developmentUsers: () =>
    request<Schemas["DevelopmentUserSummary"][]>("/auth/dev/users"),

  developmentLogin: (username: string) =>
    request<Schemas["DevelopmentLoginResponse"]>("/auth/dev/login", {
      method: "POST",
      body: { username },
    }),

  geographyOverview: () =>
    request<Schemas["GeographyOverviewResponse"]>("/geography/overview"),

  geographyUnits: (query?: { level?: string; parent_id?: string; limit?: number }) =>
    request<Schemas["Page_GeographyUnitSummary_"]>("/geography/units", { query }),

  boundaryVersions: () =>
    request<Schemas["BoundaryVersionSummary"][]>("/geography/boundary-versions"),

  // -- Map delivery -------------------------------------------------------
  // Geometry is always the simplified copy; the API has no full-resolution
  // route to call, so the client cannot request one by accident.
  mapMetadata: () => request<Schemas["MapMetadataResponse"]>("/geography/map/metadata"),

  mapFeatures: (query: {
    level: string;
    parent_id?: string;
    within_id?: string;
    limit?: number;
  }) => request<Schemas["MapFeatureCollection"]>("/geography/map/features", { query }),

  mapContext: (query: { level: string; limit?: number }) =>
    request<Schemas["MapFeatureCollection"]>("/geography/map/context", { query }),

  nationalGeography: () =>
    request<Schemas["NationalGeographyResponse"]>("/geography/national"),

  unitGeometry: (unitId: string) =>
    request<Schemas["MapFeature"]>(`/geography/units/${unitId}/geometry`),

  unitBounds: (unitId: string) =>
    request<Schemas["BoundingBoxModel"]>(`/geography/units/${unitId}/bounds`),

  unitBreadcrumbs: (unitId: string) =>
    request<Schemas["GeographyBreadcrumbsResponse"]>(
      `/geography/units/${unitId}/breadcrumbs`,
    ),

  unitChildren: (unitId: string) =>
    request<Schemas["GeographyUnitSummary"][]>(`/geography/units/${unitId}/children`),

  district: (code: string) =>
    request<Schemas["GeographyUnitDetail"]>(`/geography/districts/${code}`),

  organisationUnits: (query?: { unit_type?: string; limit?: number }) =>
    request<Schemas["Page_OrganisationUnitSummary_"]>("/organisation-units", { query }),

  facilities: (query?: { district_id?: string; limit?: number }) =>
    request<Schemas["Page_FacilitySummary_"]>("/facilities", { query }),

  configurationKeys: () =>
    request<Schemas["ConfigurationKeySummary"][]>("/governance/configuration-keys"),

  methods: () => request<Schemas["MethodDefinitionSummary"][]>("/governance/methods"),

  // -- Surveillance command centre (Prompt 23) ------------------------------
  //
  // Each returns records, not numbers. A measure carries its own period,
  // scope, source, method version and availability status, so the interface
  // renders "not configured" from the server's own words rather than
  // inventing a zero.
  nationalSummary: (query: { period_start: string; period_end: string }) =>
    request<Schemas["SurveillanceMeasure"][]>("/surveillance/national/summary", { query }),

  districtSummary: (unitId: string, query: { period_start: string; period_end: string }) =>
    request<Schemas["SurveillanceMeasure"][]>(
      `/surveillance/districts/${encodeURIComponent(unitId)}/summary`,
      { query },
    ),

  facilitySummary: (facilityId: string, query: { period_start: string; period_end: string }) =>
    request<Schemas["SurveillanceMeasure"][]>(
      `/surveillance/facilities/${encodeURIComponent(facilityId)}/summary`,
      { query },
    ),

  districtFacilities: (
    unitId: string,
    query: { period_start: string; period_end: string; limit?: number },
  ) =>
    request<Schemas["FacilityContribution"][]>(
      `/surveillance/districts/${encodeURIComponent(unitId)}/facilities`,
      { query },
    ),

  geographyUnit: (unitId: string) =>
    request<Schemas["GeographyUnitSummary"]>(`/geography/units/${encodeURIComponent(unitId)}`),

  facility: (facilityId: string) =>
    request<Schemas["FacilityDetail"]>(`/facilities/${encodeURIComponent(facilityId)}`),

  priorityDistricts: (query: {
    period_start: string;
    period_end: string;
    limit?: number;
  }) => request<Schemas["PriorityDistrict"][]>("/surveillance/priority-districts", { query }),

  surveillanceProvenance: (query: { period_start: string; period_end: string }) =>
    request<Schemas["SurveillanceProvenance"]>("/surveillance/provenance", { query }),

  overview: (query: { period_start: string; period_end: string }) =>
    request<Schemas["OverviewSnapshot"]>("/surveillance/overview", { query }),

  analyticalResults: (
    kind:
      | "recurrence"
      | "testing"
      | "treatment"
      | "baseline"
      | "anomaly"
      | "hotspot"
      | "cluster",
    query: { period_from?: string; period_to?: string; limit?: number },
  ) =>
    request<Schemas["AnalyticalRecordSummary"][]>(
      `/analytics/results/${kind}`,
      { query },
    ),

  commodityAlerts: (query: { period_from?: string; period_to?: string; limit?: number }) =>
    request<Schemas["AnalyticalRecordSummary"][]>("/analytics/commodity-alerts", { query }),

  signals: (query: {
    period_from?: string;
    period_to?: string;
    active_only?: boolean;
    limit?: number;
  }) => request<Schemas["SignalSummary"][]>("/signals", { query }),

  signal: (signalId: string) =>
    request<Schemas["SignalSummary"]>(`/signals/${encodeURIComponent(signalId)}`),

  signalExplanation: (signalId: string) =>
    request<Schemas["SignalExplanationSummary"]>(
      `/signals/${encodeURIComponent(signalId)}/explanation`,
    ),

  report: (
    product: "national_brief" | "district_brief",
    query: { period_start: string; period_end: string; geography_unit_id?: string },
  ) => request<Schemas["GeneratedReport"]>(`/reports/${product}`, { query }),

  // -- Investigations (Prompt 26) -------------------------------------------
  investigationQueues: () =>
    request<{
      queues: string[];
      overdue: { available: boolean; missing_configuration: string[]; detail: string | null };
    }>("/investigations/queues"),

  investigationQueue: (name: string, query?: { limit?: number }) =>
    request<Schemas["InvestigationQueueEntry"][]>(
      `/investigations/queues/${encodeURIComponent(name)}`,
      { query },
    ),

  investigation: (investigationId: string) =>
    request<Schemas["InvestigationDetail"]>(
      `/investigations/${encodeURIComponent(investigationId)}`,
    ),
};
