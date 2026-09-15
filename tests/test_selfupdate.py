"""Tests for updating the app from inside the app.

This is the one feature that, done wrong, takes the league offline and leaves
no way back in except the VPN trip it exists to avoid. So it is tested against
a stand-in Docker daemon — a real HTTP server on a real unix socket, speaking
the Engine API — rather than with mocks, and the assertions are about what
actually gets sent to Docker.

Three things carry the most risk:

1. **Rebuilding the container.** `docker inspect` reports the image's own
   defaults and the operator's settings merged together with no marker saying
   which is which. Replay the merge onto a new image and the old image's
   defaults win — including APP_VERSION, so the updated app would report the
   version it just replaced on the very screen that exists to say whether the
   update worked.
2. **Rollback.** Installing an unmerged pull request on the live league is only
   reasonable if a build that does not come up puts the old one back.
3. **Not taking instructions.** The Docker socket is root on the host. The tag
   is the only thing an admin supplies, and it never becomes an image name.

Run with: python tests/test_selfupdate.py
"""
import datetime
import json
import os
import socketserver
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_SANDBOX = tempfile.mkdtemp(prefix="nal-update-")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_SANDBOX, 'test.db')}"

# No test may reach a real Docker daemon. A developer machine usually has none,
# but a CI runner does — and a test that quietly starts talking to it is both
# flaky and genuinely dangerous, since these are the code paths that stop and
# replace containers. Every test either points the app at its own stand-in
# daemon (`wired_up`) or gets this socket, which cannot exist.
os.environ["DOCKER_SOCKET"] = os.path.join(_SANDBOX, "no-such-docker.sock")
os.environ["DATA_DIR"] = os.path.join(_SANDBOX, "data")
os.makedirs(os.environ["DATA_DIR"], exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import AppSetting, Role, User
from app.routers import admin
from app.services import docker_api, registry, selfupdate

app = FastAPI()
app.include_router(admin.router)


# ---------------------------------------------------------------------------
# A stand-in Docker daemon
# ---------------------------------------------------------------------------

class FakeDocker:
    """Enough of the Engine API to swap a container, and a log of every call.

    Containers are dicts keyed by name; `behaviour` lets a test make the next
    create fail, or make the replacement come up unhealthy, which is how the
    rollback paths get exercised.
    """

    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.images: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.pulled: list[str] = []
        self.pull_error: str | None = None
        self.create_error: str | None = None
        self.health_for_new = "healthy"

    # -- fixtures ---------------------------------------------------------

    def add_image(self, ref: str, image_id: str, config: dict):
        self.images[ref] = {"Id": image_id, "Config": config}
        self.images[image_id] = {"Id": image_id, "Config": config}

    def add_container(self, name: str, payload: dict):
        self.containers[name] = payload

    # -- request handling -------------------------------------------------

    def handle(self, method: str, path: str, query: dict, body: dict | None):
        self.calls.append((method, path))

        if path == "/version":
            return 200, {"Version": "99.9", "ApiVersion": "1.41"}

        if path.startswith("/containers/") and path.endswith("/json"):
            name = path[len("/containers/"):-len("/json")]
            found = self._lookup(name)
            if not found:
                return 404, {"message": f"No such container: {name}"}
            return 200, found

        if path.startswith("/images/") and path.endswith("/json"):
            ref = path[len("/images/"):-len("/json")]
            from urllib.parse import unquote
            ref = unquote(ref)
            if ref not in self.images:
                return 404, {"message": f"No such image: {ref}"}
            return 200, self.images[ref]

        if path == "/images/create":
            ref = f"{query.get('fromImage')}:{query.get('tag')}"
            self.pulled.append(ref)
            if self.pull_error:
                return "stream", [
                    {"status": "Pulling from library"},
                    {"error": self.pull_error},
                ]
            return "stream", [
                {"status": "Pulling fs layer", "id": "abc",
                 "progressDetail": {"current": 0, "total": 100}},
                {"status": "Downloading", "id": "abc",
                 "progressDetail": {"current": 100, "total": 100}},
                {"status": f"Status: Downloaded newer image for {ref}"},
            ]

        if path == "/containers/create":
            name = query.get("name", "")
            if self.create_error:
                return 400, {"message": self.create_error}
            if name in self.containers:
                return 409, {"message": f"name {name} already in use"}
            image_ref = body.get("Image", "")
            health = (
                {"Status": self.health_for_new}
                if name != "" and image_ref in self.images else None
            )
            self.containers[name] = {
                "Id": f"id-{name}-{len(self.containers)}",
                "Name": f"/{name}",
                "Image": self.images.get(image_ref, {}).get("Id", image_ref),
                "Config": dict(body),
                "HostConfig": body.get("HostConfig", {}),
                "State": {"Running": False, "Status": "created", "Health": health},
                "Mounts": [],
                "NetworkSettings": {"Networks": {}},
                "_created_from": body,
                "_networks_connected": [],
            }
            return 201, {"Id": self.containers[name]["Id"]}

        if path.startswith("/containers/") and path.endswith("/start"):
            name = path[len("/containers/"):-len("/start")]
            found_name = self._name_of(name)
            if not found_name:
                return 404, {"message": "no such container"}
            self.containers[found_name]["State"] = {
                "Running": True, "Status": "running",
                "Health": self.containers[found_name]["State"].get("Health"),
            }
            return 204, None

        if path.startswith("/containers/") and path.endswith("/stop"):
            name = path[len("/containers/"):-len("/stop")]
            found_name = self._name_of(name)
            if not found_name:
                return 404, {"message": "no such container"}
            self.containers[found_name]["State"] = {
                "Running": False, "Status": "exited", "ExitCode": 0, "Health": None,
            }
            return 204, None

        if path.startswith("/containers/") and path.endswith("/rename"):
            name = path[len("/containers/"):-len("/rename")]
            found_name = self._name_of(name)
            if not found_name:
                return 404, {"message": "no such container"}
            new_name = query.get("name")
            payload = self.containers.pop(found_name)
            payload["Name"] = f"/{new_name}"
            self.containers[new_name] = payload
            return 204, None

        if method == "DELETE" and path.startswith("/containers/"):
            name = path[len("/containers/"):]
            found_name = self._name_of(name)
            if not found_name:
                return 404, {"message": "no such container"}
            self.containers.pop(found_name)
            return 204, None

        if path.startswith("/networks/") and path.endswith("/connect"):
            network = path[len("/networks/"):-len("/connect")]
            target = self._name_of(body.get("Container", ""))
            if target:
                self.containers[target]["_networks_connected"].append(network)
            return 200, None

        return 404, {"message": f"unhandled: {method} {path}"}

    def _name_of(self, name_or_id: str) -> str | None:
        if name_or_id in self.containers:
            return name_or_id
        for name, payload in self.containers.items():
            if payload.get("Id") == name_or_id:
                return name
        return None

    def _lookup(self, name_or_id: str):
        found = self._name_of(name_or_id)
        return self.containers[found] if found else None


class _Handler(BaseHTTPRequestHandler):
    fake: FakeDocker = None  # set per-server

    def log_message(self, *args):
        pass

    def _run(self, method):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        # The client prefixes every path with the pinned API version.
        route = parsed.path
        if route.startswith("/v1."):
            route = route[route.index("/", 1):]
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None

        status, payload = self.fake.handle(method, route, query, body)

        if status == "stream":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            for event in payload:
                self.wfile.write(json.dumps(event).encode() + b"\n")
            return

        raw = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")

    def do_DELETE(self):
        self._run("DELETE")


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        # BaseHTTPRequestHandler expects a (host, port) client address.
        return request, ("local", 0)


def serve(fake: FakeDocker):
    """Run `fake` on a throwaway unix socket. Returns (socket_path, stop)."""
    path = os.path.join(tempfile.mkdtemp(prefix="nal-sock-"), "docker.sock")
    handler = type("Bound", (_Handler,), {"fake": fake})
    server = _UnixHTTPServer(path, handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        server.server_close()

    return path, stop


# ---------------------------------------------------------------------------
# Fixtures shaped like the real thing
# ---------------------------------------------------------------------------

OLD_IMAGE = "ghcr.io/jeffreyrdubois/nationalarmchairleague:latest"
NEW_TAG = "pr-54"
NEW_IMAGE = f"ghcr.io/jeffreyrdubois/nationalarmchairleague:{NEW_TAG}"

# What the image itself contributes — the entrypoint, the port, and the version
# stamped in at build time.
OLD_IMAGE_CONFIG = {
    "Env": [
        "PATH=/opt/venv/bin:/usr/local/bin:/usr/bin",
        "APP_VERSION=1.0.0+aaaaaaa",
        "BUILD_DATE=2026-08-29T14:02:11Z",
        "DATA_DIR=/app/data",
        "PUID=99",
        "PGID=100",
    ],
    "Cmd": ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"],
    "Entrypoint": ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"],
    "WorkingDir": "/app",
    "Labels": {"org.opencontainers.image.title": "National Armchair League"},
    "ExposedPorts": {"8000/tcp": {}},
}

NEW_IMAGE_CONFIG = dict(OLD_IMAGE_CONFIG, Env=[
    "PATH=/opt/venv/bin:/usr/local/bin:/usr/bin",
    "APP_VERSION=1.0.0-pr54+bbbbbbb",
    "BUILD_DATE=2026-09-15T10:00:00Z",
    "DATA_DIR=/app/data",
    "PUID=99",
    "PGID=100",
])


def a_container(data_dir="/app/data", **overrides) -> dict:
    """An inspect payload shaped like a real Unraid/compose nal container."""
    payload = {
        "Id": "c" * 64,
        "Name": "/nal",
        "Image": "sha256:oldimageid",
        "Config": {
            # Docker defaults the hostname to the short container id.
            "Hostname": "c" * 12,
            "Image": OLD_IMAGE,
            # The operator's settings and the image's, merged — as Docker
            # reports them, with nothing saying which is which.
            "Env": OLD_IMAGE_CONFIG["Env"] + [
                "ODDS_API_KEY=secret-odds",
                "TZ=America/New_York",
                "HOST_PORT=5950",
            ],
            "Cmd": OLD_IMAGE_CONFIG["Cmd"],
            "Entrypoint": OLD_IMAGE_CONFIG["Entrypoint"],
            "WorkingDir": "/app",
            "ExposedPorts": {"8000/tcp": {}},
            "Labels": {
                "org.opencontainers.image.title": "National Armchair League",
                "com.docker.compose.project": "nal",
                "net.unraid.docker.webui": "http://[IP]:[PORT:8000]/",
            },
        },
        "HostConfig": {
            "Binds": ["/mnt/user/appdata/nal:/app/data:rw"],
            "PortBindings": {"8000/tcp": [{"HostIp": "", "HostPort": "5950"}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NetworkMode": "bridge",
            "AutoRemove": False,
        },
        "Mounts": [{
            "Type": "bind", "Source": "/mnt/user/appdata/nal",
            "Destination": data_dir, "RW": True,
        }],
        "NetworkSettings": {"Networks": {"bridge": {
            "Aliases": ["c" * 12, "nal"],
            "IPAddress": "172.17.0.4",
            "Gateway": "172.17.0.1",
            "EndpointID": "e" * 64,
            "NetworkID": "n" * 64,
        }}},
        "State": {"Running": True, "Status": "running", "Health": {"Status": "healthy"}},
    }
    payload.update(overrides)
    return payload


def a_fake(data_dir="/app/data", **container_overrides) -> FakeDocker:
    fake = FakeDocker()
    fake.add_image(OLD_IMAGE, "sha256:oldimageid", OLD_IMAGE_CONFIG)
    fake.add_image("sha256:oldimageid", "sha256:oldimageid", OLD_IMAGE_CONFIG)
    fake.add_image(NEW_IMAGE, "sha256:newimageid", NEW_IMAGE_CONFIG)
    fake.add_container("nal", a_container(data_dir=data_dir, **container_overrides))
    return fake


def a_data_dir() -> str:
    return tempfile.mkdtemp(prefix="nal-data-")


# ---------------------------------------------------------------------------
# Rebuilding the container
# ---------------------------------------------------------------------------

def test_the_new_container_does_not_inherit_the_old_versions_stamp():
    """The bug that would make the update page lie about whether it worked."""
    spec = selfupdate.build_container_spec(
        a_container(), {"Config": OLD_IMAGE_CONFIG}, NEW_IMAGE
    )
    env = spec.get("Env", [])
    assert not any(e.startswith("APP_VERSION=") for e in env), env
    assert not any(e.startswith("BUILD_DATE=") for e in env), env
    assert not any(e.startswith("PATH=") for e in env), env


def test_the_operators_own_settings_survive():
    spec = selfupdate.build_container_spec(
        a_container(), {"Config": OLD_IMAGE_CONFIG}, NEW_IMAGE
    )
    env = spec["Env"]
    assert "ODDS_API_KEY=secret-odds" in env, env
    assert "TZ=America/New_York" in env, env
    assert "HOST_PORT=5950" in env, env
    assert spec["HostConfig"]["Binds"] == ["/mnt/user/appdata/nal:/app/data:rw"]
    assert spec["HostConfig"]["PortBindings"]["8000/tcp"][0]["HostPort"] == "5950"
    assert spec["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    assert spec["Image"] == NEW_IMAGE, spec["Image"]


def test_an_env_var_the_operator_overrode_is_kept():
    container = a_container()
    container["Config"]["Env"] = [
        e for e in container["Config"]["Env"] if not e.startswith("PUID=")
    ] + ["PUID=1000"]
    spec = selfupdate.build_container_spec(
        container, {"Config": OLD_IMAGE_CONFIG}, NEW_IMAGE
    )
    assert "PUID=1000" in spec["Env"], spec["Env"]


def test_compose_and_unraid_labels_survive_but_the_images_do_not():
    """Lose these and the tooling that manages the container loses track of it."""
    spec = selfupdate.build_container_spec(
        a_container(), {"Config": OLD_IMAGE_CONFIG}, NEW_IMAGE
    )
    labels = spec.get("Labels", {})
    assert labels.get("com.docker.compose.project") == "nal", labels
    assert labels.get("net.unraid.docker.webui"), labels
    assert "org.opencontainers.image.title" not in labels, labels


def test_the_entrypoint_comes_from_the_new_image_not_the_old_container():
    """Pinning the old image's entrypoint is how a changed CMD breaks an update."""
    spec = selfupdate.build_container_spec(
        a_container(), {"Config": OLD_IMAGE_CONFIG}, NEW_IMAGE
    )
    assert "Entrypoint" not in spec, spec.get("Entrypoint")
    assert "Cmd" not in spec, spec.get("Cmd")


def test_the_old_containers_identity_is_not_replayed():
    spec = selfupdate.build_container_spec(
        a_container(), {"Config": OLD_IMAGE_CONFIG}, NEW_IMAGE
    )
    assert "Hostname" not in spec, spec.get("Hostname")
    aliases = spec["NetworkingConfig"]["EndpointsConfig"]["bridge"]["Aliases"]
    assert aliases == ["nal"], aliases
    endpoint = spec["NetworkingConfig"]["EndpointsConfig"]["bridge"]
    for runtime_key in ("IPAddress", "Gateway", "EndpointID", "NetworkID"):
        assert runtime_key not in endpoint, runtime_key


def test_extra_networks_are_connected_separately():
    """Create takes one network; an app behind SWAG is on two."""
    container = a_container()
    container["NetworkSettings"]["Networks"]["swag_default"] = {"Aliases": ["nal"]}
    spec = selfupdate.build_container_spec(
        container, {"Config": OLD_IMAGE_CONFIG}, NEW_IMAGE
    )
    assert len(spec["NetworkingConfig"]["EndpointsConfig"]) == 1, spec["NetworkingConfig"]
    assert list(selfupdate.extra_networks(container)) == ["swag_default"]


def test_host_networking_is_left_alone():
    container = a_container()
    container["HostConfig"]["NetworkMode"] = "host"
    spec = selfupdate.build_container_spec(
        container, {"Config": OLD_IMAGE_CONFIG}, NEW_IMAGE
    )
    assert "NetworkingConfig" not in spec, spec.get("NetworkingConfig")


# ---------------------------------------------------------------------------
# The tag is the only thing an admin supplies
# ---------------------------------------------------------------------------

def test_a_tag_can_never_become_an_image_name():
    for hostile in (
        "latest; rm -rf /", "../../etc/passwd", "evil.io/someone/backdoor:latest",
        "lat est", "", "-starts-with-a-dash", "a" * 200, "latest\nlatest",
        "ghcr.io/evil/x", ":latest", "latest@sha256:deadbeef",
    ):
        try:
            selfupdate.validate_tag(hostile)
        except ValueError:
            continue
        raise AssertionError(f"accepted a bad tag: {hostile!r}")


def test_a_pasted_tag_is_trimmed_rather_than_refused():
    assert selfupdate.validate_tag("  latest  ") == "latest"


def test_a_good_tag_stays_inside_our_own_repository():
    for tag in ("latest", "pr-54", "1.2.3", "sha-a1b2c3d", "v1.0.0"):
        ref = selfupdate.image_ref(tag)
        assert ref == f"{selfupdate.IMAGE_REPOSITORY}:{tag}", ref


# ---------------------------------------------------------------------------
# End to end against the stand-in daemon
# ---------------------------------------------------------------------------

def test_a_successful_update_pulls_then_hands_off_to_a_helper():
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    sock, stop = serve(fake)
    try:
        selfupdate.start_update(
            NEW_TAG, started_by="Ada", data_dir=data,
            socket_path=sock, self_container="nal",
        )
        assert fake.pulled == [NEW_IMAGE], fake.pulled

        status = selfupdate.read_status(data)
        assert status["state"] == "swapping", status
        assert status["target_image"] == NEW_IMAGE, status
        assert status["started_by"] == "Ada", status

        helpers = [n for n in fake.containers if n.startswith("nal-updater-")]
        assert len(helpers) == 1, list(fake.containers)
        helper = fake.containers[helpers[0]]["_created_from"]
        # It must run the *current* image: the code that has to survive a bad
        # image and roll it back cannot have come out of that image.
        assert helper["Image"] == "sha256:oldimageid", helper["Image"]
        assert helper["Entrypoint"] == ["python", "-m", "app.services.selfupdate"]
        assert helper["HostConfig"]["AutoRemove"] is True
        assert f"{sock}:{sock}" in helper["HostConfig"]["Binds"], helper["HostConfig"]
        assert helper["HostConfig"]["NetworkMode"] == "none"

        # The plan carries the app's environment. It must not be world-readable.
        plan_file = selfupdate.plan_path(data)
        assert os.path.exists(plan_file)
        assert oct(os.stat(plan_file).st_mode)[-3:] == "600", oct(os.stat(plan_file).st_mode)
    finally:
        stop()


def test_pulling_the_same_image_again_changes_nothing():
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    fake.add_image(NEW_IMAGE, "sha256:oldimageid", OLD_IMAGE_CONFIG)
    sock, stop = serve(fake)
    try:
        selfupdate.start_update(NEW_TAG, data_dir=data, socket_path=sock, self_container="nal")
        status = selfupdate.read_status(data)
        assert status["state"] == "done", status
        assert "Already running" in status["message"], status
        assert not [n for n in fake.containers if n.startswith("nal-updater-")]
    finally:
        stop()


def test_a_tag_that_does_not_exist_fails_before_anything_is_touched():
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    fake.pull_error = "manifest unknown"
    sock, stop = serve(fake)
    try:
        selfupdate.start_update("pr-999", data_dir=data, socket_path=sock, self_container="nal")
        status = selfupdate.read_status(data)
        assert status["state"] == "failed", status
        assert "manifest unknown" in status["error"], status
        assert "nal" in fake.containers, "the running container must be untouched"
        assert not [n for n in fake.containers if n.startswith("nal-updater-")]
    finally:
        stop()


def _plan_for(fake, sock, data):
    """Run the pull phase, then read back the plan the helper would be given."""
    selfupdate.start_update(
        NEW_TAG, data_dir=data, socket_path=sock, self_container="nal"
    )
    with open(selfupdate.plan_path(data), encoding="utf-8") as handle:
        return json.load(handle)


def test_the_helper_swaps_the_container_and_reports_done():
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    sock, stop = serve(fake)
    try:
        plan = _plan_for(fake, sock, data)
        plan["health_timeout"] = 10
        assert selfupdate._run_helper(plan) == 0

        assert "nal" in fake.containers, list(fake.containers)
        replacement = fake.containers["nal"]
        assert replacement["_created_from"]["Image"] == NEW_IMAGE
        assert replacement["State"]["Running"] is True
        # The parked previous container is cleared away once the new one is up.
        assert "nal-nal-previous" not in fake.containers, list(fake.containers)

        status = selfupdate.read_status(data)
        assert status["state"] == "done", status
        assert NEW_IMAGE in status["message"], status
    finally:
        stop()


def test_a_replacement_that_never_becomes_healthy_is_rolled_back():
    """The promise that makes installing an unmerged branch reasonable."""
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    fake.health_for_new = "unhealthy"
    sock, stop = serve(fake)
    try:
        plan = _plan_for(fake, sock, data)
        plan["health_timeout"] = 10
        assert selfupdate._run_helper(plan) == 1

        assert "nal" in fake.containers, list(fake.containers)
        restored = fake.containers["nal"]
        # The original container itself is back, not a rebuild of it.
        assert restored["Id"] == "c" * 64, restored["Id"]
        assert restored["State"]["Running"] is True, restored["State"]

        status = selfupdate.read_status(data)
        assert status["state"] == "rolled_back", status
        assert "previous version has been restored" in status["message"], status
    finally:
        stop()


def test_a_replacement_that_will_not_even_start_is_rolled_back():
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    sock, stop = serve(fake)
    try:
        plan = _plan_for(fake, sock, data)
        plan["health_timeout"] = 10
        fake.create_error = "invalid mount config"
        assert selfupdate._run_helper(plan) == 1

        assert fake.containers["nal"]["Id"] == "c" * 64
        assert fake.containers["nal"]["State"]["Running"] is True
        status = selfupdate.read_status(data)
        assert status["state"] == "rolled_back", status
        assert "invalid mount config" in status["error"], status
    finally:
        stop()


def test_a_rollback_can_rebuild_a_container_that_was_removed_on_stop():
    """--rm containers vanish when stopped; the saved spec is the only way back."""
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    fake.health_for_new = "unhealthy"
    sock, stop = serve(fake)
    try:
        plan = _plan_for(fake, sock, data)
        plan["health_timeout"] = 10
        plan["auto_remove"] = True
        assert selfupdate._run_helper(plan) == 1

        assert "nal" in fake.containers, list(fake.containers)
        restored = fake.containers["nal"]["_created_from"]
        assert restored["Image"] == OLD_IMAGE, restored["Image"]
        assert "ODDS_API_KEY=secret-odds" in restored["Env"], restored["Env"]
        assert selfupdate.read_status(data)["state"] == "rolled_back"
    finally:
        stop()


def test_the_extra_networks_are_reconnected_after_the_swap():
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    fake.containers["nal"]["NetworkSettings"]["Networks"]["swag_default"] = {
        "Aliases": ["nal"]
    }
    sock, stop = serve(fake)
    try:
        plan = _plan_for(fake, sock, data)
        plan["health_timeout"] = 10
        assert selfupdate._run_helper(plan) == 0
        assert fake.containers["nal"]["_networks_connected"] == ["swag_default"]
    finally:
        stop()


# ---------------------------------------------------------------------------
# Reporting what is going on
# ---------------------------------------------------------------------------

def test_the_socket_tells_the_three_failures_apart():
    """"Unavailable" would be useless — each of these needs a different fix."""
    missing = docker_api.socket_status(os.path.join(a_data_dir(), "nope.sock"))
    assert missing["reason"] == "not_mounted", missing

    dead = os.path.join(a_data_dir(), "dead.sock")
    import socket as _socket
    server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    server.bind(dead)
    server.close()
    assert docker_api.socket_status(dead)["reason"] in ("no_daemon", "no_permission")


def test_status_survives_being_read_by_a_different_process():
    """The page that starts an update is answered by the container that replaces it."""
    data = a_data_dir()
    assert selfupdate.read_status(data)["state"] == "idle"
    selfupdate.write_status(data, state="pulling", percent=12, message="Pulling…")
    assert selfupdate.is_running(data) is True
    selfupdate.write_status(data, state="done", message="Updated.")
    assert selfupdate.is_running(data) is False
    assert selfupdate.read_status(data)["percent"] == 12, "fields merge, not replace"


# ---------------------------------------------------------------------------
# Version list
# ---------------------------------------------------------------------------

def _backdate_status(data, hours=2):
    """Age the status file, as a helper that died mid-swap would leave it."""
    path = selfupdate.status_path(data)
    with open(path, encoding="utf-8") as handle:
        status = json.load(handle)
    stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    status["updated_at"] = stamp.isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(status, handle)


def test_a_wedged_update_stops_blocking_the_next_one():
    """A stuck status file must not become the reason to go and use the VPN."""
    data = a_data_dir()
    selfupdate.write_status(data, state="swapping", message="Stopping…")
    assert selfupdate.is_running(data) is True

    _backdate_status(data)
    assert selfupdate.is_stalled(selfupdate.read_status(data)) is True
    assert selfupdate.is_running(data) is False, "a dead update must not block a retry"


def test_a_stalled_update_is_reported_as_failed_not_as_running():
    db = SessionLocal()
    boss, _player = make_users(db)
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    with wired_up(fake, data):
        selfupdate.write_status(data, state="swapping", message="Stopping…")
        _backdate_status(data)

        reported = client_for(boss).get("/admin/update/status").json()
        assert reported["running"] is False, reported
        assert reported["state"] == "failed", reported
        assert reported.get("stalled") is True, reported

        # And a new attempt is allowed straight through.
        resp = client_for(boss).post(
            "/admin/update/start", data={"tag": NEW_TAG, "confirm": NEW_TAG},
            follow_redirects=False,
        )
        assert "error=" not in resp.headers["location"], resp.headers["location"]
        assert fake.pulled == [NEW_IMAGE], fake.pulled
    db.close()


def test_versions_are_grouped_and_pull_requests_are_named():
    tags = ["latest", "pr-54", "pr-7", "1.2.0", "v1.0.0", "sha-a1b2c3d", "weird!!"]
    pulls = {54: {"title": "Prize payouts", "state": "open", "url": "http://x/54"}}
    rows = [registry._classify(tag, pulls) for tag in tags]
    rows = [r for r in rows if r]

    assert len(rows) == 6, "the malformed tag is dropped"
    by_tag = {r["tag"]: r for r in rows}
    assert by_tag["pr-54"]["label"] == "PR #54 — Prize payouts", by_tag["pr-54"]
    assert by_tag["pr-54"]["state"] == "open"
    assert by_tag["pr-7"]["label"] == "PR #7", by_tag["pr-7"]
    assert by_tag["latest"]["kind"] == registry.KIND_LATEST
    assert by_tag["1.2.0"]["kind"] == registry.KIND_RELEASE
    assert by_tag["sha-a1b2c3d"]["kind"] == registry.KIND_COMMIT

    rows.sort(key=lambda r: (r["sort"], r["tag"]))
    assert rows[0]["tag"] == "latest", [r["tag"] for r in rows]
    # Newest release first, newest PR first.
    assert [r["tag"] for r in rows if r["kind"] == registry.KIND_RELEASE] == ["1.2.0", "v1.0.0"]
    assert [r["tag"] for r in rows if r["kind"] == registry.KIND_PR] == ["pr-54", "pr-7"]


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------

_next_id = iter(range(1, 10_000))


def make_users(db):
    n = next(_next_id)
    boss = User(first_name="Ada", last_name="Admin", email=f"a{n}@example.com",
                password_hash="x", role=Role.admin)
    player = User(first_name="Pat", last_name="Player", email=f"p{n}@example.com",
                  password_hash="x", role=Role.player)
    db.add(boss)
    db.add(player)
    db.commit()
    return boss, player


def client_for(user):
    return TestClient(app, cookies={"access_token": create_access_token(user.id)})


def test_only_admins_can_see_or_start_an_update():
    db = SessionLocal()
    _boss, player = make_users(db)
    client = client_for(player)

    page = client.get("/admin/update", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/", page.status_code
    assert client.get("/admin/update/status").status_code == 403
    started = client.post(
        "/admin/update/start", data={"tag": "latest", "confirm": "latest"},
        follow_redirects=False,
    )
    assert started.status_code == 403, started.status_code
    db.close()


def test_the_page_explains_itself_when_docker_is_not_wired_up():
    db = SessionLocal()
    boss, _player = make_users(db)
    with no_docker():
        resp = client_for(boss).get("/admin/update")
        assert resp.status_code == 200, resp.status_code
        assert "In-app updates are not enabled" in resp.text
        assert "docker.sock" in resp.text, "it has to say what to add"
    db.close()


def test_an_update_cannot_start_without_a_socket():
    db = SessionLocal()
    boss, _player = make_users(db)
    with no_docker():
        resp = client_for(boss).post(
            "/admin/update/start", data={"tag": "latest", "confirm": "latest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.status_code
        assert "error=" in resp.headers["location"], resp.headers["location"]
        assert "docker.sock" in resp.headers["location"], resp.headers["location"]
    db.close()


class no_docker:
    """Guarantee there is no Docker socket, whatever the host has.

    A CI runner has a real one at the default path, so a test asserting the
    "not wired up" behaviour has to establish that itself.
    """

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="nal-nodocker-")
        self.saved = os.environ.get("DOCKER_SOCKET")
        # Named as the real one is, in a directory where it does not exist, so
        # the page's "add this path" instructions still read like the real ones.
        os.environ["DOCKER_SOCKET"] = os.path.join(self.dir, "docker.sock")
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop("DOCKER_SOCKET", None)
        else:
            os.environ["DOCKER_SOCKET"] = self.saved


class wired_up:
    """Point the app at a stand-in daemon for the length of a `with` block."""

    def __init__(self, fake, data, container="nal"):
        self.fake, self.data, self.container = fake, data, container

    def __enter__(self):
        self.sock, self.stop = serve(self.fake)
        self.saved = (
            os.environ.get("DOCKER_SOCKET"),
            os.environ.get("DATA_DIR"),
            os.environ.get("UPDATE_CONTAINER_NAME"),
        )
        os.environ["DOCKER_SOCKET"] = self.sock
        os.environ["DATA_DIR"] = self.data
        # Outside a container there is no cgroup to read the id out of, which
        # is exactly the case the override exists for.
        os.environ["UPDATE_CONTAINER_NAME"] = self.container
        return self

    def __exit__(self, *exc):
        keys = ("DOCKER_SOCKET", "DATA_DIR", "UPDATE_CONTAINER_NAME")
        for key, value in zip(keys, self.saved):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.stop()


def test_a_mistyped_confirmation_does_not_install_anything():
    """Typing the tag is the last thing between a click and the league restarting."""
    db = SessionLocal()
    boss, _player = make_users(db)
    data = a_data_dir()
    with wired_up(a_fake(data_dir=data), data):
        resp = client_for(boss).post(
            "/admin/update/start", data={"tag": "pr-54", "confirm": "latest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "confirmation" in resp.headers["location"], resp.headers["location"]
        assert not a_fake().pulled, "nothing may be pulled"
        assert selfupdate.read_status(data)["state"] == "idle", selfupdate.read_status(data)
    db.close()


def test_installing_from_the_page_runs_the_whole_thing():
    """Click Install and the image is pulled and the swap handed to a helper."""
    db = SessionLocal()
    boss, _player = make_users(db)
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    with wired_up(fake, data):
        resp = client_for(boss).post(
            "/admin/update/start", data={"tag": NEW_TAG, "confirm": NEW_TAG},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.status_code
        assert "error=" not in resp.headers["location"], resp.headers["location"]

        # The background task runs once the response has been sent.
        assert fake.pulled == [NEW_IMAGE], fake.pulled
        helpers = [n for n in fake.containers if n.startswith("nal-updater-")]
        assert len(helpers) == 1, list(fake.containers)

        status = client_for(boss).get("/admin/update/status").json()
        assert status["state"] == "swapping", status
        assert status["running"] is True, status
        assert status["started_by"] == "Ada Admin", status

        # And a second click while it is in flight is refused.
        again = client_for(boss).post(
            "/admin/update/start", data={"tag": NEW_TAG, "confirm": NEW_TAG},
            follow_redirects=False,
        )
        assert "already%20running" in again.headers["location"], again.headers["location"]
    db.close()


def test_not_knowing_which_container_we_are_says_how_to_fix_it():
    """A failure nobody could diagnose from "it did not work"."""
    db = SessionLocal()
    boss, _player = make_users(db)
    data = a_data_dir()
    saved_hostname = os.environ.get("HOSTNAME")
    with wired_up(a_fake(data_dir=data), data, container=""):
        os.environ.pop("UPDATE_CONTAINER_NAME", None)
        os.environ["HOSTNAME"] = "not-a-container"
        client_for(boss).post(
            "/admin/update/start", data={"tag": NEW_TAG, "confirm": NEW_TAG},
            follow_redirects=False,
        )
        status = selfupdate.read_status(data)
        assert status["state"] == "failed", status
        # Docker's own "no such container" says nothing an admin could act on.
        assert "UPDATE_CONTAINER_NAME" in status["error"], status
        assert "Updater Settings" in status["error"], status
    if saved_hostname is None:
        os.environ.pop("HOSTNAME", None)
    else:
        os.environ["HOSTNAME"] = saved_hostname
    db.close()


def test_the_status_endpoint_never_leaks_the_plan():
    """The plan carries the signing key; the status the page polls must not."""
    db = SessionLocal()
    boss, _player = make_users(db)
    data = a_data_dir()
    fake = a_fake(data_dir=data)
    with wired_up(fake, data):
        client_for(boss).post(
            "/admin/update/start", data={"tag": NEW_TAG, "confirm": NEW_TAG},
            follow_redirects=False,
        )
        body = client_for(boss).get("/admin/update/status").text
        assert "secret-odds" not in body, body
        assert "ODDS_API_KEY" not in body, body
    db.close()


def test_the_registry_token_is_saved_but_never_rendered_back():
    db = SessionLocal()
    boss, _player = make_users(db)
    client = client_for(boss)
    client.post("/admin/update/settings", data={
        "registry_user": "jeffreyrdubois",
        "registry_token": "ghp_supersecrettoken",
        "container_name": "nal",
    }, follow_redirects=False)

    saved = db.query(AppSetting).filter(AppSetting.key == "update_registry_token").first()
    db.refresh(saved)
    assert saved.value == "ghp_supersecrettoken", saved.value

    page = client.get("/admin/update")
    assert "ghp_supersecrettoken" not in page.text, "a token must never come back out"
    assert "jeffreyrdubois" in page.text, "the username is not a secret"

    # A blank box means "leave it alone", not "delete it".
    client.post("/admin/update/settings", data={
        "registry_user": "jeffreyrdubois", "registry_token": "", "container_name": "nal",
    }, follow_redirects=False)
    db.expire_all()
    still = db.query(AppSetting).filter(AppSetting.key == "update_registry_token").first()
    assert still.value == "ghp_supersecrettoken", still.value

    client.post("/admin/update/settings", data={
        "registry_user": "jeffreyrdubois", "registry_token": "", "clear_token": "1",
        "container_name": "nal",
    }, follow_redirects=False)
    db.expire_all()
    cleared = db.query(AppSetting).filter(AppSetting.key == "update_registry_token").first()
    assert cleared.value == "", cleared.value
    db.close()


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    # The page reads the registry; these tests are about our own code, not the
    # network, so the list is primed rather than fetched.
    registry._CACHE.update({
        "fetched_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        "tags": ["latest", "pr-54", "sha-a1b2c3d"],
        "error": "",
    })
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok  {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failures} passed, {failures} failed")
    raise SystemExit(1 if failures else 0)
