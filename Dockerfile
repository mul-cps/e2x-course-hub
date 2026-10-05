# Official Docker Hub manifest indexes resolved 2026-10-05.
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS frontend
WORKDIR /src/course-service-ui
COPY course-service-ui/package*.json ./
RUN npm ci
COPY course-service-ui/ ./
RUN npm run build

FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
ARG COMPUTE_WHEEL=""
ARG COMPUTE_WHEEL_SHA256=""
COPY compute-artifacts/ compute-artifacts/
COPY pyproject.toml README.md ./
COPY e2x_course_hub/ e2x_course_hub/
COPY share/ share/
COPY --from=frontend /src/share/e2x_course_hub/static/ share/e2x_course_hub/static/
# Frontend is already built, so package without re-running the Node hook.
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 && python -m pip install --no-cache-dir hatchling hatch-jupyter-builder && HATCH_BUILD_NO_HOOKS=true python -m hatchling build -t wheel && python -m pip wheel --wheel-dir /wheels pip==26.2.1 dist/*.whl

RUN python compute-artifacts/verify_wheel.py "$COMPUTE_WHEEL" "$COMPUTE_WHEEL_SHA256"

FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
COPY --from=builder /wheels/ /wheels/
RUN python -m pip install --no-cache-dir --no-index --no-deps /wheels/pip-26.2.1-py3-none-any.whl && python -m pip install --no-cache-dir --no-index --no-deps /wheels/*.whl && rm -rf /wheels && useradd --uid 10001 --create-home console && mkdir /data && chown console:console /data
USER 10001:10001
WORKDIR /data
ENV E2X_COURSE_HUB_CONFIG=/data/config.yml
VOLUME ["/data"]
CMD ["python", "-m", "e2x_course_hub.course_service.app", "--config=/etc/console/app.py"]
