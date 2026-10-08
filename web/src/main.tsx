import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider, Navigate } from "react-router-dom";
import { App } from "./App";
import { TokenGate } from "./TokenGate";
import { Dashboard } from "./screens/Dashboard";
import { ScheduleView } from "./screens/ScheduleView";
import { FinalsView } from "./screens/FinalsView";
import { ImportView } from "./screens/ImportView";
import { ConflictsView } from "./screens/ConflictsView";
import { DataBrowser } from "./screens/DataBrowser";
import { ProjectionsView } from "./screens/ProjectionsView";
import "./index.css";

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: "schedules", element: <ScheduleView /> },
      { path: "schedules/:scheduleId", element: <ScheduleView /> },
      { path: "finals", element: <FinalsView /> },
      { path: "finals/:scheduleId", element: <FinalsView /> },
      { path: "conflicts", element: <ConflictsView /> },
      { path: "projections", element: <ProjectionsView /> },
      { path: "data", element: <DataBrowser /> },
      { path: "import", element: <ImportView /> },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <TokenGate>
      <RouterProvider router={router} />
    </TokenGate>
  </React.StrictMode>
);
