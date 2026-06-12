"use client";

import { useMonitoringData } from "@/hooks/useMonitoringData";
import HealthBar from "./HealthBar";
import SystemSection from "./sections/SystemSection";
import DockerSection from "./sections/DockerSection";
import NetworkSection from "./sections/NetworkSection";
import ServicesSection from "./sections/ServicesSection";
import SecuritySection from "./sections/SecuritySection";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

export default function MonitoringDashboard() {
  const { snapshot, loading, error, stale } = useMonitoringData();

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-pir-text-muted text-label">
          Loading monitoring data...
        </div>
      </div>
    );
  }

  if (error && !snapshot) {
    return (
      <div className="flex items-center justify-center h-full">
        <ErrorAlert message={error} />
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <HealthBar snapshot={snapshot} stale={stale} />
      <div className="max-w-5xl mx-auto px-4 py-4 space-y-6">
        <SystemSection snapshot={snapshot} />
        <DockerSection snapshot={snapshot} />
        <NetworkSection snapshot={snapshot} />
        <ServicesSection snapshot={snapshot} />
        <SecuritySection securitySummary={snapshot?.security_summary} />
      </div>
    </div>
  );
}
