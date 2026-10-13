# Unifi Camera Bridge — Debian installer

This small installer pulls `IMAGE_REFERENCE` from GitHub Container Registry.
Supports Debian 12/13 on x64 (amd64) and ARM64 hosts/VMs with systemd. Internet
access is required. Public images need no GitHub account or Docker login.

## Install

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
identities, admin login and settings folder.

Settings are stored in `/opt/unifi-camera-bridge/data/config.json`, mounted into
the container at `/data`. This installer does not migrate older installations
or Docker volumes; use a fresh installation to adopt this layout.

## Back up settings

Stop the bridge, back up `/opt/unifi-camera-bridge` including its `data` folder,
then start it again. Protect the backup: it contains camera logins. When restoring,
keep `data` owned by `65532:65532` with permissions `0700`, and `config.json` with
permissions `0600`. Restore files before starting the container.

## Update

The default `stable` image tag follows published releases. In a host terminal:

```sh
cd /opt/unifi-camera-bridge
sudo docker compose -p unifi-camera-bridge pull manager
sudo docker compose -p unifi-camera-bridge up -d --no-build manager
```

The pull happens while the current bridge runs. Recreating the container briefly
interrupts video and clears browser sessions. Keep the `data` folder when replacing or removing containers.
Plain Compose updates do not automatically roll back or back up settings. Keep a
secure backup before upgrades, especially when release notes mention migrations.
The camera application has no access to the Docker socket.

## Pin a release or roll back

Change the image tag in `/opt/unifi-camera-bridge/compose.yaml` from `stable` to a
numbered release (for example `1.6.7`), then run the same pull/up commands. Older
versions may require a matching settings backup if a release changes its format.
Keep the same project name and data-folder mount.

Rerunning an installer resets the image reference to the one in that installer.
If an installer-driven deployment fails its health check, the installer attempts
to restart the exact previous image ID. Settings are retained; schema migrations
are not automatically reversed. Registry/pull failures leave the running bridge
untouched. Previous images should be kept until an update has been checked.

## Network and data

Admin defaults to port 18080; ONVIF uses 8100–8115 and RTSP uses 8600–8615. Debian
binds these to the selected private LAN IPv4. Keep the address stable. Complete
first-admin setup promptly on a trusted network. The installer does not alter
router or firewall policy. Credentials stay in `/opt/unifi-camera-bridge/data/config.json`; release images
contain no camera settings.

Use the application's Logs screen for camera issues, or `sudo docker logs --tail
100 unifi-camera-bridge` for startup troubleshooting. The installer is for
Debian, not native QNAP QTS. Container Station can use the same GHCR image with
the documented ports and persistent volume.
