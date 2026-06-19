// monitoring-utils.ts — logica condivisa per derivare status da MonitoringSnapshot

/**
 * Returns true if a systemd service status string indicates active/running.
 * API returns: 'active', 'running', 'stopped', 'failed', 'unknown'
 */
export function isSystemdServiceRunning(status: string): boolean {
  return status === "active" || status === "running";
}

/**
 * Returns true if a Docker container status string indicates running.
 * API returns free-text: 'Up 3 hours', 'Exited (1) 2 minutes ago', 'running', etc.
 */
export function isDockerContainerRunning(status: string): boolean {
  return status.toLowerCase().startsWith("up") || status === "running";
}

/**
 * Returns true if alerts contain a critical/warning alert for the given metric.
 * AlertInfo uses 'metric' field (NOT 'resource').
 */
export function hasSystemAlert(
  alerts: Array<{ metric: string; level: string }>,
  metrics: string[]
): boolean {
  return alerts.some(
    (a) => metrics.includes(a.metric) && (a.level === "critical" || a.level === "warning")
  );
}
