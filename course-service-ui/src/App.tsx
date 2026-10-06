import { Routes, Route, NavLink, Link, Navigate, useLocation, useSearchParams } from "react-router-dom";
import {
  LayoutDashboard,
  GraduationCap,
  FolderKanban,
  Cpu,
  Users,
  ClipboardList,
  History,
  Plug,
  ArrowUpRight,
  ShieldCheck,
  ChevronRight,
  Menu,
  X,
} from "lucide-react";
import { config } from "./config";
import AdminRecords from "./components/AdminRecords";
import CoursesTable from "./components/CoursesTable";
import CourseDetailPage from "./components/CourseDetailPage";
import ProfileDetailsPage from "./components/ProfileDetailsPage";
import { useState } from "react";
const navigation = [
  { path: "/", label: "Overview", icon: LayoutDashboard },
  { path: "/courses", label: "Courses", icon: GraduationCap },
  { path: "/projects", label: "Projects", icon: FolderKanban },
  { path: "/compute", label: "Compute access", icon: Cpu },
  { path: "/workspaces", label: "Shared workspaces", icon: Users },
  { path: "/assignments", label: "Assignments", icon: ClipboardList },
  { path: "/audit", label: "Audit log", icon: History },
  { path: "/integrations", label: "Integrations", icon: Plug },
];
function Overview() {
  return (
    <section className="admin-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Teaching & research</p>
          <h1>Welcome, {config.user.name}</h1>
          <p>
            Manage teaching, collaboration and compute access from one place.
          </p>
        </div>
        <Link className="primary-button" to="/courses">
          Manage courses <ChevronRight size={16} />
        </Link>
      </div>
      <div className="section-heading"><h2>Quick access</h2><span>Teaching, research and collaboration</span></div>
      <div className="overview-grid">
        {navigation.slice(1, 7).map(({ path, label, icon: Icon }) => (
          <Link className="overview-card" key={path} to={path}>
            <div className="card-icon">
              <Icon size={22} />
            </div>
            <ArrowUpRight className="card-arrow" size={19} />
            <h2>{label}</h2>
            <p>
              {
                {
                  "/courses":
                    "Create courses, manage terms and enroll members.",
                  "/projects": "Organize research and teaching projects.",
                  "/compute":
                    "Inspect profiles and manage time-bounded grants.",
                  "/workspaces":
                    "Manage shared notebooks and approved compute profiles.",
                  "/assignments":
                    "Allocate collaboration groups and close assignments.",
                  "/audit": "Review administrative changes and their outcomes.",
                }[path]
              }
            </p>
            <span>
              Open {label.toLowerCase()} <ChevronRight size={14} />
            </span>
          </Link>
        ))}
      </div>
      <div className="overview-footer">
        <div className="info-card">
          <ShieldCheck size={22} />
          <div>
            <h2>Permissions stay scoped</h2>
            <p>
              Each console manages its own records. Shared policy enforces
              compute limits; all changes are audited.
            </p>
            <Link to="/audit">Review audit log →</Link>
          </div>
        </div>
        <div className="info-card">
          <Plug size={22} />
          <div>
            <h2>Local course management is active</h2>
            <p>
              Manage courses locally. Moodle integration is planned and remains disabled.
            </p>
            <Link to="/integrations">View integrations →</Link>
          </div>
        </div>
      </div>
    </section>
  );
}
function Courses() {
  const [params, setParams] = useSearchParams();
  const allowedTabs = ["courses", "terms", "memberships", "groups", "groupings", "servers"];
  const tab = allowedTabs.includes(params.get("tab") ?? "") ? params.get("tab")! : "courses";
  const setTab = (next: string) => setParams(next === "courses" ? {} : {tab: next});
  return (
    <>
      <div className="section-tabs" aria-label="Course views">
        {[
          "courses",
          "terms",
          "memberships",
          "groups",
          "groupings",
          "servers",
        ].map((t) => (
          <button
            aria-pressed={tab === t}
            className={tab === t ? "active" : ""}
            key={t}
            onClick={() => setTab(t)}
          >
            {
              {
                courses: "Courses",
                terms: "Terms",
                memberships: "Members",
                groups: "Groups",
                groupings: "Groupings",
                servers: "Hub course servers",
              }[t]
            }
          </button>
        ))}
      </div>
      {tab === "servers" ? (
        <section className="admin-page">
          <div className="page-heading">
            <div>
              <p className="eyebrow">Course infrastructure</p>
              <h1>Hub course servers</h1>
              <p>Manage existing course server configurations and profiles.</p>
            </div>
          </div>
          <CoursesTable />
        </section>
      ) : (
        <AdminRecords
          key={tab}
          kind={
            tab as "courses" | "terms" | "memberships" | "groups" | "groupings"
          }
        />
      )}
    </>
  );
}
function App() {
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const current = navigation.find(item => item.path === location.pathname)?.label ?? (location.pathname.startsWith("/course/") ? "Course details" : "Administration");
  return (
    <div className="admin-shell">
      <aside className={`admin-sidebar ${menuOpen ? "menu-open" : ""}`}>
        <Link to="/" className="brand">
          <div className="brand-mark">
            <Cpu size={23} />
          </div>
          <div>
            Compute Platform<span>Administration</span>
          </div>
        </Link>
        <button className="mobile-menu-toggle" aria-label={menuOpen ? "Close navigation" : "Open navigation"} aria-expanded={menuOpen} aria-controls="admin-navigation" onClick={() => setMenuOpen(!menuOpen)}>{menuOpen ? <X size={20}/> : <Menu size={20}/>}</button>
        <p className="nav-label">WORKSPACE</p>
        <nav id="admin-navigation" aria-label="Administration">
          {navigation.map(({ path, label, icon: Icon }) => (
            <NavLink end={path === "/"} key={path} to={path} onClick={() => setMenuOpen(false)} className={({isActive}) => isActive || (path === "/courses" && location.pathname.startsWith("/course/")) ? "active" : ""}>
              <Icon size={18} />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-note">
          <ShieldCheck size={17} />
          <span>Course permissions and shared compute policy</span>
        </div>
        <div className="account-card">
          <span className="avatar">
            {config.user.name.slice(0, 2).toUpperCase()}
          </span>
          <div>
            <strong>{config.user.name}</strong>
            <span>{config.user.admin ? "Administrator" : "Course member"}</span>
          </div>
        </div>
      </aside>
      <div className="admin-body">
        <header className="topbar">
          <span>
            <Link to="/">Administration</Link> <ChevronRight size={14} /> <strong>{current}</strong>
          </span>
          <a href={`${config.baseUrl.split("/services/")[0]}/hub/home`}>
            Back to JupyterHub <ArrowUpRight size={14} />
          </a>
        </header>
        <main id="main-content">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/courses" element={<Courses />} />
            <Route path="/local" element={<Navigate to="/courses" replace />} />
            {(
              [
                "projects",
                "compute",
                "workspaces",
                "assignments",
                "audit",
              ] as const
            ).map((kind) => (
              <Route
                key={kind}
                path={`/${kind}`}
                element={<AdminRecords key={kind} kind={kind} />}
              />
            ))}
            <Route
              path="/integrations"
              element={
                <section className="admin-page">
                  <div className="page-heading">
                    <div>
                      <p className="eyebrow">Connections</p>
                      <h1>Integrations</h1>
                      <p>External integrations and their availability.</p>
                    </div>
                  </div>
                  <div className="integration-card">
                    <div className="card-icon">
                      <GraduationCap size={24} />
                    </div>
                    <div>
                      <h2>Moodle</h2>
                      <span className="status-pill">
                        Planned / Not configured
                      </span>
                      <p>
                        Local course management is active. Moodle integration is
                        planned and is not deployed.
                      </p>
                      <a
                        href="https://github.com/mul-cps/e2x-course-hub/blob/feat/cps-platform-v1/docs/cps-platform.md"
                        target="_blank"
                        rel="noreferrer"
                      >
                        Read integration documentation ↗
                      </a>
                    </div>
                  </div>
                </section>
              }
            />
            <Route
              path="/course/:courseId/:termId"
              element={<CourseDetailPage />}
            />
            <Route
              path="/course/:courseId/:termId/profiles"
              element={<ProfileDetailsPage />}
            />
          </Routes>
        </main>
      </div>
    </div>
  );
}
export default App;
