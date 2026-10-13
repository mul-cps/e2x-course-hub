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
  availability?: Availability;
};
type Availability = {
  state: "ready" | "pending" | "unavailable";
  revision: string | null;
  policyHash: string | null;
  approvedProfiles: string[];
  approvedWorkspaceCount: number;
  checkedAt: string;
};
const unavailableAccess: Availability = {
  state: "unavailable",
  revision: null,
  policyHash: null,
  approvedProfiles: [],
  approvedWorkspaceCount: 0,
  checkedAt: "",
};
const unavailable: Status = {
  version: 1,
  state: "unavailable",
  title: "Production qualification report: unavailable",
  detail:
    "A current operator qualification report is unavailable. Broader production qualification remains unconfirmed.",
  updatedAt: "",
  checks: [],
  availability: unavailableAccess,
};
const labels = {
  qualification: "Qualification in progress",
  "limited-pilot": "Limited pilot",
  enabled: "Enabled",
  unavailable: "Unknown",
};
const utcTimestamp = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
function freshTimestamp(value: unknown, maxAge: number): value is string {
  if (typeof value !== "string" || !utcTimestamp.test(value)) return false;
  const timestamp = Date.parse(value);
  return (
    !Number.isNaN(timestamp) &&
    new Date(timestamp).toISOString().slice(0, 19) === value.slice(0, 19) &&
    Date.now() - timestamp <= maxAge &&
    timestamp - Date.now() <= 30000
  );
}
function boundedText(value: unknown, max: number): value is string {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    value.length <= max &&
    Array.from(value).every(
      (char) => char.charCodeAt(0) >= 32 || char === "\n" || char === "\t",
    )
  );
}

function validStatus(value: unknown): value is Status {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const status = value as Status;
  return (
    Object.keys(status).every((key) =>
      [
        "version",
        "state",
        "title",
        "detail",
        "updatedAt",
        "checks",
        "availability",
      ].includes(key),
    ) &&
    status.version === 1 &&
    Object.hasOwn(labels, status.state) &&
    boundedText(status.title, 200) &&
    boundedText(status.detail, 2000) &&
    freshTimestamp(status.updatedAt, 15 * 60 * 1000) &&
    Array.isArray(status.checks) &&
    status.checks.length <= 32 &&
    status.checks.every(
      (check) =>
        check &&
        typeof check.id === "string" &&
        /^[a-z][a-z0-9-]{0,63}$/.test(check.id) &&
        boundedText(check.label, 160) &&
        Object.keys(check).length === 3 &&
        ["pending", "passed", "blocked"].includes(check.state),
    ) &&
    new Set(status.checks.map((check) => check.id)).size ===
      status.checks.length &&
    (status.state !== "enabled" ||
      (status.checks.length > 0 &&
        status.checks.every((check) => check.state === "passed")))
  );
}
function validAvailability(value: unknown): value is Availability {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const access = value as Availability;
  if (
    Object.keys(access).length !== 6 ||
    !Object.keys(access).every((key) =>
      [
        "state",
        "revision",
        "policyHash",
        "approvedProfiles",
        "approvedWorkspaceCount",
        "checkedAt",
      ].includes(key),
    ) ||
    !["ready", "pending", "unavailable"].includes(access.state) ||
    !freshTimestamp(access.checkedAt, 30000) ||
    !Number.isInteger(access.approvedWorkspaceCount) ||
    access.approvedWorkspaceCount < 0 ||
    access.approvedWorkspaceCount > 32 ||
    !Array.isArray(access.approvedProfiles) ||
    access.approvedProfiles.length > 32 ||
    !access.approvedProfiles.every(
      (profile) =>
        typeof profile === "string" &&
        /^interactive-shared-[1-9][0-9]{0,2}$/.test(profile),
    ) ||
    new Set(access.approvedProfiles).size !== access.approvedProfiles.length
  )
    return false;
  if (access.state === "unavailable")
    return (
      access.revision === null &&
      access.policyHash === null &&
      access.approvedWorkspaceCount === 0 &&
      access.approvedProfiles.length === 0
    );
  return (
    typeof access.revision === "string" &&
    /^sha256:[a-f0-9]{64}$/.test(access.revision) &&
    typeof access.policyHash === "string" &&
    /^[a-f0-9]{64}$/.test(access.policyHash) &&
    (access.approvedWorkspaceCount === 0
      ? access.approvedProfiles.length === 0
      : access.approvedProfiles.length > 0 &&
        access.approvedProfiles.length <= access.approvedWorkspaceCount)
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
        const access =
          value && typeof value === "object" && !Array.isArray(value)
            ? (value as Status).availability
            : undefined;
        if (active)
          setStatus({
            ...(validStatus(value) ? value : unavailable),
            availability: validAvailability(access)
              ? access
              : unavailableAccess,
          });
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
  const access = status.availability ?? unavailableAccess;
  const accessReady =
    access.state === "ready" && access.approvedWorkspaceCount > 0;
  const accessTitle = loading
    ? "GPU access: checking approval"
    : accessReady
      ? "GPU access: approved workspaces ready"
      : access.state === "pending"
        ? "GPU access: approval activation pending"
        : access.state === "ready"
          ? "GPU access: no approved workspaces"
          : "GPU access: status unavailable";
  return (
    <aside
      className="platform-status-panel"
      aria-label="GPU group sharing status"
      aria-live="polite"
    >
      <div className="platform-status-heading">
        <h2>{accessTitle}</h2>
        <span className={`status-pill ${accessReady ? "good" : "muted"}`}>
          {loading
            ? "Checking"
            : accessReady
              ? "Approved workspaces"
              : access.state === "pending"
                ? "Pending"
                : "Unverified"}
        </span>
      </div>
      {!loading && (
        <>
          <p>
            {accessReady
              ? `${access.approvedWorkspaceCount} approved workspace${access.approvedWorkspaceCount === 1 ? "" : "s"} in this console. Approved profiles: ${access.approvedProfiles.join(", ")}.`
              : access.state === "pending"
                ? "Approval activation is waiting for current Hubs, gateway and GPU node readbacks."
                : access.state === "ready"
                  ? "No workspaces in this console currently have a GPU approval."
                  : "Live workspace approvals could not be verified."}
          </p>
          <p>
            Only approved GPU profiles may Start. Start still checks member
            grants, course ceilings and global reservations. Broader production
            qualification is reported separately below.
          </p>
        </>
      )}
      <section role="group" aria-label="Production qualification report">
        <div className="platform-status-heading">
          <h3>Production qualification report</h3>
          <span className={`status-pill platform-status-${status.state}`}>
            {loading ? "Checking" : labels[status.state]}
          </span>
        </div>
        {!loading && (
          <>
            {status.state !== "unavailable" && (
              <p>
                <strong>{status.title}</strong>
              </p>
            )}
            <p>{status.detail}</p>
          </>
        )}
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
      </section>
    </aside>
  );
}
