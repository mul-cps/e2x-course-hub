FROM ghcr.io/mul-cps/e2x-course-hub@sha256:b6856fa9c0ac1ddd42541c5bb629ee31ff8a5fd7420cd152548d6fdc8edde7b7
ARG SOURCE_REVISION
ARG WORKSPACES_SHA256
LABEL org.opencontainers.image.source="https://github.com/mul-cps/e2x-course-hub" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.mul-cps.source.stopped-absent-shares-base="sha256:b6856fa9c0ac1ddd42541c5bb629ee31ff8a5fd7420cd152548d6fdc8edde7b7" \
      io.mul-cps.source.workspaces-sha256="${WORKSPACES_SHA256}"
COPY workspaces.py /usr/local/lib/python3.12/site-packages/e2x_course_hub/cps/workspaces.py
