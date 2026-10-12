# Unifi Camera Bridge — Docker installation

Release 1.6.1, packaging revision 2 (classic Docker archive). Packages contain the application only: no saved cameras, admin
password or recordings. This is a Linux container.

## Choose a package

| Host | Package |
| --- | --- |
| QNAP TS-464; other Intel/AMD 64-bit Docker hosts | amd64 (x64) |
| Apple Silicon Docker Desktop; compatible 64-bit ARM Linux hosts | arm64 |

32-bit ARM NAS models and Windows containers are not supported. Docker Desktop
on Windows must use Linux containers. QNAP instructions target Container Station
3; hardware-specific QNAP validation remains to be completed. x64 is tested under
emulation on the development Mac, not on a physical x64 NAS.

## QNAP TS-464: Container Station

1. Unzip the **amd64** package on your computer. In Container Station, open
   **Images → Import** and select `image.tar.gz`. Use image import, not container
   import. The imported tag is `local/sonoff-protect-bridge:1.6.1-amd64`.
2. Open `compose.qnap.yaml` and copy its contents. No IP address edits are needed.
3. In **Applications → Create**, name the application `sonoff-protect-bridge`,
   paste the YAML, validate and create. Use the imported local image;
   disable any option to pull an image first. No registry account is needed.
4. Open `http://YOUR_NAS_IP:18080`. Port 18080 avoids the usual QNAP admin port.
5. Choose an admin password. The setup page automatically fills **Bridge host
   address** from the NAS LAN IP used in the browser URL. Check the address and
   create the manager; no one-time code or container terminal is needed. Use a
   DHCP reservation/static address for the NAS.
6. Add a camera, inspect it and save. In Protect use **Advanced Adoption** with
   the address copied from each camera view and that camera's ONVIF credentials.
   Verify HQ/LQ (where present), audio, motion recording and PTZ (where supported).

The QNAP template makes port 18080 available on all NAS IPv4 interfaces.
Complete initial setup promptly: the first visitor who can reach the page can
create the admin account. After setup, the admin password is required.

The named volume `sonoff-protect-bridge-data` holds settings. It is created with
permissions for the non-root application user. Avoid substituting an ordinary
NAS shared-folder mount without first granting UID/GID 65532 write access.
There is no need for privileged mode, host networking, USB or GPU passthrough.
LQ codec conversion uses the CPU; monitor NAS load as you add views.

## Other Docker hosts

Unzip the matching architecture package. In its directory:

```sh
docker load -i image.tar.gz
cp env.example .env
```

Edit `.env`: for remote administration set `ADMIN_BIND_IP` to the Docker host's
LAN IP. Then:

```sh
docker compose up -d --no-build
```

Open `http://HOST_IP:18080` (or localhost for the default loopback binding).
Choose an admin password. Opening via a private LAN IPv4 address fills the bridge
address automatically. When using localhost, a hostname or a reverse proxy, check
or enter the actual address Protect should reach; Docker internals are not used.
This changes setup suggestions only, not the Compose network binding.
Complete setup as described above. Compose v2 with `pull_policy: never` support
is required. The image is already built; the NAS need not compile anything.

## Network and firewall

- Admin: TCP 18080 → container 8080. The QNAP template binds to all NAS IPv4
  interfaces; the general Docker template defaults to loopback. Use trusted
  networks and do not forward it from the internet. It uses HTTP; use an HTTPS
  reverse proxy if needed.
- Protect to bridge: TCP 8100–8115 (ONVIF/snapshots/events) and 8600–8615 (RTSP).
  These port numbers must match on both sides of Docker's mapping.
- Bridge to cameras: their configured ONVIF and RTSP ports (usually 80 and 554).
  Routed subnets work when the firewall and routes allow both connections.
- WS-Discovery multicast is not provided. Add bridge views manually in Protect.
- No camera streams or credentials are embedded in this package.

If a published port is occupied, stop and resolve the conflict before starting.
The admin port may change in `.env` or the QNAP YAML. To move camera port ranges,
change their matching base environment variables and published ranges together.
Maximum default capacity is 16 views, not necessarily 16 physical cameras.

## Updating and keeping settings

Import/load the new image, update the image tag in the same application's YAML
and recreate that application while retaining `sonoff-protect-bridge-data`.
In CLI installations run `docker compose up -d --no-build` after updating the tag.
Keep the same volume and endpoint ports. Restarting briefly interrupts streams
and clears browser sign-in sessions. Do not delete the volume or use `down -v`.

## Moving an existing bridge

A fresh install starts empty. To retain existing camera identities, names and
logins, transfer the existing `/data` volume separately; do not add it to the
redistributable image. A backup contains camera credentials: store and transfer
it securely. This package has not copied or stopped the existing Mac bridge.

For the current Mac Compose installation, a planned cutover can use:

```sh
# Run in the existing source/Compose directory on the Mac.
docker compose stop manager
umask 077
docker compose run --rm --no-deps -T --entrypoint tar manager -C /data -czf - . > bridge-data.tgz
# If the cutover is delayed, restart the original with: docker compose start manager
```

Transfer `bridge-data.tgz` securely to the destination. Before initial setup on
the destination (image loaded, release directory current):

```sh
docker compose run --rm --no-deps -T --entrypoint tar manager -C /data -xzf - < bridge-data.tgz
docker compose up -d --no-build
```

These commands restore into a **fresh, empty** volume. Do not overwrite an
already configured destination; stop and back it up first. For a QNAP deployment
created from `compose.qnap.yaml`, use `docker compose -f compose.qnap.yaml` instead.
The same named volume is used by both templates. The Docker CLI may require an
administrator SSH session on QNAP.

Sign in using the existing admin password and change **Bridge address** to the
new host's LAN IP. Endpoint identities and ports are preserved, but Protect's
old IP associations may need updating/reconnection. Do not remove/re-adopt
cameras without first considering recording retention. Keep the original bridge
stopped during cutover to avoid duplicate endpoints with the same identities.

## Troubleshooting

- **Wrong format on image import:** use **Images → Import**, not Containers, and
  select the inner `image.tar.gz` rather than the ZIP. Packaging revision 2 uses
  a classic Docker archive with legacy metadata. If needed, decompress it to
  `image.tar` and import that file. Earlier kits used an OCI-style export that
  can be rejected by NAS importers.
- **No matching manifest / pull denied:** import `image.tar.gz` first, select the
  matching architecture and use the exact imported image tag; disable pulling.
- **exec format error:** wrong architecture. TS-464 needs amd64, not arm64.
- **Cannot open admin page:** verify NAS IP, binding, firewall and TCP 18080.
- **Permission denied under /data:** use the supplied named volume or correct
  ownership on a custom bind mount (UID/GID 65532).
- **Healthy container but no video:** health checks cover the manager only.
  Inspect **Logs**, test camera access and check routing/advertised bridge IP.
- **No automatic address:** open the manager using the NAS/host LAN IPv4 address,
  or enter it manually. Saved addresses are preserved during upgrades.
- **Setup page no longer shown:** an admin account already exists; sign in.

Detailed camera behavior and limitations are in `APPLICATION.md`.

References: [QNAP TS-464 specifications](https://www.qnap.com/en/product/ts-464/specs/package),
[Container Station 3 guide](https://www.qnap.com/en-us/how-to/tutorial/article/how-to-use-container-station-3),
[Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/).
