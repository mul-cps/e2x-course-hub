# Archive compatibility release candidate

This branch adds the no-argument filesystem provider
`e2x_course_hub.cps.filesystem.CourseServiceFilesystemAdapter` for the existing
`CourseServiceApp.filesystem_adapter_class` setting. It preserves the old SDK
constructor, provisioning method and authenticated HTTPS transport. Archive
partitions exact retained workspace records by group and requires every
read-only proof before returning aggregate success.

The base is deployed image `ghcr.io/mul-cps/e2x-course-hub@sha256:d475f8ff0d9c3075e3c995020c95ff16217eed5851b667a371a7510b7645daa2`.
The source tree is a070050 plus the provider, with the two already-installed
workspaces/app overlays retained exactly from that image. All 51 installed
Python modules are byte-identical except the package version; the provider is
one additional module. The wheel does not own any SDK or Lab-extension files.
The six served UI assets are preserved byte-for-byte; their SHA-256 manifest
is committed beside this document. Their source provenance remains the base
image's existing UI revision label, not an unperformed frontend rebuild.

Build with Python build/hatchling and `HATCH_BUILD_NO_HOOKS=true`, after placing
the exact base image's `/usr/local/share/e2x_course_hub/` assets under
`share/e2x_course_hub/` and verifying the complete manifest. This intentionally
skips rebuilding an unchanged UI. The Dockerfile verifies the resulting wheel
hash, installs without dependency resolution, and runs pip check. It requires
a build context containing that Dockerfile and the verified wheel.

No database schema migration is introduced. The new provider must be selected
explicitly in each console's configuration; its import requires the compatible
compute SDK (declared as the `compute-storage` extra). Archive retry also needs
the matched gateway storage-reverification backport. Until that gateway and
provider are deployed and exercised together, this remains a candidate.

Roll back the console image and provider class setting together; the prior
image does not contain this provider. Preserve the matching database and never
re-provision an archived group as part of rollback. Actual archive is read-only
and retains data; no deletion action is added.
