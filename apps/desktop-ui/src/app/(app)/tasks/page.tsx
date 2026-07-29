"use client";

import TaskSurface from "@/components/tasks/TaskSurface";

// The shared page redirected to /triage/ outside local mode. This product is
// the local one and does not ship /triage/, so the branch could only ever send
// the user to a missing page.
export default function TasksPage() {
  return <TaskSurface />;
}
