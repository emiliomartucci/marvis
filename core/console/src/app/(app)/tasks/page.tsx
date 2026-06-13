"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import TaskSurface from "@/components/tasks/TaskSurface";

function isLocalMode(): boolean {
  const value = process.env.NEXT_PUBLIC_LOCAL_MODE;
  return value === "1" || value === "true";
}

export default function TasksPage() {
  const router = useRouter();

  useEffect(() => {
    if (!isLocalMode()) router.replace("/triage/");
  }, [router]);

  if (!isLocalMode()) return null;
  return <TaskSurface />;
}
