import { useEffect, useMemo, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useAuth } from "../../auth/context";
import type { PeriodSelection } from "../../design-system/period";

type Range = { period_start: string; period_end: string };
const jobKey = (range?: Range) => ["live", "dashboard-job", range ?? "latest"] as const;

export function useLiveDashboard(period?: PeriodSelection, ready = true, autoSync = false) {
  const { user } = useAuth();
  const liveMode = user?.source_status?.mode === "live";
  const client = useQueryClient();
  const range = useMemo(() => period ? { period_start: period.start, period_end: period.end } : undefined, [period]);
  const key = ["live", "dashboard", range ?? "latest"] as const;
  const attempted = useRef<string | null>(null);
  const job = useQuery({
    queryKey: jobKey(range),
    queryFn: () => api.latestLiveDashboardJob(range),
    enabled: liveMode && ready,
    retry: false,
    refetchInterval: 3000,
  });
  const running = job.data?.status === "queued" || job.data?.status === "running";
  const query = useQuery({
    queryKey: key,
    queryFn: () => api.latestLiveDashboard(range),
    enabled: liveMode && ready,
    retry: false,
  });
  const sync = useMutation({
    mutationFn: (selected: Range) => api.submitLiveDashboardJob(selected),
    onSuccess: (receipt, selected) => client.setQueryData(jobKey(selected), receipt),
  });
  const status = job.data?.status;
  const jobId = job.data?.id;
  useEffect(() => {
    if (status && status !== "queued" && status !== "running") {
      void client.invalidateQueries({ queryKey: ["live", "dashboard"] });
      void client.invalidateQueries({ queryKey: ["live", "patient"] });
    }
  }, [status, jobId, client]);
  const periodKey = period ? `${period.start}:${period.end}` : null;
  useEffect(() => {
    if (autoSync && ready && liveMode && periodKey && range && query.isSuccess && job.isSuccess &&
        ((!query.data && !job.data) || status === "interrupted") &&
        !sync.isPending && attempted.current !== periodKey) {
      attempted.current = periodKey;
      sync.mutate(range);
    }
  }, [autoSync, ready, liveMode, periodKey, range, query.isSuccess, query.data,
    job.isSuccess, job.data, status, sync]);
  const failed = status === "failed" || status === "interrupted";
  const jobError = failed ? new Error(
    job.data?.error_code === "local_configuration"
      ? "Synchronization requires server configuration. Your saved snapshot is unchanged."
      : status === "interrupted"
        ? "Synchronization was interrupted. Sign in, discover scope, and resume the saved work."
        : "The latest refresh failed. Previously saved figures remain available. Retry to resume."
  ) : null;
  const refresh = () => range ? sync.mutate(range) : void query.refetch();
  const refreshError = query.error ?? sync.error ?? job.error ?? jobError;
  return { ...query, liveMode, job: job.data, refreshError,
    error: query.data ? null : refreshError,
    isLoading: query.isLoading || (!query.data && (running || sync.isPending)),
    refresh,
    synchronization: { isPending: running || sync.isPending,
      isError: failed || sync.isError, mutate: refresh },
  };
}
