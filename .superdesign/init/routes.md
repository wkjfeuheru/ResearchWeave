# Routes
Existing dashboard: / -> autopilot-dashboard/src/main.tsx -> App.tsx. No browser router.
New target: /chat, /models, /skills in frontend/web; these do not exist yet.

## autopilot-dashboard/src/main.tsx
```
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

```
