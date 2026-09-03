/**
 * The five states a data region can be in.
 *
 * Loading, forbidden, unavailable, empty and populated are genuinely different
 * answers, and the interface renders each differently. Collapsing "you may not
 * see this" and "there is nothing here" into one blank panel is how a
 * scoped-out district becomes an apparently healthy one, and how a failed
 * request becomes an apparent absence of malaria.
 *
 * Shared by every analytical screen so the distinction cannot drift apart
 * between them.
 */

import type { ReactNode } from "react";

import { ApiError } from "../api/client";
import { EmptyState, LoadingState, NoDataState, UnavailableState } from "./States";

export interface QueryLike<T> {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  data: T[] | undefined;
}

interface QueryRegionProps<T> {
  query: QueryLike<T>;
  loadingLabel: string;
  emptyTitle: string;
  emptyDescription: string;
  children: (rows: T[]) => ReactNode;
}

export function QueryRegion<T>({
  query,
  loadingLabel,
  emptyTitle,
  emptyDescription,
  children,
}: QueryRegionProps<T>) {
  if (query.isPending) {
    return <LoadingState label={loadingLabel} />;
  }

  if (query.isError) {
    const error = query.error;
    if (error instanceof ApiError && error.isForbidden) {
      return (
        <NoDataState
          title="Outside your authorised scope"
          description="Your account is not authorised for this information. This is a statement about permissions, not about malaria."
          awaiting={error.requirement ?? undefined}
        />
      );
    }
    return (
      <UnavailableState
        title="This section could not be loaded"
        description={
          error instanceof ApiError
            ? error.message
            : "The server did not answer. These figures are not zero; they are unknown."
        }
      />
    );
  }

  const rows = query.data ?? [];
  if (rows.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} />;
  }

  return <>{children(rows)}</>;
}
