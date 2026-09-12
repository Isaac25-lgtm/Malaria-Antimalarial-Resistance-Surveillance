import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Schemas } from "../../api/client";

export function useRecurrenceAnalysis(initialRunId: string | null = null) {
  const [runId, setRunId] = useState<string | null>(initialRunId);
  const submit = useMutation({
    mutationFn: (body: Schemas["RunCreate"]) => api.submitRecurrenceRun(body),
    onSuccess: (run) => setRunId(run.id),
  });
  const run = useQuery({
    queryKey: ["recurrence", "run", runId],
    queryFn: () => api.recurrenceRun(runId!),
    enabled: runId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 750 : false;
    },
    retry: false,
  });
  return {
    submit: (body: Schemas["RunCreate"]) => submit.mutate(body),
    isSubmitting: submit.isPending,
    submitError: submit.error,
    run: run.data,
    runError: run.error,
    runId,
    reset: () => setRunId(null),
  };
}
