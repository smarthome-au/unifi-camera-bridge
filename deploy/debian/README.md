# Unifi Camera Bridge — Debian installer

For Debian 12 or 13 on an Intel/AMD x64 or ARM64 machine/VM with systemd.
Both bridge images are included. The installer selects the right one, installs
Docker Engine and Compose if needed, imports the image and starts the bridge.
No registry account, manual image import or configuration-file editing is needed.
This installs bridge version 1.6.8. It does not install Debian itself.

## Install

Copy the installer archive to the Debian machine. In that directory:

```sh
tar -xzf unifi-camera-bridge-1.6.8-debian-installer.tar.gz
cd unifi-camera-bridge-1.6.8-debian
sudo ./install.sh
```

If sudo is unavailable, sign in as root and run `./install.sh`.
Internet access is required to install Debian prerequisites and Docker packages;
the bridge images are already included. A working Docker/Compose installation
is reused. The installer does not upgrade or replace a working engine.

At the end, the installer prints an address such as `http://192.168.50.4:18080`.
Open the printed address from your browser, choose the admin password and add
cameras. The setup page fills the bridge address from the LAN IP in that URL.
No one-time token is required. Use a stable/DHCP-reserved host address.

## What it sets up

- Docker Engine and the Compose plugin, using Docker's signed Debian repository
  when installation is necessary. Debian prerequisites are Python 3, iproute2 and
  CA certificates. No unrelated container packages are automatically removed.
- An installation directory at `/opt/unifi-camera-bridge` containing generated
  Compose configuration and installation metadata. These files have no camera
  credentials.
- A container named `unifi-camera-bridge`, with a read-only filesystem and a
  non-root application user. It starts automatically when Docker starts.
- Persistent settings in `/opt/unifi-camera-bridge/data/config.json`.
  Back up the `data` folder securely; it contains camera credentials.
- TCP port 18080 for administration, 8100–8115 for ONVIF/events/snapshots, and
  8600–8615 for RTSP, bound to the detected private LAN IPv4 address. Port numbers
  for camera views remain identical inside and outside Docker.

The first visitor who can reach a fresh setup page can create the admin account.
Complete setup after installation. The installer does not change your router or
custom firewall rules. Docker-published ports need consideration when using UFW
or firewalld; keep this service on trusted networks rather than forwarding it
from the internet. See Docker's [Debian installation guidance](https://docs.docker.com/engine/install/debian/).

The installer never accesses the Docker socket from inside the bridge container
and never places camera passwords in command arguments.

## Updates and reruns

Extract a newer installer bundle and run its `install.sh`. The installer keeps
its existing LAN binding, admin port and settings, then replaces the
application image. Upgrades briefly interrupt video and clear browser sessions.
If startup fails, it attempts to restore the previous deployment configuration.
A failed first installation retains its data folder so settings are not discarded.

This installer does not migrate older installations or Docker volumes. Start
with a fresh installation to use folder storage. Existing settings are not deleted.
An unrelated container with the same name is not taken over automatically.
Existing conflicting Docker packages are reported without removing them.

This installer sets up a separate host. It does not migrate or stop your current
Mac/QNAP bridge or change existing Protect adoptions. You can add cameras afresh,
or separately transfer your settings when ready for a planned cutover.

## Optional controls

Normally the host's private LAN address is chosen automatically, preferring its
default-route interface and excluding common Docker/VPN interfaces. If multiple
LAN interfaces are ambiguous, specify the one Protect can reach:

```sh
sudo ./install.sh --bind-address 192.168.50.4
```

This must be an address actually assigned to the Debian host. It is not a camera
address. An update keeps the previous binding; if the host IP has changed, use
this option and then update **Bridge address** in the web interface too.

For an occupied admin port:

```sh
sudo ./install.sh --admin-port 18081
```

A conflict on a camera port stops installation instead of silently changing
adoption addresses. It does not stop unrelated services to free ports.

For a read-only preflight (requires Python 3, iproute2 and CA certificates already
installed):

```sh
sudo ./install.sh --check
```

## Manage the service

View status:

```sh
sudo docker compose -p unifi-camera-bridge -f /opt/unifi-camera-bridge/compose.yaml ps
```

Use **Logs** in the bridge web interface for camera issues. For startup errors:

```sh
sudo docker logs --tail 100 unifi-camera-bridge
```

Stop/start without removing settings:

```sh
sudo docker compose -p unifi-camera-bridge -f /opt/unifi-camera-bridge/compose.yaml stop
sudo docker compose -p unifi-camera-bridge -f /opt/unifi-camera-bridge/compose.yaml up -d --no-build --pull never
```

Back up `/opt/unifi-camera-bridge/data` while the bridge is stopped for a consistent
copy. Keep the folder owned by `65532:65532` with permissions `0700`; `config.json`
uses `0600`. Restore it with the same ownership and permissions before starting
the bridge. Removing a container does not remove this folder.

## Scope and validation

This installer targets standard Debian hosts/VMs, not QNAP's native QTS operating
system, Debian derivatives, WSL or a nested Docker container. Use the QNAP Docker
kit for Container Station.

Validation includes installer workflow/failure-path tests, real Docker package
installation in disposable Debian environments, and bridge container deployment
checks. A full Debian VM boot/reboot installation has not been exercised here.
