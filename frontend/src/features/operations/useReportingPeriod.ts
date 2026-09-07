import { useSyncExternalStore } from "react";
import { monthPeriod, type PeriodSelection } from "../../design-system/period";

let selection: PeriodSelection | undefined;
const listeners = new Set<() => void>();
function current() { return selection ??= monthPeriod(-1); }
function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
function select(period: PeriodSelection) {
  selection = period;
  listeners.forEach((listener) => listener());
}

/** One reporting window follows the user between live workspaces. */
export function useReportingPeriod(): [PeriodSelection, (period: PeriodSelection) => void] {
  return [useSyncExternalStore(subscribe, current, current), select];
}
