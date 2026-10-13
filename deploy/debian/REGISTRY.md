# Unifi Camera Bridge — Debian installer

This small installer pulls `IMAGE_REFERENCE` from GitHub Container Registry.
Supports Debian 12/13 on x64 (amd64) and ARM64 hosts/VMs with systemd. Internet
access is required. Public images need no GitHub account or Docker login.

## Install or migrate an existing installer-managed deployment

Extract the downloaded release package and run:

```sh
tar -xzf unifi-camera-bridge-RELEASE_VERSION-debian-installer.tar.gz
cd unifi-camera-bridge-RELEASE_VERSION-debian
sudo ./install.sh
```

Docker and Compose are installed when needed. The image for the host CPU is
pulled automatically. The installer prints the web interface address; finish
first-time setup there. `--check`, `--bind-address LAN_IP` and `--admin-port PORT`
remain available. An update retains the existing bind address, ports, camera
identities, admin login and persistent settings volume.

Existing installer-managed deployments can run this installer directly. It moves
`/opt/sonoff-protect-bridge` or `/opt/unifi-protect-bridge` to
`/opt/unifi-camera-bridge` and switches the Docker project/container to
`unifi-camera-bridge`. Settings are copied to `unifi-camera-bridge-data` while the
old container is stopped. Login details, camera identities, address and ports
are preserved. The original volume remains as a recovery backup; protect it as
it contains camera credentials. A failed migration attempts to restart the
original deployment and retains both settings copies for review.

If multiple installation directories or a destination container/volume already
exist, the installer stops without overwriting them. Unmanaged Docker/QNAP
installations are not taken over automatically. When updating these, retain the
existing settings volume and project until you have arranged a settings migration.

## Update

The default `stable` image tag follows published releases. In a host terminal:

```sh
cd /opt/unifi-camera-bridge
sudo docker compose -p unifi-camera-bridge pull manager
sudo docker compose -p unifi-camera-bridge up -d --no-build manager
```

The pull happens while the current bridge runs. Recreating the container briefly
interrupts video and clears browser sessions. Keep the settings volume; never
add `--volumes` or `-v` to a remove/down command unless deleting settings is intended.
Plain Compose updates do not automatically roll back or back up settings. Keep a
secure backup before upgrades, especially when release notes mention migrations.
The camera application has no access to the Docker socket.

## Pin a release or roll back

Change the image tag in `/opt/unifi-camera-bridge/compose.yaml` from `stable` to a
numbered release (for example `1.6.5`), then run the same pull/up commands. Older
versions may require a matching settings backup if a release changes its format.
Do not change the project name or named data volume.

Rerunning an installer resets the image reference to the one in that installer.
If an installer-driven deployment fails its health check, the installer attempts
to restart the exact previous image ID. Settings are retained; schema migrations
are not automatically reversed. Registry/pull failures leave the running bridge
untouched. Previous images should be kept until an update has been checked.

## Network and data

Admin defaults to port 18080; ONVIF uses 8100–8115 and RTSP uses 8600–8615. Debian
binds these to the selected private LAN IPv4. Keep the address stable. Complete
first-admin setup promptly on a trusted network. The installer does not alter
router or firewall policy. Credentials stay in the protected settings volume
`unifi-camera-bridge-data`; release images contain no camera settings.

Use the application's Logs screen for camera issues, or `sudo docker logs --tail
100 unifi-camera-bridge` for startup troubleshooting. The installer is for
Debian, not native QNAP QTS. Container Station can use the same GHCR image with
the documented ports and persistent volume.
