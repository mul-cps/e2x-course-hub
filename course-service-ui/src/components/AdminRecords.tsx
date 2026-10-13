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
  Cpu,
} from "lucide-react";
import { requests } from "../api/client";
import { config } from "../config";
import PlatformStatusPanel from "./PlatformStatusPanel";

type Row = Record<string, unknown>;
type Field = {
  key: string;
  label: string;
  required?: boolean;
  type?: "number" | "list" | "textarea" | "date";
  options?: string[];
  reference?: string;
  help?: string;
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
    {
      ...course,
      required: false,
      help: "Optionally associate this project with an existing course.",
    },
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
      help: "Maximum simultaneous interactive reservations. A shared workspace reserves one allowance per member.",
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
function execution(profile: Row): string {
  const gpu =
    profile.gpu && typeof profile.gpu === "object"
      ? (profile.gpu as Row)
      : undefined;
  const modes: Record<string, string> = {
    none: "CPU",
    shared: "Shared GPU",
    exclusive: "Exclusive GPU",
  };
  const kinds: Record<string, string> = {
    interactive: "Interactive",
    batch: "Batch",
  };
  return [
    kinds[String(profile.kind)] ?? display(profile.kind),
    modes[String(gpu?.mode)] ?? "GPU mode unspecified",
  ].join(" · ");
}
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
  const [loadFailed, setLoadFailed] = useState(false);
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [sort, setSort] = useState<{ key: string; ascending: boolean } | null>(
    null,
  );
  const [search, setSearch] = useState(""),
    [person, setPerson] = useState(""),
    [view, setView] = useState<"profiles" | "grants">("profiles");
  const [draft, setDraft] = useState<Row | null>(null),
    [editing, setEditing] = useState(false),
    [allocation, setAllocation] = useState("");
  const dialogRef = useRef<HTMLElement>(null);
  const [removeMember, setRemoveMember] = useState<Row | null>(null),
    [membership, setMembership] = useState("");
  const [profileDetails, setProfileDetails] = useState<Row | null>(null);
  const [groupApproval, setGroupApproval] = useState<Row | null>(null);
  const [approvalProfile, setApprovalProfile] = useState("");
  const [approvalError, setApprovalError] = useState("");
  const [profileEditing, setProfileEditing] = useState(false),
    [selectedProfile, setSelectedProfile] = useState("");
  const [profileDiscovery, setProfileDiscovery] = useState<{
    workspace: string;
    loading: boolean;
    profiles: Row[];
    failed: boolean;
  }>({ workspace: "", loading: true, profiles: [], failed: false });
  const [grantWorkspace, setGrantWorkspace] = useState("");
  const [grantDiscovery, setGrantDiscovery] = useState<{
    workspace: string;
    loading: boolean;
    profiles: Row[];
    failed: boolean;
  }>({ workspace: "", loading: false, profiles: [], failed: false });
  const [confirmation, setConfirmation] = useState<{
    message: string;
    run: () => Promise<void>;
  } | null>(null);
  const dialogOpen = Boolean(
    draft || confirmation || removeMember || profileDetails,
  );
  useEffect(() => {
    if (!dialogOpen) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const background: [HTMLElement, boolean][] = [];
    let branch = dialog?.parentElement;
    while (branch && branch !== document.body) {
      for (const sibling of Array.from(branch.parentElement?.children ?? [])) {
        if (sibling !== branch && sibling instanceof HTMLElement) {
          background.push([sibling, sibling.inert]);
          sibling.inert = true;
        }
      }
      branch = branch.parentElement;
    }
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
        setProfileDetails(null);
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
      for (const [element, inert] of background) element.inert = inert;
      document.body.style.overflow = originalOverflow;
      previous?.focus();
    };
  }, [dialogOpen, busy]);
  const url = `${config.apiUrl}/${kind === "compute" ? "compute" : `local/${kind}`}`;
  async function refresh(lookup = person) {
    setLoading(true);
    setLoadFailed(false);
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
      setLoadFailed(true);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    let active = true;
    setLoading(true);
    setRecords([]);
    setLoadFailed(false);
    setDraft(null);
    setProfileDetails(null);
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
        if (active) {
          setError(String(e));
          setLoadFailed(true);
        }
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
        ...fields[kind].flatMap((f) =>
          f.reference && !(kind === "workspaces" && f.reference === "profiles")
            ? [f.reference]
            : [],
        ),
        ...(kind === "workspaces" ? ["memberships"] : []),
        ...(kind === "compute" && config.user.admin ? ["workspaces"] : []),
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
      if (active)
        setRefs((previous) => ({ ...previous, ...Object.fromEntries(items) }));
    });
    return () => {
      active = false;
    };
  }, [kind]);
  const grantBaseProfiles = (refs.profiles ?? []).filter((profile) => {
    const gpu = profile.gpu;
    return (
      !profile.nativeGroupWorkspace &&
      !profile.nativePilotWorkspace &&
      !(gpu && typeof gpu === "object" && (gpu as Row).mode !== "none") &&
      !(typeof gpu === "string" && gpu !== "0") &&
      !/^interactive-(shared|exclusive|native)-/.test(String(profile.id))
    );
  });
  const grantProfiles = [
    ...grantBaseProfiles,
    ...(grantDiscovery.workspace === grantWorkspace &&
    !grantDiscovery.loading &&
    !grantDiscovery.failed
      ? grantDiscovery.profiles.filter(
          (profile) =>
            profile.kind === "interactive" &&
            profile.enabled === true &&
            profile.nativeGroupWorkspace === grantWorkspace &&
            (profile.gpu as Row | undefined)?.mode === "shared" &&
            !grantBaseProfiles.some((base) => base.id === profile.id),
        )
      : []),
  ];
  const grantDialogOpen = kind === "compute" && draft !== null;
  useEffect(() => {
    if (!grantDialogOpen || !grantWorkspace) return;
    let active = true;
    setGrantDiscovery({
      workspace: grantWorkspace,
      loading: true,
      profiles: [],
      failed: false,
    });
    requests
      .get(`${config.apiUrl}/compute`, { workspace: grantWorkspace })
      .then((result) => {
        if (active)
          setGrantDiscovery({
            workspace: grantWorkspace,
            loading: false,
            profiles: rowsFrom(result),
            failed: false,
          });
      })
      .catch(() => {
        if (active)
          setGrantDiscovery({
            workspace: grantWorkspace,
            loading: false,
            profiles: [],
            failed: true,
          });
      });
    return () => {
      active = false;
    };
  }, [grantDialogOpen, grantWorkspace]);
  const workspaceProfileScope =
    kind === "workspaces" ? String(draft?.id ?? "") : "";
  useEffect(() => {
    if (kind !== "workspaces") return;
    let active = true;
    setRefs((previous) => ({ ...previous, profiles: [] }));
    requests
      .get(
        `${config.apiUrl}/compute`,
        workspaceProfileScope
          ? { workspace: workspaceProfileScope }
          : undefined,
      )
      .then((result) => {
        if (active)
          setRefs((previous) => ({ ...previous, profiles: rowsFrom(result) }));
      })
      .catch(() => {
        if (active) setRefs((previous) => ({ ...previous, profiles: [] }));
      });
    return () => {
      active = false;
    };
  }, [kind, workspaceProfileScope]);
  useEffect(() => {
    if (kind !== "workspaces" || !profileDetails) return;
    let active = true;
    const workspace = String(profileDetails.id);
    setProfileDiscovery({
      workspace,
      loading: true,
      profiles: [],
      failed: false,
    });
    requests
      .get(`${config.apiUrl}/compute`, { workspace })
      .then((result) => {
        if (active)
          setProfileDiscovery({
            workspace,
            loading: false,
            profiles: rowsFrom(result),
            failed: false,
          });
      })
      .catch(() => {
        if (active)
          setProfileDiscovery({
            workspace,
            loading: false,
            profiles: [],
            failed: true,
          });
      });
    return () => {
      active = false;
    };
  }, [kind, profileDetails]);
  const detailProfiles = profileDiscovery.profiles.filter((profile) => {
    if (profile.kind !== "interactive" || profile.enabled !== true)
      return false;
    const gpu = profile.gpu as Row | undefined;
    const groupScoped =
      profile.nativeGroupWorkspace !== undefined ||
      profile.nativePilotWorkspace !== undefined ||
      (gpu && gpu.mode !== "none") ||
      String(profile.id).startsWith("interactive-shared-") ||
      String(profile.id).startsWith("interactive-exclusive-");
    return (
      !groupScoped ||
      profile.nativeGroupWorkspace === String(profileDetails?.id)
    );
  });
  const detailsPending =
    profileDiscovery.loading ||
    profileDiscovery.workspace !== String(profileDetails?.id);
  const profileEditable =
    profileDetails?.state === "stopped" &&
    (profileDetails.source == null || profileDetails.source === "local") &&
    profileDetails.external_id == null &&
    !profileDetails.archive_pending &&
    !profileDetails.archived;
  const storedProfileAvailable = detailProfiles.some(
    (profile) => profile.id === profileDetails?.profile,
  );
  useEffect(() => {
    setGroupApproval(null);
    setApprovalError("");
    setApprovalProfile("");
    if (kind !== "workspaces" || !profileDetails) return;
    let active = true;
    requests.get(`${config.apiUrl}/native-group-approvals`).then((result) => {
      const registry = result as Row;
      const valid = registry && typeof registry.revision === "string" &&
        /^sha256:[0-9a-f]{64}$/.test(registry.revision) &&
        typeof registry.ready === "boolean" && Array.isArray(registry.qualified_profiles) &&
        registry.qualified_profiles.every((profile) => typeof profile === "string") &&
        registry.approvals && typeof registry.approvals === "object" && !Array.isArray(registry.approvals);
      if (active) setGroupApproval(valid ? registry : null);
    }).catch(() => {
      // The backend reserves qualification changes for console administrators.
      if (active) setGroupApproval(null);
    });
    return () => { active = false; };
  }, [kind, profileDetails]);
  const qualifiedProfiles = Array.isArray(groupApproval?.qualified_profiles)
    ? groupApproval.qualified_profiles.filter((value): value is string => typeof value === "string")
    : [];
  const approvedGroup = (groupApproval?.approvals as Record<string, Row> | undefined)?.[String(profileDetails?.id)];
  async function updateGroupApproval(revoke = false) {
    if (!profileDetails || !groupApproval || busy || (!revoke && !approvalProfile)) return;
    setBusy(true);
    setApprovalError("");
    try {
      await requests.post(
        `${config.apiUrl}/native-group-approvals/${encodeURIComponent(String(profileDetails.id))}/${revoke ? "revoke" : "approve"}`,
        { expected_revision: groupApproval.revision, ...(!revoke ? { profile: approvalProfile } : {}) },
      );
      await refresh();
      setProfileDetails({ ...profileDetails });
    } catch (e) {
      setApprovalError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function mutate(run: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await run();
      setDraft(null);
      setConfirmation(null);
      setRemoveMember(null);
      setProfileDetails(null);
      await refresh();
      setNotice(message);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  function open(record?: Row) {
    setGrantWorkspace("");
    setGrantDiscovery({
      workspace: "",
      loading: false,
      profiles: [],
      failed: false,
    });
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
  const workspaceProfiles = (refs.profiles ?? []).filter(
    (p) =>
      (p.enabled !== false ||
        (workspaceProfileScope !== "" &&
          p.nativePilotWorkspace === workspaceProfileScope)) &&
      (p.kind === "interactive" ||
        (p.kind == null && String(p.id).startsWith("interactive-"))),
  );
  const cpuOnly =
    workspaceProfiles.length > 0 &&
    workspaceProfiles.every(
      (p) =>
        String(p.id).includes("cpu") ||
        (p.gpu as Row | undefined)?.mode === "none",
    );
  async function save() {
    if (!draft) return;
    try {
      if (
        kind === "compute" &&
        Array.isArray(draft.profiles) &&
        draft.profiles.some(
          (id) => !grantProfiles.some((profile) => profile.id === id),
        )
      )
        throw new Error(
          "Select profiles verified for the current shared workspace or clear unavailable choices.",
        );
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
      if (
        payload.starts &&
        payload.expires &&
        Date.parse(String(payload.starts)) >=
          Date.parse(String(payload.expires))
      )
        throw new Error("Expiry must be after the start date.");
      if (kind === "workspaces") {
        if (!workspaceProfiles.some((p) => p.id === draft.profile))
          throw new Error("Select an available interactive profile.");
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
  const filtered = records
    .filter((r) => display(r).toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) =>
      sort
        ? display(a[sort.key]).localeCompare(display(b[sort.key]), undefined, {
            numeric: true,
          }) * (sort.ascending ? 1 : -1)
        : 0,
    );
  const columns =
    kind === "audit"
      ? ["time", "actor", "kind", "target", "outcome"]
      : kind === "compute"
        ? view === "grants"
          ? [
              "id",
              "profiles",
              "projects",
              "allowance",
              "starts",
              "expires",
              "reason",
            ]
          : [
              "id",
              "name",
              "execution",
              "cpu",
              "memory",
              "gpuMemoryGiB",
              "qualification",
              "enabled",
            ]
        : kind === "memberships"
          ? [
              "person_id",
              "canonical_person_id",
              "course_id",
              "term_id",
              "role",
              "starts",
              "expires",
              "source",
            ]
          : kind === "workspaces"
            ? ["id", "course_id", "group_id", "profile", "state", "account_state", "notice"]
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
    allowance: "Interactive allowance",
    starts: "Valid from",
    canonical_person_id: "Canonical person",
    execution: "Execution",
    qualification: "Qualification",
    account_state: "Account",
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
      {(kind === "compute" || kind === "workspaces") && <PlatformStatusPanel />}
      {kind === "compute" && (
        <p className="info-note">
          The catalog includes interactive notebooks and batch jobs. Profile
          availability comes from shared policy; GPU qualification and workspace
          approval must be current. Grants do not activate disabled profiles.
        </p>
      )}
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
          {search && (
            <button
              className="clear-search"
              aria-label="Clear search"
              onClick={() => setSearch("")}
            >
              <X size={15} />
            </button>
          )}
          <span className="record-count">
            {filtered.length}
            {search ? ` of ${records.length}` : ""}{" "}
            {filtered.length === 1 ? "record" : "records"}
          </span>
          <button onClick={() => void refresh()} disabled={loading || busy}>
            <RefreshCw size={16} /> Refresh
          </button>
        </div>
        {loading ? (
          <div className="empty-state" role="status">
            Loading {titles[kind].toLowerCase()}…
          </div>
        ) : loadFailed ? (
          <div className="empty-state">
            <AlertCircle size={32} />
            <h2>Records could not be loaded</h2>
            <p>Retry to see current records and availability.</p>
            <button disabled={busy} onClick={() => void refresh()}>
              Retry loading
            </button>
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
          <div
            className="table-scroll"
            role="region"
            aria-label={`${titles[kind]} records`}
            tabIndex={0}
          >
            <table className="records-table">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th
                      key={c}
                      aria-sort={
                        sort?.key === c
                          ? sort.ascending
                            ? "ascending"
                            : "descending"
                          : "none"
                      }
                    >
                      <button
                        className="column-sort"
                        onClick={() =>
                          setSort({
                            key: c,
                            ascending: sort?.key === c ? !sort.ascending : true,
                          })
                        }
                      >
                        {labels[c] ?? c.replaceAll("_", " ")}
                        <span aria-hidden="true">
                          {sort?.key === c ? (sort.ascending ? "↑" : "↓") : "↕"}
                        </span>
                      </button>
                    </th>
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
                      <td
                        key={c}
                        title={display(r[c])}
                        className={`${j === 0 ? "record-title" : ""} ${c === "id" ? "record-id" : ""}`}
                      >
                        {c === "account_state" ? (
                          display(({ ready: "Account ready", not_provisioned: "Provision on Start", review_required: "Review required", recovery_required: "Recovery required", unavailable: "Status unavailable" } as Record<string, string>)[String(r[c])] ?? "Status unavailable")
                        ) : c === "execution" ? (
                          execution(r)
                        ) : c === "qualification" ? (
                          display((r.gpu as Row | undefined)?.qualification)
                        ) : [
                            "state",
                            "source",
                            "outcome",
                            "role",
                            "enabled",
                          ].includes(c) ? (
                          <span
                            className={`status-pill ${r[c] === "failure" || r[c] === "denied" ? "bad" : r[c] === "running" || r[c] === "success" || r[c] === true ? "good" : r[c] === false || r[c] === "stopped" || r[c] === "closed" ? "muted" : ""}`}
                          >
                            {display(
                              c === "enabled"
                                ? r[c] === true
                                  ? "Available"
                                  : r[c] === false
                                    ? "Unavailable"
                                    : undefined
                                : (r[c] ??
                                    (c === "state" ? "open" : undefined)),
                            )}
                          </span>
                        ) : ["time", "starts", "expires"].includes(c) &&
                          r[c] &&
                          !Number.isNaN(Date.parse(String(r[c]))) ? (
                          <time dateTime={String(r[c])}>
                            {new Intl.DateTimeFormat("en-GB", {
                              dateStyle: "medium",
                              timeStyle: "short",
                              timeZone: "UTC",
                            }).format(new Date(String(r[c])))}{" "}
                            UTC
                          </time>
                        ) : (
                          display(r[c])
                        )}
                      </td>
                    ))}
                    <td>
                      <div className="row-actions">
                        {kind !== "audit" && (
                          <details>
                            <summary>Details</summary>
                            <dl>
                              {Object.entries(r)
                                .filter(([key]) => !columns.includes(key))
                                .map(([key, value]) => (
                                  <div key={key}>
                                    <dt>
                                      {key
                                        .replaceAll("_", " ")
                                        .replace(/^./, (c) => c.toUpperCase())}
                                    </dt>
                                    <dd>{display(value)}</dd>
                                  </div>
                                ))}
                            </dl>
                          </details>
                        )}
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
                            <button
                              aria-label={`View workspace profile ${display(r.id)}`}
                              onClick={() => {
                                setProfileDiscovery({
                                  workspace: String(r.id),
                                  loading: true,
                                  profiles: [],
                                  failed: false,
                                });
                                setProfileEditing(false);
                                setSelectedProfile("");
                                setError("");
                                setProfileDetails({ ...r });
                              }}
                            >
                              <Cpu size={15} />
                              Compute profile
                            </button>
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
              <p className="form-intro">
                Fields marked * are required. Changes are checked against your
                permissions and recorded in the audit log.
              </p>
              <div className="form-grid">
                {kind === "compute" && config.user.admin && (
                  <label className="span-two">
                    Shared workspace
                    <select
                      aria-label="Shared workspace"
                      value={grantWorkspace}
                      disabled={busy}
                      onChange={(event) => {
                        setGrantWorkspace(event.target.value);
                        setGrantDiscovery({
                          workspace: event.target.value,
                          loading: Boolean(event.target.value),
                          profiles: [],
                          failed: false,
                        });
                        setDraft({
                          ...draft,
                          profiles: Array.isArray(draft.profiles)
                            ? draft.profiles.filter((id) =>
                                grantBaseProfiles.some(
                                  (profile) => profile.id === id,
                                ),
                              )
                            : [],
                        });
                      }}
                    >
                      <option value="">No shared workspace</option>
                      {(refs.workspaces ?? [])
                        .filter(
                          (workspace) =>
                            (workspace.source == null ||
                              workspace.source === "local") &&
                            workspace.external_id == null,
                        )
                        .map((workspace) => (
                          <option
                            key={String(workspace.id)}
                            value={String(workspace.id)}
                          >
                            {display(workspace.name ?? workspace.id)}
                          </option>
                        ))}
                    </select>
                    <span className="field-help">
                      GPU choices are available only for the selected approved
                      shared workspace. CPU choices remain available.
                    </span>
                    {grantWorkspace &&
                      (grantDiscovery.loading ||
                        grantDiscovery.workspace !== grantWorkspace) && (
                        <span role="status">
                          Checking workspace GPU profiles…
                        </span>
                      )}
                    {grantWorkspace &&
                      grantDiscovery.workspace === grantWorkspace &&
                      grantDiscovery.failed && (
                        <span role="alert">
                          Workspace GPU profiles could not be verified. CPU
                          choices remain available.
                        </span>
                      )}
                  </label>
                )}
                {fields[kind]
                  .filter(
                    (f) =>
                      kind !== "assignments" ||
                      draft.mode === "random" ||
                      !["seed", "group_size"].includes(f.key),
                  )
                  .map((f) => {
                    let options =
                      f.reference === "profiles" && kind === "workspaces"
                        ? workspaceProfiles
                        : f.reference === "profiles" && kind === "compute"
                          ? grantProfiles
                          : (refs[f.reference ?? ""] ?? []);
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
                            aria-label={f.label}
                            value={String(value)}
                            onChange={(e) => change(e.target.value)}
                          >
                            {f.options.map((o) => (
                              <option key={o}>{o}</option>
                            ))}
                          </select>
                        ) : f.reference &&
                          (options.length > 0 || f.reference === "profiles") ? (
                          <select
                            aria-label={f.label}
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
                        {f.help && <span className="field-help">{f.help}</span>}
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
              {kind === "workspaces" && cpuOnly && (
                <p className="availability-note">
                  <Cpu size={17} />
                  Only CPU profiles are currently available. GPU profiles are
                  disabled by shared policy.
                </p>
              )}
              {kind === "workspaces" && workspaceProfiles.length === 0 && (
                <p className="availability-note">
                  No eligible interactive profiles are available. Check compute
                  access before creating a workspace.
                </p>
              )}
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
      {profileDetails && (
        <div className="dialog-backdrop">
          <section
            ref={dialogRef}
            className="editor-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="profile-details-title"
          >
            <div className="dialog-heading">
              <div>
                <p className="eyebrow">
                  {profileEditing
                    ? "Change stopped workspace profile"
                    : "Profile details"}
                </p>
                <h2 id="profile-details-title">Workspace compute profile</h2>
              </div>
              <button
                aria-label="Close profile details"
                disabled={busy}
                onClick={() => setProfileDetails(null)}
              >
                <X size={20} />
              </button>
            </div>
            <dl className="form-grid">
              <div>
                <dt>Workspace</dt>
                <dd>{display(profileDetails.id)}</dd>
              </div>
              <div>
                <dt>Workspace state</dt>
                <dd>{display(profileDetails.state)}</dd>
              </div>
              <div>
                <dt>Stored profile</dt>
                <dd data-testid="stored-workspace-profile">
                  {display(profileDetails.profile)}
                </dd>
              </div>
              <div>
                <dt>Current availability</dt>
                <dd data-testid="stored-profile-availability" role="status">
                  {detailsPending
                    ? "Checking availability…"
                    : profileDiscovery.failed
                      ? "Availability could not be verified"
                      : storedProfileAvailable
                        ? "Available for this workspace"
                        : "Unavailable in current workspace policy"}
                </dd>
              </div>
            </dl>
            {profileDiscovery.failed && !detailsPending && (
              <p role="alert">
                Profile discovery was denied or unavailable. The stored profile
                is shown above; availability has not been confirmed.
              </p>
            )}
            {!detailsPending && !profileDiscovery.failed && (
              <div data-testid="workspace-available-profiles">
                <h3>Available interactive profiles</h3>
                {detailProfiles.length ? (
                  <ul>
                    {detailProfiles.map((profile) => (
                      <li key={String(profile.id)}>{display(profile.id)}</li>
                    ))}
                  </ul>
                ) : (
                  <p>No profiles are currently available for this workspace.</p>
                )}
              </div>
            )}
            {profileEditable &&
              (profileEditing ? (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    if (
                      detailsPending ||
                      profileDiscovery.failed ||
                      !detailProfiles.some(
                        (profile) => profile.id === selectedProfile,
                      )
                    )
                      return;
                    void mutate(
                      () =>
                        requests.post(
                          `${config.apiUrl}/workspaces/${encodeURIComponent(String(profileDetails.id))}/profile`,
                          { profile: selectedProfile },
                        ),
                      "Workspace profile updated.",
                    );
                  }}
                >
                  <label>
                    New compute profile
                    <select
                      aria-label="New compute profile"
                      required
                      value={selectedProfile}
                      disabled={
                        busy || detailsPending || profileDiscovery.failed
                      }
                      onChange={(event) =>
                        setSelectedProfile(event.target.value)
                      }
                    >
                      <option value="">Select a profile</option>
                      {detailProfiles.map((profile) => (
                        <option
                          key={String(profile.id)}
                          value={String(profile.id)}
                        >
                          {display(profile.id)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <p className="field-help">
                    The workspace stays stopped and its files are preserved.
                  </p>
                  {error && <p role="alert">{error}</p>}
                  <div className="dialog-footer">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => setProfileEditing(false)}
                    >
                      Cancel
                    </button>
                    <button
                      className="primary-button"
                      disabled={
                        busy ||
                        detailsPending ||
                        profileDiscovery.failed ||
                        selectedProfile === profileDetails.profile ||
                        !detailProfiles.some(
                          (profile) => profile.id === selectedProfile,
                        )
                      }
                    >
                      {busy ? "Saving…" : "Save profile"}
                    </button>
                  </div>
                </form>
              ) : (
                <div className="dialog-footer">
                  <button
                    disabled={
                      busy ||
                      detailsPending ||
                      profileDiscovery.failed ||
                      !detailProfiles.length
                    }
                    onClick={() => {
                      setSelectedProfile(
                        storedProfileAvailable
                          ? String(profileDetails.profile)
                          : "",
                      );
                      setProfileEditing(true);
                    }}
                  >
                    Change profile
                  </button>
                </div>
              ))}
            {!profileEditable && <p className="info-note">{profileDetails.state !== "stopped" ? "Stop and reconcile this workspace before changing its compute profile." : "Externally managed or archived workspace profiles are read-only."}</p>}
            <p className="form-intro">
              Profiles are managed through shared compute policy. Starting this
              workspace still checks member permissions, resource ceilings and
              reservations.
            </p>
            {groupApproval && (
              <div className="form-grid" data-testid="workspace-gpu-approval">
                <div className="full-width">
                  <h3>Group GPU approval</h3>
                  <p role="status">
                    {approvedGroup
                      ? groupApproval.ready === true
                        ? "Approved configuration is loaded by both Hubs, the gateway and the GPU node."
                        : "Approval saved. Activation is waiting for both Hubs, the gateway and the GPU node."
                      : "This workspace does not have a GPU approval."}
                  </p>
                  <p className="form-intro">
                    Stop the workspace before changing its approval. Approval
                    checks the course ceiling, neutral account, storage and
                    reviewed member identities. Members also need current
                    compute grants before Start; each member contributes one
                    interactive allowance.
                  </p>
                </div>
                <label>
                  Qualified shared GPU profile
                  <select
                    value={approvalProfile}
                    onChange={(e) => setApprovalProfile(e.target.value)}
                    disabled={busy}
                  >
                    <option value="">Select profile</option>
                    {qualifiedProfiles.map((profile) => (
                      <option key={profile} value={profile}>
                        {profile}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="dialog-actions">
                  <button
                    disabled={
                      busy ||
                      !approvalProfile ||
                      profileDetails.state !== "stopped" ||
                      profileDetails.archived === true ||
                      profileDetails.archive_pending === true
                    }
                    onClick={() => void updateGroupApproval()}
                  >
                    {busy ? "Saving…" : "Approve GPU profile"}
                  </button>
                  {approvedGroup && (
                    <button
                      disabled={busy || profileDetails.state !== "stopped"}
                      onClick={() => void updateGroupApproval(true)}
                    >
                      Revoke GPU approval
                    </button>
                  )}
                </div>
                {qualifiedProfiles.length === 0 && (
                  <p className="full-width">
                    No shared GPU profiles are currently qualified for approval.
                  </p>
                )}
                {profileDetails.state !== "stopped" && (
                  <p className="full-width">
                    Stop and reconcile this workspace before changing GPU
                    approval.
                  </p>
                )}
                {approvalError && (
                  <p className="full-width" role="alert">
                    {approvalError}
                  </p>
                )}
              </div>
            )}
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
