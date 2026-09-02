/**
 * Top-level error boundary.
 *
 * A rendering fault must not leave a blank page, which reads as a system
 * failure with no explanation (blueprint appendix 145). The boundary shows what
 * happened and how to recover, and never renders the raw error to the user.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  failed: boolean;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Logged to the browser console for a developer; never surfaced to the
    // user, and never sent anywhere that could carry surveillance content.
    console.error("MARS interface error", error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.failed) {
      return this.props.children;
    }

    return (
      <main className="state state--unavailable" role="alert" style={{ margin: "3rem" }}>
        <span className="chip chip--priority">Interface error</span>
        <p className="state__title">The MARS interface could not render this view</p>
        <p className="state__description">
          This is a fault in the application, not in the underlying data. Reloading
          usually recovers it. If it persists, report it with the time it occurred.
        </p>
        <button type="button" className="button" onClick={() => window.location.reload()}>
          Reload
        </button>
      </main>
    );
  }
}
