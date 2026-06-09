// v1.0.0 - 2026-03-13 - Read-only RBAC permission matrix
"use client";

const FEATURES = [
  { name: "Dashboard", viewer: "read", operator: "read", admin: "full", super_admin: "full" },
  { name: "Tasks", viewer: "read", operator: "write", admin: "full", super_admin: "full" },
  { name: "Agents", viewer: "read", operator: "run", admin: "manage", super_admin: "manage" },
  { name: "Operators Metrics", viewer: "own", operator: "read", admin: "read+cost", super_admin: "read+cost" },
  { name: "CI Status", viewer: "read", operator: "read", admin: "bypass", super_admin: "bypass" },
  { name: "Users & Teams", viewer: "none", operator: "own team", admin: "manage", super_admin: "manage all" },
  { name: "SSO Config", viewer: "none", operator: "none", admin: "read", super_admin: "manage" },
  { name: "Activity Log", viewer: "own", operator: "own", admin: "all", super_admin: "all" },
  { name: "Settings", viewer: "none", operator: "none", admin: "manage", super_admin: "manage" },
];

const ROLES = ["viewer", "operator", "admin", "super_admin"] as const;

function accessLevel(access: string): "full" | "partial" | "none" {
  if (access === "none") return "none";
  if (["full", "manage", "manage all", "all", "bypass", "read+cost"].includes(access)) return "full";
  return "partial";
}

const LEVEL_CLASSES: Record<string, string> = {
  full: "bg-emerald-50 text-emerald-700 dark:bg-emerald-900/20 dark:text-emerald-400",
  partial: "bg-amber-50 text-amber-700 dark:bg-amber-900/20 dark:text-amber-400",
  none: "bg-red-50 text-red-600 dark:bg-red-900/20 dark:text-red-400",
};

export default function RBACMatrix() {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-pir">
            <th className="px-3 py-2 text-left font-medium text-pir-text-muted">Feature</th>
            {ROLES.map((role) => (
              <th key={role} className="px-3 py-2 text-center font-medium text-pir-text-muted">
                {role.replace("_", " ")}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {FEATURES.map((feat) => (
            <tr key={feat.name} className="border-b border-pir last:border-b-0">
              <td className="px-3 py-2 font-medium text-pir-text-primary">{feat.name}</td>
              {ROLES.map((role) => {
                const access = feat[role];
                const level = accessLevel(access);
                return (
                  <td key={role} className="px-3 py-2 text-center">
                    <span className={`inline-block px-2 py-0.5 rounded text-[10px] ${LEVEL_CLASSES[level]}`}>
                      {access}
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
