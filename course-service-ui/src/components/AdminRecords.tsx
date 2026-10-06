import { useEffect, useRef, useState } from "react";
import {
  Plus,
  Search,
  RefreshCw,
  X,
  Pencil,
  Trash2,
  AlertCircle,
  CheckCircle2,
  FolderOpen,
} from "lucide-react";
import { requests } from "../api/client";
import { config } from "../config";

type Row = Record<string, unknown>;
type Field = {
  key: string;
  label: string;
  required?: boolean;
  type?: "number" | "list" | "textarea" | "date";
  options?: string[];
  reference?: string;
};
type Kind =
  | "courses"
  | "terms"
  | "memberships"
  | "groups"
  | "groupings"
  | "projects"
  | "compute"
  | "workspaces"
  | "assignments"
  | "audit";
const id: Field = { key: "id", label: "Identifier", required: true };
const course: Field = {
  key: "course_id",
  label: "Course",
  required: true,
  reference: "courses",
};
const term: Field = { key: "term_id", label: "Term", reference: "terms" };
const name: Field = { key: "name", label: "Name", required: true };
const fields: Record<Kind, Field[]> = {
  courses: [
    id,
    name,
    { key: "description", label: "Description", type: "textarea" },
  ],
  terms: [id, course, name],
  memberships: [
    id,
    course,
    term,
    { key: "person_id", label: "Hub username", required: true },
    { key: "role", label: "Role", options: ["student", "instructor", "ta"] },
    { key: "group_id", label: "Group", reference: "groups" },
    { key: "starts", label: "Valid from (UTC)", type: "date" },
    { key: "expires", label: "Expires (UTC)", type: "date" },
  ],
  groups: [
    id,
    course,
    term,
    name,
    {
      key: "members",
      label: "Members",
      type: "list",
      reference: "memberships",
    },
  ],
  groupings: [
    id,
    course,
    term,
    name,
    { key: "group_ids", label: "Groups", type: "list", reference: "groups" },
  ],
  projects: [
    id,
    name,
    { key: "description", label: "Description", type: "textarea" },
  ],
  compute: [
    id,
    { key: "person", label: "Canonical person ID", required: true },
    {
      key: "profiles",
      label: "Allowed profiles",
      type: "list",
      reference: "profiles",
    },
    { key: "projects", label: "Projects", type: "list", reference: "projects" },
    {
      key: "allowance",
      label: "Interactive allowance",
      type: "number",
      required: true,
    },
    { key: "starts", label: "Valid from (UTC)", type: "date" },
    { key: "expires", label: "Expires (UTC)", type: "date", required: true },
    {
      key: "reason",
      label: "Reason for access",
      type: "textarea",
      required: true,
    },
  ],
  assignments: [
    id,
    course,
    { ...term, required: true },
    {
      key: "mode",
      label: "Allocation method",
      options: ["random", "csv", "manual"],
    },
    { key: "group_size", label: "Students per group", type: "number" },
    { key: "seed", label: "Random seed" },
  ],
  workspaces: [
    id,
    course,
    { ...term, required: true },
    { key: "group_id", label: "Group", required: true, reference: "groups" },
    {
      key: "profile",
      label: "Compute profile",
      required: true,
      reference: "profiles",
    },
  ],
  audit: [],
};
const titles: Record<Kind, string> = {
  courses: "Courses",
  terms: "Terms",
  memberships: "Members",
  groups: "Groups",
  groupings: "Groupings",
  projects: "Projects",
  compute: "Compute access",
  workspaces: "Shared workspaces",
  assignments: "Assignments",
  audit: "Audit log",
};
const descriptions: Record<Kind, string> = {
  courses:
    "Manage local courses, teaching terms and student access in one place.",
  terms: "Organize teaching periods within your courses.",
  memberships: "Manage course membership, roles and time-bounded access.",
  groups: "Create collaboration groups from course members.",
  groupings: "Collect course groups for teaching and assignments.",
  projects: "Organize research and teaching projects.",
  compute:
    "Inspect permitted profiles and manage time-bounded grants through shared policy.",
  workspaces:
    "Manage shared notebooks and their lifecycle. Files survive shutdown.",
  assignments:
    "Allocate student groups using a reproducible seed, CSV or manual allocation.",
  audit:
    "Review who changed records, when and whether the operation succeeded.",
};
const display = (v: unknown): string =>
  v == null || v === ""
    ? "—"
    : Array.isArray(v)
      ? v.map(display).join(", ")
      : typeof v === "object"
        ? Object.entries(v as Row)
            .map(([k, val]) => `${k}: ${display(val)}`)
            .join(" · ")
        : String(v);
function rowsFrom(result: unknown): Row[] {
  const d = result as Row;
  const content = d.records ?? d.grants ?? d.profiles;
  if (Array.isArray(content)) return content;
  if (content && typeof content === "object")
    return Object.entries(content).map(([key, value]) =>
      typeof value === "object" && value !== null
        ? { id: key, ...(value as Row) }
        : { id: key, value },
    );
  return [];
}
export default function AdminRecords({ kind }: { kind: Kind }) {
  const [records, setRecords] = useState<Row[]>([]),
    [refs, setRefs] = useState<Record<string, Row[]>>({});
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [search, setSearch] = useState(""),
    [person, setPerson] = useState(""),
    [view, setView] = useState<"profiles" | "grants">("profiles");
  const [draft, setDraft] = useState<Row | null>(null),
    [editing, setEditing] = useState(false),
    [allocation, setAllocation] = useState("");
  const dialogRef = useRef<HTMLElement>(null);
  const [removeMember, setRemoveMember] = useState<Row | null>(null),
    [membership, setMembership] = useState("");
  const [confirmation, setConfirmation] = useState<{
    message: string;
    run: () => Promise<void>;
  } | null>(null);
  const dialogOpen = Boolean(draft || confirmation || removeMember);
  useEffect(() => {
    if (!dialogOpen) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    const focusable = () =>
      Array.from(
        dialog?.querySelectorAll<HTMLElement>(
          "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea, a[href]",
        ) ?? [],
      );
    focusable()[0]?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) {
        setDraft(null);
        setConfirmation(null);
        setRemoveMember(null);
      }
      if (e.key === "Tab") {
        const items = focusable(),
          first = items[0],
          last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      previous?.focus();
    };
  }, [dialogOpen, busy]);
  const url = `${config.apiUrl}/${kind === "compute" ? "compute" : `local/${kind}`}`;
  async function refresh(lookup = person) {
    setLoading(true);
    setError("");
    try {
      const result = await requests.get(
        url,
        kind === "compute" && lookup ? { person: lookup } : undefined,
      );
      setRecords(rowsFrom(result));
      setView(lookup && kind === "compute" ? "grants" : "profiles");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    let active = true;
    setLoading(true);
    setRecords([]);
    setDraft(null);
    setSearch("");
    setError("");
    setNotice("");
    setPerson("");
    setView("profiles");
    requests
      .get(url)
      .then((r) => {
        if (active) setRecords(rowsFrom(r));
      })
      .catch((e) => {
        if (active) setError(String(e));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [url]);
  useEffect(() => {
    let active = true;
    const needed = [
      ...new Set([
        ...fields[kind].flatMap((f) => (f.reference ? [f.reference] : [])),
        ...(kind === "workspaces" ? ["memberships"] : []),
      ]),
    ];
    Promise.all(
      needed.map(async (key) => {
        try {
          return [
            key,
            rowsFrom(
              await requests.get(
                `${config.apiUrl}/${key === "profiles" ? "compute" : `local/${key}`}`,
              ),
            ),
          ] as const;
        } catch {
          return [key, []] as const;
        }
      }),
    ).then((items) => {
      if (active) setRefs(Object.fromEntries(items));
    });
    return () => {
      active = false;
    };
  }, [kind]);
  async function mutate(run: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await run();
      setDraft(null);
      setConfirmation(null);
      setRemoveMember(null);
      await refresh();
      setNotice(message);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  function open(record?: Row) {
    setEditing(Boolean(record));
    setDraft(
      record
        ? { ...record }
        : Object.fromEntries(
            fields[kind].map((f) => [
              f.key,
              f.options?.[0] ??
                (f.type === "list"
                  ? []
                  : f.type === "number"
                    ? kind === "compute"
                      ? 1
                      : 2
                    : ""),
            ]),
          ),
    );
    setAllocation("");
    setError("");
  }
  async function save() {
    if (!draft) return;
    try {
      const payload: Row = {};
      for (const f of fields[kind]) {
        const v = draft[f.key];
        if (v !== "" && v != null)
          payload[f.key] =
            f.type === "date"
              ? new Date(
                  /Z$|[+-]\d{2}:\d{2}$/.test(String(v)) ? String(v) : `${v}Z`,
                ).toISOString()
              : v;
      }
      if (kind === "assignments") {
        if (draft.mode === "csv") payload.rows = allocation;
        if (draft.mode === "manual") {
          const groups: Record<string, string[]> = {};
          for (const line of allocation.split("\n").filter((l) => l.trim())) {
            const split = line.indexOf(":");
            if (split < 1)
              throw new Error(
                "Use one group per line: group: username, username",
              );
            const group = line.slice(0, split).trim();
            if (groups[group]) throw new Error("Group names must be unique");
            groups[group] = line
              .slice(split + 1)
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean);
          }
          payload.rows = groups;
        }
        if (draft.mode !== "random") {
          delete payload.group_size;
          delete payload.seed;
        }
      }
      if (kind === "workspaces") {
        const selected = refs.courses?.find((r) => r.id === draft.course_id);
        const bindings = Array.isArray(selected?.workspace_bindings)
          ? (selected.workspace_bindings as Row[])
          : [];
        const matching = bindings.filter((b) => b.group_id === draft.group_id);
        if (matching.length !== 1 || !selected?.resource_ceiling)
          throw new Error(
            "This course group needs an administrator-approved neutral account binding and resource ceiling before a workspace can be created.",
          );
        payload.hub_user = matching[0].hub_user;
        payload.hub_server = matching[0].hub_server;
        payload.course_ceiling = selected.resource_ceiling;
      }
      if (
        editing &&
        [
          "courses",
          "terms",
          "memberships",
          "groups",
          "groupings",
          "projects",
        ].includes(kind)
      ) {
        // Partial updates preserve service-owned provenance, bindings and metadata.
        if (kind !== "courses" && kind !== "projects") {
          payload.course_id = draft.course_id;
          if (draft.term_id) payload.term_id = draft.term_id;
        }
      }
      const target =
        kind === "workspaces"
          ? `${config.apiUrl}/workspaces/create`
          : kind === "assignments"
            ? `${config.apiUrl}/assignments/allocate`
            : url;
      await mutate(
        () => requests.post(target, payload),
        `${titles[kind]} saved successfully.`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  function ask(message: string, run: () => Promise<void>) {
    setConfirmation({ message, run });
  }
  const filtered = records.filter((r) =>
    display(r).toLowerCase().includes(search.toLowerCase()),
  );
  const columns =
    kind === "audit"
      ? ["time", "actor", "kind", "target", "outcome"]
      : kind === "compute"
        ? view === "grants"
          ? ["id", "profiles", "allowance", "expires", "reason"]
          : ["id", "name", "cpu", "memory", "gpuMemoryGiB", "enabled"]
        : kind === "memberships"
          ? ["person_id", "course_id", "term_id", "role", "expires"]
          : kind === "workspaces"
            ? ["id", "course_id", "group_id", "profile", "state"]
            : kind === "assignments"
              ? ["id", "course_id", "term_id", "mode", "state"]
              : [
                  "name",
                  "id",
                  ...(["terms", "groups", "groupings"].includes(kind)
                    ? ["course_id", "term_id"]
                    : []),
                  "source",
                ];
  const labels: Record<string, string> = {
    id: "Identifier",
    person_id: "Member",
    course_id: "Course",
    term_id: "Term",
    gpuMemoryGiB: "GPU memory (GiB)",
    kind: "Operation",
    time: "Time",
    profiles: "Profiles",
    expires: "Expires",
    allowance: "Allowance",
  };
  const writable =
    config.user.admin ||
    [
      "terms",
      "memberships",
      "groups",
      "groupings",
      "assignments",
      "workspaces",
    ].includes(kind);
  return (
    <section className="admin-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Administration</p>
          <h1>{titles[kind]}</h1>
          <p>{descriptions[kind]}</p>
        </div>
        {kind !== "audit" && writable && (
          <button className="primary-button" onClick={() => open()}>
            <Plus size={17} />{" "}
            {kind === "compute"
              ? "Add grant"
              : `Create ${kind === "memberships" ? "membership" : kind === "workspaces" ? "workspace" : kind === "assignments" ? "assignment" : kind === "groupings" ? "grouping" : kind.slice(0, -1)}`}
          </button>
        )}
      </div>
      {kind === "compute" && (
        <form
          className="lookup-panel"
          onSubmit={(e) => {
            e.preventDefault();
            if (person.trim()) void refresh(person.trim());
          }}
        >
          <label>
            Look up a person’s grants
            <input
              value={person}
              onChange={(e) => setPerson(e.target.value)}
              placeholder="Reviewed canonical person ID"
              required
            />
          </label>
          <button className="primary-button">View grants</button>
          <button
            type="button"
            onClick={() => {
              setPerson("");
              void refresh("");
            }}
          >
            All profiles
          </button>
          <p className="field-help">
            Identity linking is pending qualification. Usernames and email
            addresses cannot substitute for canonical person IDs.
          </p>
        </form>
      )}
      {error && (
        <div className="feedback error" role="alert">
          <AlertCircle size={18} />
          <span>{error}</span>
        </div>
      )}
      {notice && (
        <div className="feedback success" role="status">
          <CheckCircle2 size={18} />
          {notice}
        </div>
      )}
      <div className="records-panel">
        <div className="table-toolbar">
          <div className="search-box">
            <Search size={17} />
            <input
              aria-label={`Search ${titles[kind]}`}
              placeholder={`Search ${titles[kind].toLowerCase()}…`}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <span className="record-count">
            {filtered.length} {filtered.length === 1 ? "record" : "records"}
          </span>
          <button onClick={() => void refresh()} disabled={loading || busy}>
            <RefreshCw size={16} /> Refresh
          </button>
        </div>
        {loading ? (
          <div className="empty-state" role="status">
            Loading {titles[kind].toLowerCase()}…
          </div>
        ) : filtered.length === 0 ? (
          <div className="empty-state">
            <FolderOpen size={32} />
            <h2>
              {search
                ? "No matching records"
                : `No ${kind === "compute" ? view : titles[kind].toLowerCase()} yet`}
            </h2>
            <p>
              {search
                ? "Try another search."
                : kind === "audit"
                  ? "Changes will appear here with their actor and outcome."
                  : kind === "compute"
                    ? "Profiles and grants are supplied by the shared compute service."
                    : "Create a record to get started. Only records you are authorized to access appear here."}
            </p>
            {!search && kind !== "audit" && writable && (
              <button onClick={() => open()}>
                Create your first{" "}
                {kind === "memberships" ? "membership" : "record"}
              </button>
            )}
          </div>
        ) : (
          <div className="table-scroll">
            <table className="records-table">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th key={c}>{labels[c] ?? c.replaceAll("_", " ")}</th>
                  ))}
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r, i) => (
                  <tr key={String(r.id ?? i)}>
                    {columns.map((c, j) => (
                      <td key={c} className={j === 0 ? "record-title" : ""}>
                        {[
                          "state",
                          "source",
                          "outcome",
                          "role",
                          "enabled",
                        ].includes(c) ? (
                          <span
                            className={`status-pill ${r[c] === "failure" || r[c] === "denied" ? "bad" : ""}`}
                          >
                            {display(
                              r[c] ?? (c === "state" ? "open" : undefined),
                            )}
                          </span>
                        ) : (
                          display(r[c])
                        )}
                      </td>
                    ))}
                    <td>
                      <div className="row-actions">
                        {kind === "audit" ? (
                          <details>
                            <summary>Details</summary>
                            <dl>
                              {Object.entries(r)
                                .filter(([k]) => !columns.includes(k))
                                .map(([k, v]) => (
                                  <div key={k}>
                                    <dt>{k}</dt>
                                    <dd>{display(v)}</dd>
                                  </div>
                                ))}
                            </dl>
                          </details>
                        ) : kind === "workspaces" ? (
                          <>
                            {["start", "stop"].map((action) => (
                              <button
                                key={action}
                                disabled={busy}
                                onClick={() =>
                                  ask(
                                    `${action === "stop" ? "Stop this workspace? Its active kernel will be interrupted; files are preserved." : "Start this workspace? All member reservations must be available."}`,
                                    () =>
                                      mutate(
                                        () =>
                                          requests.post(
                                            `${config.apiUrl}/workspaces/${encodeURIComponent(String(r.id))}/${action}`,
                                            {},
                                          ),
                                        `Workspace ${action} requested.`,
                                      ),
                                  )
                                }
                              >
                                {action === "start" ? "Start" : "Stop"}
                              </button>
                            ))}
                            <button
                              disabled={busy}
                              onClick={() => {
                                setRemoveMember(r);
                                setMembership("");
                              }}
                            >
                              Remove member
                            </button>
                          </>
                        ) : kind === "assignments" ? (
                          <button
                            disabled={busy || r.state === "closed"}
                            onClick={() =>
                              ask(
                                "Close this assignment? Associated writers will stop before the archive becomes read-only. Files are preserved.",
                                () =>
                                  mutate(
                                    () =>
                                      requests.post(
                                        `${config.apiUrl}/assignments/${encodeURIComponent(String(r.id))}/close`,
                                        {},
                                      ),
                                    "Assignment closure requested.",
                                  ),
                              )
                            }
                          >
                            Close assignment
                          </button>
                        ) : (
                          writable &&
                          (r.source == null || r.source === "local") &&
                          (kind !== "compute" || view === "grants") && (
                            <>
                              <button
                                aria-label={`Edit ${display(r.name ?? r.id)}`}
                                onClick={() => open(r)}
                              >
                                <Pencil size={15} />
                                {kind === "compute" ? "Use for grant" : "Edit"}
                              </button>
                              {kind !== "compute" && (
                                <button
                                  className="danger-button"
                                  aria-label={`Remove ${display(r.name ?? r.id)}`}
                                  disabled={busy}
                                  onClick={() =>
                                    ask(
                                      "Remove this record? Referenced records must be removed first. This action is audited.",
                                      () =>
                                        mutate(
                                          () => requests.del(url, { id: r.id }),
                                          "Record removed.",
                                        ),
                                    )
                                  }
                                >
                                  <Trash2 size={15} />
                                </button>
                              )}
                            </>
                          )
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {draft && (
        <div className="dialog-backdrop">
          <section
            ref={dialogRef}
            className="editor-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="editor-title"
          >
            <div className="dialog-heading">
              <div>
                <p className="eyebrow">
                  {editing ? "Edit record" : "New record"}
                </p>
                <h2 id="editor-title">{titles[kind]}</h2>
              </div>
              <button
                aria-label="Close editor"
                disabled={busy}
                onClick={() => setDraft(null)}
              >
                <X size={20} />
              </button>
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void save();
              }}
            >
              <div className="form-grid">
                {fields[kind]
                  .filter(
                    (f) =>
                      kind !== "assignments" ||
                      draft.mode === "random" ||
                      !["seed", "group_size"].includes(f.key),
                  )
                  .map((f) => {
                    let options = refs[f.reference ?? ""] ?? [];
                    if (
                      f.reference === "terms" ||
                      f.reference === "groups" ||
                      f.reference === "memberships"
                    )
                      options = options.filter(
                        (r) =>
                          r.course_id === draft.course_id &&
                          (!draft.term_id ||
                            !r.term_id ||
                            r.term_id === draft.term_id),
                      );
                    const locked =
                      editing &&
                      (f.key === "id" ||
                        (!["courses", "projects", "compute"].includes(kind) &&
                          ["course_id", "term_id"].includes(f.key)));
                    const value = draft[f.key] ?? (f.type === "list" ? [] : "");
                    const change = (v: unknown) =>
                      setDraft({
                        ...draft,
                        [f.key]: v,
                        ...(f.key === "course_id"
                          ? {
                              term_id: "",
                              group_id: "",
                              group_ids: [],
                              members: [],
                            }
                          : {}),
                      });
                    return (
                      <label
                        key={f.key}
                        className={f.type === "textarea" ? "span-two" : ""}
                      >
                        {f.label}
                        {f.required && <span className="required"> *</span>}
                        {f.type === "textarea" ? (
                          <textarea
                            aria-label={f.label}
                            rows={3}
                            value={String(value)}
                            onChange={(e) => change(e.target.value)}
                          />
                        ) : f.options ? (
                          <select
                            value={String(value)}
                            onChange={(e) => change(e.target.value)}
                          >
                            {f.options.map((o) => (
                              <option key={o}>{o}</option>
                            ))}
                          </select>
                        ) : f.reference && options.length > 0 ? (
                          <select
                            multiple={f.type === "list"}
                            required={f.required}
                            disabled={locked}
                            value={
                              f.type === "list"
                                ? (value as string[])
                                : String(value)
                            }
                            onChange={(e) =>
                              change(
                                f.type === "list"
                                  ? Array.from(
                                      e.target.selectedOptions,
                                      (o) => o.value,
                                    )
                                  : e.target.value,
                              )
                            }
                          >
                            {f.type !== "list" && (
                              <option value="">
                                Select {f.label.toLowerCase()}
                              </option>
                            )}
                            {options.map((r, i) => {
                              const val = String(
                                f.reference === "memberships"
                                  ? r.person_id
                                  : (r.id ?? r.name ?? i),
                              );
                              return (
                                <option key={val} value={val}>
                                  {display(r.name ?? r.person_id ?? r.id)}
                                </option>
                              );
                            })}
                          </select>
                        ) : (
                          <input
                            required={f.required}
                            disabled={locked}
                            type={
                              f.type === "number"
                                ? "number"
                                : f.type === "date"
                                  ? "datetime-local"
                                  : "text"
                            }
                            min={f.type === "number" ? 1 : undefined}
                            step={f.type === "number" ? 1 : undefined}
                            value={
                              f.type === "list"
                                ? (value as string[]).join(", ")
                                : f.type === "date"
                                  ? String(value).replace(/Z$/, "").slice(0, 16)
                                  : String(value)
                            }
                            onChange={(e) =>
                              change(
                                f.type === "number"
                                  ? Number(e.target.value)
                                  : f.type === "list"
                                    ? e.target.value
                                        .split(",")
                                        .map((v) => v.trim())
                                        .filter(Boolean)
                                    : e.target.value,
                              )
                            }
                          />
                        )}
                        {f.key === "id" && (
                          <span className="field-help">
                            Stable identifier; cannot be changed after creation.
                          </span>
                        )}
                        {f.type === "list" && (
                          <span className="field-help">
                            {options.length
                              ? "Select multiple entries with Ctrl or Cmd."
                              : "Enter comma-separated identifiers. Available choices require authorized reference data."}
                          </span>
                        )}
                      </label>
                    );
                  })}
                {kind === "assignments" && draft.mode !== "random" && (
                  <label className="span-two">
                    {draft.mode === "csv" ? "CSV allocation" : "Manual groups"}
                    <textarea
                      required
                      rows={5}
                      value={allocation}
                      onChange={(e) => setAllocation(e.target.value)}
                      placeholder={
                        draft.mode === "csv"
                          ? "person_id,group_id\nalice,team-a\nbob,team-a"
                          : "team-a: alice, bob\nteam-b: carol, dave"
                      }
                    />
                    <span className="field-help">
                      Use existing course member usernames. Allocation is
                      validated before records are created.
                    </span>
                  </label>
                )}
              </div>
              {kind === "workspaces" && (
                <p className="info-note">
                  The neutral account and resource ceiling come from approved
                  course configuration. Starting reserves one allowance for
                  every member.
                </p>
              )}
              {error && (
                <p className="feedback error" role="alert">
                  {error}
                </p>
              )}
              <div className="dialog-footer">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setDraft(null)}
                >
                  Cancel
                </button>
                <button className="primary-button" disabled={busy}>
                  {busy
                    ? "Saving…"
                    : kind === "assignments"
                      ? "Allocate groups"
                      : kind === "compute"
                        ? "Save grant"
                        : "Save record"}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
      {removeMember && (
        <div className="dialog-backdrop">
          <section
            ref={dialogRef}
            className="confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="remove-title"
          >
            <h2 id="remove-title">Remove workspace member</h2>
            <p>
              This stops the shared kernel, revokes the member’s access and
              preserves files.
            </p>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void mutate(
                  () =>
                    requests.post(
                      `${config.apiUrl}/workspaces/${encodeURIComponent(String(removeMember.id))}/remove-member`,
                      { membership_id: membership },
                    ),
                  "Member removal requested.",
                );
              }}
            >
              <label>
                Course membership
                <select
                  required
                  value={membership}
                  onChange={(e) => setMembership(e.target.value)}
                >
                  <option value="">Select member</option>
                  {(refs.memberships ?? [])
                    .filter(
                      (m) =>
                        m.course_id === removeMember.course_id &&
                        m.group_id === removeMember.group_id &&
                        (!removeMember.term_id ||
                          m.term_id === removeMember.term_id),
                    )
                    .map((m) => (
                      <option key={String(m.id)} value={String(m.id)}>
                        {display(m.person_id)}
                      </option>
                    ))}
                </select>
              </label>
              {error && <p role="alert">{error}</p>}
              <div className="dialog-footer">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setRemoveMember(null)}
                >
                  Cancel
                </button>
                <button
                  className="primary-button"
                  disabled={busy || !membership}
                >
                  {busy ? "Removing…" : "Remove member"}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
      {confirmation && (
        <div className="dialog-backdrop">
          <section
            ref={dialogRef}
            className="confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="confirm-title"
          >
            <h2 id="confirm-title">Confirm action</h2>
            <p>{confirmation.message}</p>
            {error && <p role="alert">{error}</p>}
            <div className="dialog-footer">
              <button disabled={busy} onClick={() => setConfirmation(null)}>
                Cancel
              </button>
              <button
                className="primary-button"
                disabled={busy}
                onClick={() => void confirmation.run()}
              >
                {busy ? "Working…" : "Confirm"}
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
