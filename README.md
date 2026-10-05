# e2x Course Hub

A comprehensive JupyterHub extension providing advanced course management, profile configuration, and dynamic spawning capabilities for educational environments. This system enables role-based access control, flexible resource allocation, and seamless integration with KubeSpawner for Kubernetes-based deployments.

## Overview

`e2x-course-hub` is designed for educational institutions running JupyterHub in Kubernetes environments. It provides:

- **Dynamic Profile Management**: Configure and manage multiple profiles per course/term with runtime overrides
- **Course Service**: Web-based UI for managing course members and permissions
- **KubeSpawner Integration**: Custom hooks for profile lists and pre-spawn configuration
- **Role-Based Access Control**: Fine-grained permissions for students, graders, and course admins
- **Mount Management**: Flexible volume mount definitions with placeholder support
- **User Management**: Automatic JupyterHub user creation and group management

## Key Features

### Course Management
- **Multi-term Support**: Manage multiple active terms per course
- **Profile Configuration**: Define course-specific profiles with runtime overrides
- **Metadata**: Rich course information including descriptions and maintainer contacts
- **Member Management**: Add/remove students and graders with appropriate permissions

### Profile System
- **Inheritance**: Profiles can inherit from base profiles with selective overrides
- **Runtime Configuration**: Specify images, resources, environment variables, and more
- **Parameterization**: Use placeholders for dynamic value injection (username, course_id, term_id)
- **Mount Requests**: Define volume mounts per profile with flexible mount definitions

### Web Interface
- **Modern React UI**: Built with React 19, TypeScript, and TailwindCSS
- **Course Overview**: View all accessible courses and their details
- **Member Management**: Add/remove course members with role-based permissions
- **Profile Details**: View detailed profile configurations

### KubeSpawner Integration
- **Profile List Hook**: Generates dynamic profile lists based on user permissions
- **Pre-spawn Hook**: Configures volume mounts and other settings before spawning
- **Term Sorting**: Customizable sorting for term organization (WS25, SS25, etc.)

## Architecture

```
e2x_course_hub/
├── api/                    # API layer
│   ├── api.py             # Central API aggregator
│   ├── course_api.py      # Course management API
│   ├── profile_api.py     # Profile resolution API
│   └── hub_api.py         # JupyterHub API integration
├── course_service/        # Web service
│   ├── app.py            # Tornado application
│   └── handlers/         # Request handlers
├── schema/               # Pydantic models
│   ├── course.py        # Course configuration schemas
│   ├── profile.py       # Profile schemas
│   ├── runtime.py       # Runtime/resource schemas
│   ├── mount.py         # Volume mount schemas
│   └── user.py          # User/role schemas
├── kubespawner_hooks.py  # KubeSpawner integration
└── mocks/               # Mock data for testing

course-service-ui/        # React frontend
├── src/
│   ├── api/             # API client
│   ├── components/      # React components
│   └── hooks/           # Custom React hooks
```

## Installation

### From Git

```bash
pip install git+https://github.com/Digiklausur/e2x-course-hub.git
```

### For Development

```bash
git clone https://github.com/Digiklausur/e2x-course-hub
cd e2x-course-hub
pip install -e ".[dev]"
```

### Building the Frontend

```bash
cd course-service-ui
npm install
npm run build
```

The built assets are automatically included in the Python package via `hatch-jupyter-builder`.

## Configuration

### Server Configuration File

Create a YAML configuration file (e.g., `config.yml`):

```yaml
profile_dir: "/path/to/profiles"
course_config_dir: "/path/to/courses"
mount_definitions_file: "/path/to/mount_definitions.yaml"

roles:
  student:
    priority: 100
    scopes: 
      - spawn:profile!profile=student_profile
  grader:
    priority: 200
    scopes:
      - spawn:profile!profile=grader_profile
      - add:course-members!role=student
      - remove:course-members!role=student
  course_admin:
    priority: 300
    scopes:
      - add:course-members
      - remove:course-members
```

### Course Configuration

Create YAML files in your `course_config_dir`:

```yaml
# courses/AMR.yaml
metadata:
  course_id: AMR
  course_name: Autonomous Mobile Robots
  description: Advanced robotics course

profile_runtime_overrides:
  grader_profile:
    image:
      name: ghcr.io/my-org/teacher-notebook
      tag: latest

terms:
  WS25:
    allowed_profiles:
      - grader_profile
      - student_profile
    profile_runtime_overrides:
      student_profile:
        resources:
          limits:
            memory: "2Gi"
```

### Profile Configuration

Create YAML files in your `profile_dir`:

```yaml
# profiles/student.yaml
name: student_profile
display_name: "Student Profile"
inherits: base_profile

inputs:
  username:
    required: true
    type: string
  course_id:
    required: true
    type: string
  term_id:
    required: true
    type: string

runtime:
  image:
    name: jupyter/datascience-notebook
    tag: latest
  environment:
    NBGRADER_COURSE_ID: "${{inputs.course_id}}-${{inputs.term_id}}"
  resources:
    limits:
      memory: "1Gi"
      cpu: "1"

mount_requests:
  home:
    id: student_home
    args:
      username: "${{ inputs.username }}"
      course_id: "${{ inputs.course_id }}"
```

### Mount Definitions

Define reusable mount patterns:

```yaml
# mount_definitions.yaml
student_home:
  inputs: 
    username:
      required: true
      type: string
    course_id:
      required: true
      type: string
  description: "Student home directory"
  name: "home-${{inputs.username}}"
  mountPath: "/home/jovyan"
  subPath: "${{inputs.course_id}}/${{inputs.username}}"
```

## Usage

### As a JupyterHub Service

```python
# jupyterhub_config.py
c.JupyterHub.services = [
    {
        'name': 'course-service',
        'url': 'http://localhost:10101',
        'command': [
            'python', '-m', 'e2x_course_hub.course_service.app',
            '--CourseServiceApp.server_config_file=/etc/jupyterhub/config.yml',
            '--CourseServiceApp.port=10101',
            '--CourseServiceApp.add_users_to_hub=True'
        ],
        'environment': {
            'E2X_COURSE_HUB_CONFIG': '/etc/jupyterhub/config.yml'
        }
    }
]

c.JupyterHub.load_roles = [
    {
        'name': 'course-service',
        'services': ['course-service'],
        'scopes': [
            'access:services!service=course-service',
            'admin:users',
            'list:users',
            'read:users',
            'admin:groups',
        ]
    }
]
```

### KubeSpawner Integration

```python
# jupyterhub_config.py
from e2x_course_hub.kubespawner_hooks import (
    get_profile_list_hook,
    get_pre_spawn_hook,
    configure_autospawn
)

from e2x_course_hub.course_service._data import KUBESPAWNER_TEMPLATE_PATH, JUPYTERHUB_TEMPLATE_PATH

# Configure KubeSpawner
c.JupyterHub.spawner_class = 'kubespawner.KubeSpawner'

# Add hooks
c.KubeSpawner.profile_list = get_profile_list_hook(
    server_config_file='/etc/jupyterhub/config.yml'
)
c.KubeSpawner.pre_spawn_hook = get_pre_spawn_hook(
    server_config_file='/etc/jupyterhub/config.yml'
)

# Configure JupyterHub and the KubeSpawner to use the templates from e2x_course_hub
c.KubeSpawner.additional_profile_form_template_paths = [KUBESPAWNER_TEMPLATE_PATH]

c.JupyterHub.template_paths = [JUPYTERHUB_TEMPLATE_PATH]

# Optional: Configure autospawn behavior
# Automatically spawns the server if only one course/profile is available
configure_autospawn(
    c,
    auto_spawn_single_course=True,  # Enable auto-spawn for single course
    auto_spawn_countdown=5           # Countdown in seconds before spawning
)
```

#### Autospawn Configuration

The `configure_autospawn` function adds template variables to control automatic server spawning behavior:

- **`auto_spawn_single_course`** (bool): When `True`, automatically spawns the server if the user has access to only one course/profile. Default: `False`
- **`auto_spawn_countdown`** (int): Number of seconds to show a countdown before auto-spawning. Gives users time to cancel if needed. Default: `5`

This function safely merges with any existing `template_vars` in your configuration, preserving other template variables you may have set.

**Note**: For autospawn to work, you need custom JupyterHub templates that implement the autospawn logic using these template variables.

### Standalone Course Service

```python
from e2x_course_hub.course_service.app import CourseServiceApp

app = CourseServiceApp()
app.server_config_file = "/path/to/config.yml"
app.port = 10101
app.add_users_to_hub = True
app.initialize()
app.start()
```

## API Reference

### Course Service REST API

#### Courses
- `GET /api/courses` - List courses accessible to current user
- `GET /api/courses/<course_id>/<term_id>` - Get course details
- `POST /api/courses/<course_id>/<term_id>/leave` - Leave a course

#### Course Members
- `GET /api/courses/<course_id>/<term_id>/members` - List course members
- `POST /api/courses/<course_id>/<term_id>/members` - Add members to course
- `DELETE /api/courses/<course_id>/<term_id>/members` - Remove members from course

#### Profiles
- `GET /api/courses/<course_id>/<term_id>/profiles/<profile_id>` - Get profile details

#### Permissions
- `GET /api/courses/<course_id>/<term_id>/permissions` - Check user permissions
- `GET /api/courses/<course_id>/<term_id>/roles` - List assignable roles

### Python API

```python
from e2x_course_hub.api import API
from e2x_course_hub.api.hub_api import HubAPI
from e2x_course_hub.schema.user import User

# Initialize API
hub_api = HubAPI(api_token="...", api_url="...")
api = API(
    server_config_file="/path/to/config.yml",
    hub_api=hub_api,
    add_users_to_hub=True
)

# Get user's courses
user = User(username="alice", groups=["AMR.WS25.graders"])
courses = api.course_api.list_courses(user)

# Get resolved profile
profile = api.profile_api.get_profile(
    user=user,
    course_id="AMR",
    term_id="WS25",
    profile_id="student_profile"
)

# Add course members
api.course_api.add_course_members(
    user=user,
    course_id="AMR",
    term_id="WS25",
    role="student",
    usernames=["student1", "student2"]
)
```

## Development

### Running Mock Server

```bash
cd e2x_course_hub/mocks
python mock_server.py
```

Access at: http://localhost:8888

### Frontend Development

```bash
cd course-service-ui
npm run dev
```

### Code Quality

```bash
# Python linting
ruff check .

# Frontend linting
cd course-service-ui
npm run lint
npm run format
```

### Running Tests

```bash
pytest
```

## Environment Variables

- `JUPYTERHUB_SERVICE_PREFIX` - URL prefix for the service (set by JupyterHub)
- `JUPYTERHUB_API_TOKEN` - API token for JupyterHub authentication (set by JupyterHub)
- `JUPYTERHUB_API_URL` - JupyterHub API URL (set by JupyterHub)
- `E2X_COURSE_HUB_CONFIG` - Path to server configuration file

## Dependencies

### Python (>=3.8)
- `pydantic>=2.0` - Data validation and schema definition
- `jinja2` - Template rendering
- `PyYAML` - YAML configuration parsing
- `jupyterhub` - JupyterHub integration (runtime)
- `tornado` - Web framework (runtime)

### Frontend
- React 19 with TypeScript
- TailwindCSS 4 for styling
- TanStack Table for data tables
- Radix UI for accessible components
- React Router for navigation

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes with tests
4. Run linting and tests
5. Submit a pull request

## Support

- **Issues**: https://github.com/Digiklausur/e2x-course-hub/issues
- **Documentation**: https://github.com/Digiklausur/e2x-course-hub#readme
## Qualified local collaboration deployment contract

Shared workspace starts validate current central policy, register the source-owned
workspace, then POST only its fixed profile to Hub. GPU reservations are acquired
exclusively by the Hub pre-spawn adapter. Stops capture the source-owned reservation
attempt before requesting Hub shutdown, then ask the gateway to release that exact
attempt. The gateway independently verifies Hub and Kubernetes Pod absence; a 409
retains capacity. Console assertions cannot force release.

Each administrator-owned course `workspace_bindings` entry supplies exact
`group_id`, `hub_user`, `hub_server`, `namespace`, and `pod`. Preserve existing
Spawner-derived names, slugs, PVCs and NFS locations when qualifying these values.
Do not infer Pod names from usernames. Provision dedicated neutral Hub users through
operator-controlled Hub RBAC with role `cps-workspace-kernel`; the central gateway
verifies that role and rejects owners present in personal canonical mappings.
Browser visitors keep their existing usernames and separate identities. The console
does not grant this operator role itself.

Configure `CourseServiceApp.filesystem_adapter_class` to an operator-owned dotted
Python class import. Its async `provision(workspace, actor=...)` must provision or
verify the exact existing storage, ownership and writable group mount without
renaming or deleting files. Its async `archive(workspaces, actor=...)` must identify
**all** storage writers, prove their shutdown (including writers outside Hub),
apply read-only enforcement at the filesystem/storage boundary, test denied writes
through every retained mount, preserve all files and return a dict containing
`read_only_verified: true` plus nonempty `evidence` references. Partial failures
must be retryable and must not restore write access. The adapter is trusted operator
code, never browser input; an empty class fails closed. A DB flag is not filesystem
enforcement. Assignment closure first prevents restarts, stops all associated shared
servers and revokes Shares; missing/failed archive qualification returns 503 with
`archive_pending` retained. Explicit Stop does not clear an archive pending barrier.

`providers.reconcile(provider, sink)` passes normalized course/member/group/grouping
snapshots to the same `WorkspaceService.reconcile_snapshot(snapshot, actor=...)`
sink for local and future read-only providers. Reconciliation interrupts existing
visitors before membership/Shares recalculation and current registry updates. Missing
verified canonical members or operator bindings fail closed. Moodle remains visibly
planned: disabled initialization has no network/secrets/timers; enabling it fails
startup. No Moodle implementation is implied by a fake-provider test.

Person linkage requires separately verified institutional email (`verified: true`)
and explicit canonical UUID. Administrator review is additionally required for
existing email/UUID handover and does not replace verification. No SMTP is configured
or contacted, and no cross-Hub identity is automatically inferred. Schema v5 retains
legacy account/reference records but marks pre-v5 email proofs unverified until an
explicit verified mapping is reapplied. Shared writers using those mappings remain
blocked meanwhile.

Use distinct CPS and CIT deployment/config/SQLite paths and OAuth clients, callback
URLs, cookie paths and private tokens. Database ownership is persistent and opening
a CPS DB as CIT fails before migration. Before upgrade use the provider's SQLite
online `backup(path)` API into a restricted persistent backup volume; retain the
matching application image/config version and verify restoring it into an isolated
same-console DB. Never copy an open SQLite file without its journal. Current schema
is v5; older applications must not downgrade it.

The source-built `Dockerfile` uses digest-pinned official Python/Node manifest
indexes, builds frontend assets and a wheel, installs runtime wheels, runs as UID
10001, and persists `/data`. Mount a console-specific `/etc/console/app.py` read-only;
configure SQLite/config paths inside that console's `/data`. Supply secrets privately
and only route through existing Hub service endpoints. Run image qualification and
storage permissions/backup restore checks before deployment; this change does not
publish an image or deploy services.
