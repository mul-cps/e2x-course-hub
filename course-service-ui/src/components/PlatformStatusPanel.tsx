import { useEffect, useState } from "react";
import { config } from "../config";

type Check = {
  id: string;
  label: string;
  state: "pending" | "passed" | "blocked";
};
type Status = {
  version: 1;
  state: "qualification" | "limited-pilot" | "enabled" | "unavailable";
  title: string;
  detail: string;
  updatedAt: string;
  checks: Check[];
};
const unavailable: Status = {
  version: 1,
  state: "unavailable",
  title: "GPU group sharing: status unavailable",
  detail:
    "A current operator status is unavailable. Availability has not been confirmed.",
  updatedAt: "",
  checks: [],
};
const labels = {
  qualification: "Qualification in progress",
  "limited-pilot": "Limited pilot",
  enabled: "Enabled",
  unavailable: "Unavailable",
};

function validStatus(value: unknown): value is Status {
  if (!value || typeof value !== "object") return false;
  const status = value as Status;
  return (
    status.version === 1 &&
    Object.hasOwn(labels, status.state) &&
    typeof status.title === "string" &&
    typeof status.detail === "string" &&
    typeof status.updatedAt === "string" &&
    !Number.isNaN(Date.parse(status.updatedAt)) &&
    Array.isArray(status.checks) &&
    status.checks.length <= 32 &&
    status.checks.every(
      (check) =>
        check &&
        typeof check.id === "string" &&
        typeof check.label === "string" &&
        ["pending", "passed", "blocked"].includes(check.state),
    )
  );
}

export default function PlatformStatusPanel() {
  const [status, setStatus] = useState<Status>(unavailable);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    let next: ReturnType<typeof setTimeout> | undefined;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    let request: AbortController | undefined;
    async function refresh() {
      request = new AbortController();
      timeout = setTimeout(() => request?.abort(), 8000);
      try {
        const response = await fetch(`${config.apiUrl}/platform-status`, {
          credentials: "same-origin",
          cache: "no-store",
          redirect: "error",
          signal: request.signal,
        });
        if (!response.ok) throw new Error("Status unavailable");
        const value: unknown = await response.json();
        if (!validStatus(value)) throw new Error("Status unavailable");
        if (active) setStatus(value);
      } catch {
        if (active) setStatus(unavailable);
      } finally {
        clearTimeout(timeout);
        if (active) {
          setLoading(false);
          next = setTimeout(() => void refresh(), 20000);
        }
      }
    }
    void refresh();
    return () => {
      active = false;
      clearTimeout(next);
      clearTimeout(timeout);
      request?.abort();
    };
  }, []);
  const blockers = status.checks.filter((check) => check.state === "blocked");
  const remaining = status.checks.filter((check) => check.state === "pending");
  const passed = status.checks.filter((check) => check.state === "passed");
  return (
    <aside
      className="platform-status-panel"
      aria-label="GPU group sharing status"
      aria-live="polite"
    >
      <div className="platform-status-heading">
        <h2>{loading ? "GPU group sharing: checking status" : status.title}</h2>
        <span className={`status-pill platform-status-${status.state}`}>
          {loading ? "Checking" : labels[status.state]}
        </span>
      </div>
      {!loading && <p>{status.detail}</p>}
      {blockers.length > 0 && (
        <p>
          <strong>Blocked:</strong>{" "}
          {blockers.map((check) => check.label).join(" · ")}
        </p>
      )}
      {remaining.length > 0 && (
        <p>
          <strong>Remaining checks:</strong>{" "}
          {remaining.map((check) => check.label).join(" · ")}
        </p>
      )}
      {passed.length > 0 && (
        <p className="platform-status-meta">
          <strong>Passed:</strong>{" "}
          {passed.map((check) => check.label).join(" · ")}
        </p>
      )}
      {!loading && status.state !== "unavailable" && (
        <p className="platform-status-meta">
          Updated{" "}
          <time dateTime={status.updatedAt}>
            {new Date(status.updatedAt).toLocaleString(undefined, {
              timeZone: "UTC",
            })}{" "}
            UTC
          </time>
        </p>
      )}
    </aside>
  );
}
