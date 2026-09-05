import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useAuth } from "../../auth/context";
import type { PeriodSelection } from "../../design-system/period";

export function useLiveDashboard(period?: PeriodSelection) {
  const { user } = useAuth();
  const liveMode = user?.source_status?.mode === "live";
  const client = useQueryClient();
  const range = period ? { period_start: period.start, period_end: period.end } : undefined;
  const key = ["live", "dashboard", range ?? "latest"] as const;
  const attempted = useRef<string | null>(null);
  const query = useQuery({
    queryKey: key,
    queryFn: () => api.latestLiveDashboard(range),
    enabled: liveMode,
    retry: false,
  });
  const sync = useMutation({
    mutationFn: () => {
      if (!range) throw new Error("Choose a reporting period first");
      return api.synchronizeLiveDashboard(range);
    },
    onSuccess: (snapshot) => {
      client.setQueryData(key, snapshot);
      client.setQueryData(["live", "dashboard", "latest"], snapshot);
    },
  });
  const periodKey = period ? `${period.start}:${period.end}` : null;
  useEffect(() => {
    if (liveMode && periodKey && query.isSuccess && !query.data && !sync.isPending && attempted.current !== periodKey) {
      attempted.current = periodKey;
      sync.mutate();
    }
  }, [liveMode, periodKey, query.isSuccess, query.data, sync]);
  return { ...query, liveMode, error: query.error ?? sync.error,
    isLoading: query.isLoading || sync.isPending,
    refresh: () => range ? sync.mutate() : void query.refetch(),
  };
}
