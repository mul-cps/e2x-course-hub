# Reviewed compute SDK build artifacts

Generic upstream console builds leave `COMPUTE_WHEEL` and
`COMPUTE_WHEEL_SHA256` empty and do not install the CPS compute SDK.
CPS/CIT platform qualification requires a compatible, reviewed released SDK wheel
so `cps_compute.storage.HTTPFilesystemAdapter` can import inside the console image.

The release pipeline must fetch that exact wheel and its independently reviewed
SHA256 into this directory, then supply both Docker build arguments:

```sh
podman build --build-arg COMPUTE_WHEEL=cps_compute-0.1.0-py3-none-any.whl \
  --build-arg COMPUTE_WHEEL_SHA256=00f81cc1153384b7f3376fd1f7929c63cdd285d0fbb3840e843adfefb660bf9b \
  -t console-qualified .
```

The example digest identifies the locally reviewed 2026-10-05 SDK artifact; it is
not a claim of a published release. Future releases must use their reviewed wheel
and digest, with console/SDK interface compatibility qualification. Wheels are
ignored; README and verification code are tracked. Never silently replace the
specified wheel with a PyPI `cps-compute` package. Its exact bytes and project name
are checked before dependency resolution. All SDK dependency wheels join the
builder's wheelhouse; the nonroot runtime installs only that wheelhouse offline.

The build fails for a missing digest, mismatched digest, non-basename filename,
missing artifact or a wheel whose metadata names another project. An absent SDK
keeps generic upstream builds usable, but must not qualify a deployment configured
with a `cps_compute` filesystem adapter.
