import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { api, type ApiError } from "../../api/client";
import type { PeriodSelection } from "../../design-system/period";
import { UnavailableState } from "../../design-system/States";
import { RecurrenceDefinitionPanel } from "./RecurrenceDefinitionPanel";
import { RecurrenceResults } from "./RecurrenceResults";
import type { RecurrenceParameters } from "./types";
import { useRecurrenceAnalysis } from "./useRecurrenceAnalysis";
import "./recurrence.css";

export function RecurrenceWorkspace({ period, liveMode }: { period: PeriodSelection; liveMode: boolean }) {
  const [search, setSearch] = useSearchParams();
  const queryClient = useQueryClient();
  const analysis = useRecurrenceAnalysis(search.get("run"));
  const programme = useQuery({ queryKey: ["recurrence", "programme"], queryFn: api.recurrenceProgramme, retry: false });
  const definitions = useQuery({ queryKey: ["recurrence", "definitions"], queryFn: api.recurrenceDefinitions, retry: false });
  const save = useMutation({
    mutationFn: ({ name, parameters }: { name: string; parameters: RecurrenceParameters }) =>
      api.createRecurrenceDefinition({ name, parameters: { ...parameters } }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["recurrence", "definitions"] }),
  });
  const error = analysis.submitError ?? analysis.runError ?? definitions.error ?? save.error;
  useEffect(() => {
    if (analysis.runId && search.get("run") !== analysis.runId) {
      setSearch({ run: analysis.runId }, { replace: true });
    }
  }, [analysis.runId, search, setSearch]);
  return (
    <section className="recurrence-workspace" aria-label="Configurable recurrence analysis">
      <RecurrenceDefinitionPanel period={period} liveMode={liveMode} programme={programme.data}
        definitions={definitions.data ?? []} saving={save.isPending}
        busy={analysis.isSubmitting || analysis.run?.status === "queued" || analysis.run?.status === "running"}
        onSave={(name, parameters) => save.mutate({ name, parameters: { ...parameters } })}
        onApply={(request) => analysis.submit(request)} />
      {error ? <UnavailableState title="The analysis could not be started" description={error.message}
        requestId={(error as ApiError).requestId} onRetry={analysis.reset} /> : null}
      {analysis.run ? <RecurrenceResults run={analysis.run} /> : (
        <div className="recurrence-intro">
          <strong>Choose a definition, then apply it.</strong>
          <span>MARS will freeze the definition, evidence coverage and method version before calculating patients and aggregate views.</span>
        </div>
      )}
    </section>
  );
}
