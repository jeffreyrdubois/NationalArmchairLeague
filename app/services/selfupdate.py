"""Update the app by replacing its own container, from inside the app.

Updating used to mean a VPN into the house and a trip through the Unraid Docker
tab. This does the same three things from the admin page: pull the image, swap
the container, and check the new one actually came up.

A container cannot replace itself — the moment it stops, whatever is doing the
work dies with it. So the swap is handed to a short-lived **helper container**
started from the image that is *currently* running (never the new one, so a bad
image can never be the thing responsible for undoing itself). The helper stops
the app, renames it out of the way, starts the replacement, and waits for it to
report healthy. If it doesn't, the helper puts the old container back. That
last part is the point of the whole design: testing an unmerged pull request on
the live league is only reasonable if a bad build cannot strand you.

Progress is written to a file on the data volume rather than kept in memory,
because by the time the update finishes the process that started it no longer
exists — the page that polls for the result is being served by the new
container.

Run as ``python -m app.services.selfupdate`` this module *is* the helper.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

from app.services.docker_api import (
    DockerClient, DockerError, default_socket, registry_auth_header,
)

logger = logging.getLogger(__name__)


def data_dir_default() -> str:
    """The data volume, read when needed rather than frozen at import."""
    return os.getenv("DATA_DIR", "/app/data")


STATUS_FILENAME = "update-status.json"
PLAN_FILENAME = "update-plan.json"

# The image is fixed in code. The admin picks a tag, never a repository — an
# arbitrary image reference from a form would turn "update the app" into "run
# anything you like on the host as root", which is exactly what the mounted
# Docker socket makes possible.
IMAGE_REPOSITORY = os.getenv(
    "UPDATE_IMAGE_REPO", "ghcr.io/jeffreyrdubois/nationalarmchairleague"
)

# Docker's own tag grammar, and nothing looser.
TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")

# How long the replacement gets to report healthy before it is rolled back.
# The image's healthcheck has a 20s start period and a 60s interval, so the
# first real verdict lands around 25s in; three minutes leaves room for a slow
# array spin-up without leaving a broken container running all afternoon.
HEALTH_TIMEOUT = int(os.getenv("UPDATE_HEALTH_TIMEOUT", "180"))

TERMINAL_STATES = ("done", "failed", "rolled_back", "idle")

# An update that has not said anything for this long is not running any more —
# the helper container died, or the host rebooted mid-swap. Without this the
# status file would read "swapping" forever and refuse to let another update
# start, which is precisely the "now I have to VPN in" outcome the whole
# feature exists to avoid. Generous, because pulling a few hundred megabytes
# over a slow line is legitimately quiet for a while.
STALE_AFTER_SECONDS = 900


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status_path(data_dir: str | None = None) -> str:
    return os.path.join(data_dir or data_dir_default(), STATUS_FILENAME)


def plan_path(data_dir: str | None = None) -> str:
    return os.path.join(data_dir or data_dir_default(), PLAN_FILENAME)


def read_status(data_dir: str | None = None) -> dict:
    try:
        with open(status_path(data_dir), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"state": "idle", "message": "No update has been run yet."}


def write_status(data_dir: str | None = None, **fields) -> dict:
    """Merge fields into the status file. Never raises — status is not the job.

    Written to a temporary file and moved into place so a page polling for
    progress can never read a half-written line of JSON.
    """
    status = read_status(data_dir)
    if status.get("state") == "idle" and "state" not in fields:
        status = {}
    status.update(fields)
    status["updated_at"] = _now()
    path = status_path(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(status, handle, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not write update status: %s", exc)
    return status


def seconds_since_update(status: dict) -> float | None:
    """How long the status file has been silent, or None if it never spoke."""
    stamp = status.get("updated_at")
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def is_stalled(status: dict) -> bool:
    """An update that claims to be running but has stopped saying anything."""
    if status.get("state") in TERMINAL_STATES:
        return False
    silent_for = seconds_since_update(status)
    return silent_for is not None and silent_for > STALE_AFTER_SECONDS


def is_running(data_dir: str | None = None) -> bool:
    """Whether an update is genuinely in flight.

    A stalled one is not: it must not go on blocking the next attempt, since
    trying again from this page is the only recovery that does not involve the
    trip to the server.
    """
    status = read_status(data_dir)
    if status.get("state") in TERMINAL_STATES:
        return False
    return not is_stalled(status)


def validate_tag(tag: str) -> str:
    tag = (tag or "").strip()
    if not TAG_PATTERN.match(tag):
        raise ValueError(
            "That is not a valid image tag — tags are letters, digits, dots, "
            "dashes and underscores."
        )
    return tag


def image_ref(tag: str) -> str:
    return f"{IMAGE_REPOSITORY}:{validate_tag(tag)}"


# ---------------------------------------------------------------------------
# Rebuilding the container
# ---------------------------------------------------------------------------

# Config keys worth carrying across, in the shape /containers/create wants.
_CONFIG_KEYS = (
    "Hostname", "Domainname", "User", "ExposedPorts", "Tty", "OpenStdin",
    "StdinOnce", "Env", "Cmd", "Healthcheck", "Volumes", "WorkingDir",
    "Entrypoint", "Labels", "StopSignal", "StopTimeout", "Shell",
)

# Endpoint settings a human chose, as opposed to the runtime state Docker fills
# in (addresses, gateways, endpoint ids) which must not be replayed.
_ENDPOINT_KEYS = ("Aliases", "Links", "IPAMConfig", "DriverOpts")


def _strip_image_defaults(container_config: dict, image_config: dict) -> dict:
    """Keep what the operator set; drop what the old image contributed.

    Everything `docker inspect` reports on a container is a merge of the image's
    own defaults and whatever was passed at `docker run`, with no marker saying
    which is which. Replaying that merge onto a new image pins the old image's
    defaults over the new one's — and the value that matters most is APP_VERSION,
    baked into the image at build time. Carry it forward and the updated app
    reports the version it replaced, on the very screen that exists to tell you
    whether the update worked.

    So each value is compared against the old image's, and anything that simply
    came from there is left out for the new image to supply.
    """
    spec: dict = {}
    for key in _CONFIG_KEYS:
        if key not in container_config:
            continue
        value = container_config[key]
        if value in (None, "", [], {}):
            continue
        if key == "Env":
            image_env = set(image_config.get("Env") or [])
            operator_env = [entry for entry in value if entry not in image_env]
            if operator_env:
                spec["Env"] = operator_env
            continue
        if key == "Labels":
            image_labels = image_config.get("Labels") or {}
            # Compose and Unraid both label their containers; those are not the
            # image's and have to survive, or the tooling loses track of it.
            operator_labels = {
                k: v for k, v in value.items() if image_labels.get(k) != v
            }
            if operator_labels:
                spec["Labels"] = operator_labels
            continue
        if value == image_config.get(key):
            continue
        spec[key] = value
    return spec


def _networking_config(container: dict) -> tuple[dict | None, dict]:
    """(config for the first network, the rest to connect afterwards).

    ``/containers/create`` accepts only one network, so a container attached to
    several — an app behind SWAG, typically — is created on the first and
    connected to the others once it exists.
    """
    mode = (container.get("HostConfig") or {}).get("NetworkMode") or ""
    if mode in ("host", "none") or mode.startswith("container:"):
        return None, {}

    networks = ((container.get("NetworkSettings") or {}).get("Networks") or {})
    short_id = (container.get("Id") or "")[:12]
    cleaned: dict[str, dict] = {}
    for name, endpoint in networks.items():
        settings = {}
        for key in _ENDPOINT_KEYS:
            value = (endpoint or {}).get(key)
            if not value:
                continue
            if key == "Aliases":
                # Docker adds the container's own short id as an alias; feeding
                # it back would pin the new container to the old one's id.
                value = [alias for alias in value if alias != short_id]
                if not value:
                    continue
            settings[key] = value
        cleaned[name] = settings

    if not cleaned:
        return None, {}
    first = next(iter(cleaned))
    return {"EndpointsConfig": {first: cleaned[first]}}, {
        name: settings for name, settings in cleaned.items() if name != first
    }


def build_container_spec(container: dict, image: dict, new_image_ref: str) -> dict:
    """The body for ``/containers/create`` that reproduces this container.

    Used for both directions: the replacement is this container with a new
    image, and a rollback is this container with the image it had. Sharing one
    function means the rollback path is not a separate, less-travelled one.
    """
    image_config = (image or {}).get("Config") or {}
    spec = _strip_image_defaults(container.get("Config") or {}, image_config)

    # Docker defaults an unset hostname to the container's short id. Replaying
    # that would give the new container the old one's identity on the network.
    if spec.get("Hostname") and spec["Hostname"] == (container.get("Id") or "")[:12]:
        spec.pop("Hostname")

    spec["Image"] = new_image_ref
    spec["HostConfig"] = dict(container.get("HostConfig") or {})
    networking, _extra = _networking_config(container)
    if networking:
        spec["NetworkingConfig"] = networking
    return spec


def extra_networks(container: dict) -> dict:
    return _networking_config(container)[1]


def container_name(container: dict) -> str:
    return (container.get("Name") or "").lstrip("/")


def _data_mount(container: dict, data_dir: str) -> dict | None:
    """The helper needs the same /app/data the app has, to report progress."""
    for mount in container.get("Mounts") or []:
        if mount.get("Destination") != data_dir:
            continue
        if mount.get("Type") == "volume":
            return {
                "Type": "volume", "Source": mount.get("Name"),
                "Target": data_dir, "ReadOnly": False,
            }
        return {
            "Type": "bind", "Source": mount.get("Source"),
            "Target": data_dir, "ReadOnly": False,
        }
    return None


# ---------------------------------------------------------------------------
# Phase one: pull, then hand the swap to a helper container
# ---------------------------------------------------------------------------

def _progress_reporter(data_dir: str):
    """Turn Docker's per-layer chatter into one line and a percentage."""
    layers: dict[str, tuple[int, int]] = {}
    last_written = [0.0]

    def report(event: dict) -> None:
        layer = event.get("id")
        detail = event.get("progressDetail") or {}
        if layer and detail.get("total"):
            layers[layer] = (detail.get("current") or 0, detail["total"])
        current = sum(c for c, _ in layers.values())
        total = sum(t for _, t in layers.values())
        percent = int(current / total * 100) if total else 0
        # Docker reports progress many times a second; the status file does not
        # need to hear about all of it.
        now = time.monotonic()
        if now - last_written[0] < 1.0:
            return
        last_written[0] = now
        status = event.get("status") or "Pulling"
        write_status(
            data_dir,
            state="pulling",
            percent=percent,
            message=f"{status}… {percent}%" if total else str(status),
        )

    return report


def start_update(
    tag: str,
    *,
    started_by: str = "",
    data_dir: str | None = None,
    socket_path: str | None = None,
    registry_user: str = "",
    registry_token: str = "",
    self_container: str | None = None,
) -> None:
    """Pull the requested tag and hand the swap to a helper container.

    Runs in the background: pulling a multi-hundred-megabyte image takes far
    longer than a request should, and the caller only needs to know it started.
    Everything after the helper launches happens outside this process.
    """
    data_dir = data_dir or data_dir_default()
    socket_path = socket_path or default_socket()
    client = DockerClient(socket_path)
    target = image_ref(tag)

    try:
        name = self_container or os.getenv("UPDATE_CONTAINER_NAME") or _own_container_id()
        if not name:
            raise DockerError(
                "Could not work out which container the app is running in. "
                "Set UPDATE_CONTAINER_NAME to the container's name."
            )
        try:
            container = client.inspect_container(name)
        except DockerError as exc:
            if exc.status != 404:
                raise
            # Worked out from the cgroup, or fallen back to the hostname — and
            # a compose file or an Unraid template can set that to anything.
            # Docker's own "no such container" says nothing about how to fix it.
            raise DockerError(
                f"Docker has no container called '{name}', which is what this "
                "app worked out it is running in. Set the container name in "
                "Updater Settings (or UPDATE_CONTAINER_NAME) to its real name."
            ) from exc
        current_image_id = container.get("Image") or ""
        old_image_ref = (container.get("Config") or {}).get("Image") or current_image_id

        write_status(
            data_dir,
            state="pulling",
            percent=0,
            tag=tag,
            target_image=target,
            from_image=old_image_ref,
            from_version=os.getenv("APP_VERSION", ""),
            container=container_name(container),
            started_by=started_by,
            started_at=_now(),
            message=f"Pulling {target}…",
            error="",
        )

        auth = (
            registry_auth_header(registry_user, registry_token)
            if registry_token else None
        )
        client.pull_image(
            IMAGE_REPOSITORY, validate_tag(tag),
            auth_header=auth, on_progress=_progress_reporter(data_dir),
        )

        new_image = client.inspect_image(target)
        if new_image.get("Id") == current_image_id:
            write_status(
                data_dir, state="done", percent=100,
                message=f"Already running {target} — nothing to do.",
            )
            return

        old_image = client.inspect_image(current_image_id)
        plan = {
            "socket_path":    socket_path,
            "data_dir":       data_dir,
            "container_id":   container.get("Id"),
            "container_name": container_name(container),
            "new_spec":       build_container_spec(container, old_image, target),
            "old_spec":       build_container_spec(container, old_image, old_image_ref),
            "extra_networks": extra_networks(container),
            "target_image":   target,
            "old_image":      old_image_ref,
            "auto_remove":    bool((container.get("HostConfig") or {}).get("AutoRemove")),
            "health_timeout": HEALTH_TIMEOUT,
        }
        _write_plan(plan, data_dir)

        write_status(
            data_dir, state="swapping", percent=100,
            message="Image pulled. Handing over to the updater…",
        )
        _launch_helper(client, container, plan, data_dir)

    except Exception as exc:  # noqa: BLE001 - the admin has to see every failure
        logger.exception("Update to %s failed before the swap", tag)
        write_status(
            data_dir, state="failed",
            message="The update could not be started.",
            error=str(exc)[:500],
        )


def _write_plan(plan: dict, data_dir: str) -> None:
    """Hand the plan over on disk rather than in an environment variable.

    It carries the container's environment, which includes the session signing
    key and any API tokens, so it is written owner-only and the helper deletes
    it the moment it has been read.
    """
    path = plan_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(plan, handle)


def _launch_helper(client: DockerClient, container: dict, plan: dict, data_dir: str) -> None:
    """Start the throwaway container that outlives us and does the swap.

    Built from the image that is running *now*, not the one being installed: the
    code that has to survive a bad image and roll it back cannot be code that
    came out of that image.
    """
    socket_path = plan["socket_path"]
    mount = _data_mount(container, data_dir)
    if not mount:
        raise DockerError(
            f"The app's {data_dir} volume could not be identified, so the "
            "updater would have nowhere to report back to."
        )

    spec = {
        # The app's own entrypoint drops privileges and fixes ownership; the
        # helper wants neither, and does need to open the Docker socket
        # whatever group it belongs to.
        "Image": container.get("Image"),
        "Entrypoint": ["python", "-m", "app.services.selfupdate"],
        "Cmd": [],
        "User": "0:0",
        "WorkingDir": "/app",
        "Env": [f"NAL_UPDATE_PLAN={plan_path(data_dir)}", "PYTHONUNBUFFERED=1"],
        "Labels": {"nal.role": "updater"},
        "HostConfig": {
            # Nothing to reach but the socket, and no ports to collide with the
            # container it is about to replace.
            "NetworkMode": "none",
            "AutoRemove": True,
            "Binds": [f"{socket_path}:{socket_path}"],
            "Mounts": [mount],
            "RestartPolicy": {"Name": "no"},
        },
    }
    name = f"nal-updater-{int(time.time())}"
    helper_id = client.create_container(name, spec)
    client.start_container(helper_id)
    logger.info("Updater container %s started", name)


def _own_container_id() -> str:
    """This container's id, read out of its own cgroup or mountinfo.

    Docker sets the hostname to the short id by default, but a compose file or
    an Unraid template can override it, so the hostname alone is not reliable.
    """
    for path, pattern in (
        ("/proc/self/mountinfo", re.compile(r"/docker/containers/([0-9a-f]{64})")),
        ("/proc/self/cgroup", re.compile(r"[0-9a-f]{64}")),
    ):
        try:
            with open(path, encoding="utf-8") as handle:
                match = pattern.search(handle.read())
            if match:
                return match.group(1) if match.groups() else match.group(0)
        except OSError:
            continue
    return os.getenv("HOSTNAME", "")


# ---------------------------------------------------------------------------
# Phase two: the helper container
# ---------------------------------------------------------------------------

def _wait_until_healthy(client: DockerClient, name: str, timeout: int) -> tuple[bool, str]:
    """Watch the replacement until it is demonstrably up, or demonstrably not.

    A container that has a healthcheck has to actually pass it. One that does
    not only has to still be running after a grace period — long enough for an
    import error or a failed migration to have taken it down.
    """
    deadline = time.monotonic() + timeout
    grace_until = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            state = (client.inspect_container(name) or {}).get("State") or {}
        except DockerError as exc:
            return False, f"The new container disappeared: {exc}"

        if state.get("Status") in ("exited", "dead"):
            return False, (
                f"The new container exited with code {state.get('ExitCode')}."
            )
        health = (state.get("Health") or {}).get("Status")
        if health == "healthy":
            return True, "The new container is healthy."
        if health == "unhealthy":
            return False, "The new container started but never became healthy."
        if not health and state.get("Running") and time.monotonic() > grace_until:
            return True, "The new container is running."
        time.sleep(2)
    return False, f"The new container did not come up within {timeout}s."


def _run_helper(plan: dict) -> int:
    data_dir = plan["data_dir"]
    client = DockerClient(plan["socket_path"])
    name = plan["container_name"]
    parked = f"{name}-nal-previous"

    def note(state: str, message: str, **extra):
        write_status(data_dir, state=state, message=message, **extra)
        logger.info("[updater] %s", message)

    # The old container is still serving the page that asked for this. Give the
    # response a moment to land before pulling the floor out.
    time.sleep(3)

    note("swapping", "Stopping the current container…")
    client.remove_container(parked, force=True)
    try:
        client.stop_container(plan["container_id"])
        if not plan.get("auto_remove"):
            client.rename_container(plan["container_id"], parked)
        else:
            # --rm containers are already gone; the saved spec is all we have.
            parked = ""
    except DockerError as exc:
        note("failed", "Could not stop the current container.", error=str(exc)[:500])
        return 1

    def restore(reason: str) -> int:
        note("rolled_back", f"{reason} Putting the previous version back…")
        try:
            client.remove_container(name, force=True)
            if parked:
                client.rename_container(parked, name)
            else:
                client.create_container(name, plan["old_spec"])
            client.start_container(name)
            note(
                "rolled_back",
                f"{reason} The previous version has been restored.",
                error=reason,
            )
            return 1
        except DockerError as exc:
            note(
                "failed",
                "The update failed and the previous version could not be "
                "restored — start the container from the Unraid Docker tab.",
                error=f"{reason} Rollback also failed: {exc}"[:500],
            )
            return 2

    note("starting", f"Starting {plan['target_image']}…")
    try:
        client.create_container(name, plan["new_spec"])
        for network, settings in (plan.get("extra_networks") or {}).items():
            client.connect_network(network, name, settings)
        client.start_container(name)
    except DockerError as exc:
        return restore(f"The new container could not be started: {exc}")

    healthy, detail = _wait_until_healthy(
        client, name, plan.get("health_timeout", HEALTH_TIMEOUT)
    )
    if not healthy:
        return restore(detail)

    if parked:
        client.remove_container(parked, force=True)
    note(
        "done",
        f"Updated to {plan['target_image']}.",
        percent=100,
        finished_at=_now(),
        error="",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[updater] %(message)s")
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else os.getenv("NAL_UPDATE_PLAN", plan_path())
    try:
        with open(path, encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.error("No usable update plan at %s: %s", path, exc)
        return 2
    # It holds the app's environment, secrets included. It has been read; it
    # has no business outliving that.
    try:
        os.unlink(path)
    except OSError:
        pass
    return _run_helper(plan)


if __name__ == "__main__":
    raise SystemExit(main())
