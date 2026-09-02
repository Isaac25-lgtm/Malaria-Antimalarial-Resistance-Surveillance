/**
 * Application entry point.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import "./design-system/tokens.css";
import "./design-system/base.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element #root was not found in index.html");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
