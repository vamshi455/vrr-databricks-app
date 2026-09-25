import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";
import { initScale } from "./ui-scale";

// Before render, not inside an effect: the root font-size decides the size of every
// element on the page, so applying it after mount would paint the whole app at the
// fallback size and reflow it on every single load.
initScale();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
