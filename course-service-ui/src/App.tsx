import { Routes, Route, Link } from "react-router-dom";
import CoursesPage from "./components/CoursesPage";
import CourseDetailPage from "./components/CourseDetailPage";
import ProfileDetailsPage from "./components/ProfileDetailsPage";

function App() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="bg-hbrs-dark-blue text-primary-foreground p-4 px-8 shadow-md">
        <h1 className="text-2xl font-semibold m-0 text-neutral-100">
          Compute Platform Administration
        </h1>
        <nav className="flex gap-4 mt-3" aria-label="Administration">
          <Link to="/">Courses</Link>
          <Link to="/integrations">Integrations</Link>
        </nav>
      </header>
      <main className="flex-1 p-8 bg-muted/30">
        <Routes>
          <Route path="/integrations" element={<section>
            <h2 className="text-xl font-semibold">Integrations → Moodle</h2>
            <p>Planned / Not configured</p>
            <p>Local course management is active. Moodle integration is planned and is not deployed.</p>
            <a href="https://github.com/mul-cps/e2x-course-hub/blob/feat/cps-platform-v1/docs/cps-platform.md">Integration documentation</a>
          </section>} />
          <Route path="/" element={<CoursesPage />} />
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
  );
}

export default App;
