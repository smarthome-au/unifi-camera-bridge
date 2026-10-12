# Publishing Unifi Camera Bridge on GitHub

## First publication

Use a public repository named `unifi-camera-bridge`. Push this source directory
as the repository root, including `.github/workflows`, `.gitignore`, tests and
deployment scripts. Do not publish runtime settings, local support archives,
credential files or the historical `VALIDATION.txt` log.

GitHub Actions uses the repository's own `GITHUB_TOKEN`; no personal registry
password is saved as a workflow secret. The image name is derived from the
lowercase repository owner/name, for example `ghcr.io/owner/unifi-camera-bridge`.
The publish job has package-write and release-write permission; test jobs are
read-only. Actions are pinned to verified commit IDs.

On `main` pushes and pull requests, tests run without publishing. A `vX.Y.Z` tag
runs the tests, checks that the tag matches `manager.VERSION`, and builds
`linux/amd64` and `linux/arm64`. Both architectures are inspected and started
before the resulting digest is promoted to `stable`. The numbered image tag,
OCI version/source/revision labels, provenance and SBOM accompany the release.

Create the first release after the main-branch tests pass:

```sh
git tag v1.6.3
git push origin v1.6.3
```

The workflow publishes the numbered image and `stable`, creates a GitHub Release,
and attaches a small Debian installer, checksum and portable/QNAP Compose files.
GitHub packages initially may be private even for a public repository. After the
first publish, open the container package's settings and set visibility to Public.
Verify pulling both CPU variants without registry credentials before announcing
installation availability. Repository visibility and package visibility are
separate settings.

## Future releases

Update `manager.VERSION`, the Dockerfile default `VERSION`, source Compose tag
and relevant release documentation together. Commit to main, wait for tests,
then push the matching `vX.Y.Z` tag. Never reuse a numbered release tag or move it
to a new commit. Publish versions in increasing order: each successful release
moves `stable`. This workflow currently accepts stable three-part versions, not
prerelease tags. Package/release creation still depends on the account's Actions
and package-publishing policies.

The workflow checks container startup/imports on both CPUs. It does not validate
live cameras or execute a full Debian VM installation on every release.

## Local packaging

After the image has been published, produce a registry installer without images:

```sh
python3 scripts/package-registry-installer.py --image ghcr.io/OWNER/unifi-camera-bridge:stable dist
python3 scripts/package-registry-compose.py --image ghcr.io/OWNER/unifi-camera-bridge:stable dist
```

Replace OWNER with the actual lowercase account/organisation. The installer
contains a validated reference, checksums and deployment scripts, not a Docker
image. For a fixed-version installer, replace `stable` with a published version
or use its `@sha256:...` digest. An explicit fixed reference does not track later
releases when `docker compose pull` runs.

Existing offline packaging scripts remain usable. Debian registry upgrades reuse
the old installer-managed container and settings volume. A failed pull leaves
the old application running; a failed installer deployment attempts to restart
the previous image ID, even if `stable` now points at the failed new image.
Direct `docker compose pull/up` does not include this installer rollback behavior.
