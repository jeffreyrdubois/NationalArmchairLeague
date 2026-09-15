"""A very small Docker Engine API client, speaking HTTP over the unix socket.

The app talks to Docker for exactly one reason — replacing its own container
with a newer image — so this covers only the handful of endpoints that takes,
and does it with nothing but the standard library. Adding the official SDK for
six endpoints would pull a dependency tree into an image whose whole selling
point is that it has no moving parts.

Nothing here is used unless ``/var/run/docker.sock`` has been mounted into the
container. Without it, ``available()`` is False and the update page says so
rather than failing.
"""
from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import socket
import urllib.parse
from typing import Callable, Iterator

logger = logging.getLogger(__name__)

def default_socket() -> str:
    """Where the Docker socket is, read when it is needed rather than at import.

    Resolving this once at import would freeze it into every default argument,
    which makes the setting untestable and means relocating the socket needs a
    rebuild rather than an environment variable.
    """
    return os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")

# Pinned rather than negotiated: 1.41 ships with Docker 20.10 (2020) and every
# release since, and covers every field used here. Asking for the daemon's
# newest version instead would mean the request bodies change shape under us
# when the host upgrades Docker.
API_VERSION = "v1.41"


class DockerError(RuntimeError):
    """A Docker API call that came back as an error.

    ``status`` is the HTTP status, so callers can tell "no such container"
    (404) from "the daemon said no" (409) from "the socket is not usable".
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over an AF_UNIX socket instead of TCP."""

    def __init__(self, socket_path: str, timeout: float = 60.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def socket_status(socket_path: str | None = None) -> dict:
    """Why the Docker socket is or isn't usable, in terms an admin can act on.

    The three failures look identical from the app's side but need completely
    different fixes, so they are told apart here rather than collapsed into one
    "unavailable": the socket is not mounted, it is mounted but this process
    cannot open it (a group problem), or the daemon itself is not answering.
    """
    socket_path = socket_path or default_socket()
    if not os.path.exists(socket_path):
        return {
            "ok": False,
            "reason": "not_mounted",
            "detail": (
                f"{socket_path} is not present in the container. Add it as a "
                "volume mapping to enable in-app updates."
            ),
        }
    if not os.access(socket_path, os.R_OK | os.W_OK):
        return {
            "ok": False,
            "reason": "no_permission",
            "detail": (
                f"{socket_path} is mounted but this process cannot open it — "
                "the app user is not in the socket's group. Restart the "
                "container; the entrypoint adds the group automatically."
            ),
        }
    try:
        client = DockerClient(socket_path, timeout=10)
        version = client.version()
    except Exception as exc:  # noqa: BLE001 - reported, never raised onward
        return {"ok": False, "reason": "no_daemon", "detail": str(exc)}
    return {
        "ok": True,
        "reason": None,
        "detail": f"Docker {version.get('Version', '?')} (API {version.get('ApiVersion', '?')})",
    }


def available(socket_path: str | None = None) -> bool:
    return socket_status(socket_path)["ok"]


def registry_auth_header(username: str, password: str) -> str:
    """The X-Registry-Auth value Docker expects: base64url of a JSON credential."""
    blob = json.dumps({"username": username, "password": password}).encode()
    return base64.urlsafe_b64encode(blob).decode().rstrip("=")


class DockerClient:
    """Just enough of the Engine API to swap one container for another."""

    def __init__(self, socket_path: str | None = None, timeout: float = 60.0):
        self.socket_path = socket_path or default_socket()
        self.timeout = timeout

    # -- plumbing ----------------------------------------------------------

    def _open(self, method: str, path: str, body=None, headers=None, timeout=None):
        conn = _UnixHTTPConnection(self.socket_path, timeout or self.timeout)
        payload = None
        send_headers = {"Host": "docker", "Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body).encode()
            send_headers["Content-Type"] = "application/json"
        send_headers.update(headers or {})
        try:
            conn.request(method, f"/{API_VERSION}{path}", body=payload, headers=send_headers)
            return conn, conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise DockerError(f"Could not reach Docker on {self.socket_path}: {exc}") from exc

    @staticmethod
    def _fail(response, raw: bytes) -> DockerError:
        text = raw.decode("utf-8", "replace").strip()
        try:
            text = json.loads(text).get("message", text)
        except (ValueError, AttributeError):
            pass
        return DockerError(
            f"Docker returned {response.status}: {text[:400]}", status=response.status
        )

    def _call(self, method: str, path: str, body=None, headers=None, timeout=None):
        conn, response = self._open(method, path, body, headers, timeout)
        try:
            raw = response.read()
            if response.status >= 400:
                raise self._fail(response, raw)
            if not raw:
                return None
            try:
                return json.loads(raw)
            except ValueError:
                return raw.decode("utf-8", "replace")
        finally:
            conn.close()

    def _stream(self, method: str, path: str, headers=None, timeout=None) -> Iterator[dict]:
        """Iterate the JSON objects Docker streams from a long-running call."""
        conn, response = self._open(method, path, None, headers, timeout)
        try:
            if response.status >= 400:
                raise self._fail(response, response.read())
            buffer = b""
            while True:
                chunk = response.read(4096)
                if not chunk:
                    break
                buffer += chunk
                # Docker emits one JSON object per line, but a chunk boundary
                # can land mid-object, so incomplete tails are kept back.
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        logger.debug("Unparsed line from Docker stream: %r", line[:200])
            if buffer.strip():
                try:
                    yield json.loads(buffer.strip())
                except ValueError:
                    pass
        finally:
            conn.close()

    # -- the endpoints we actually use -------------------------------------

    def version(self) -> dict:
        return self._call("GET", "/version", timeout=10) or {}

    def inspect_container(self, name_or_id: str) -> dict:
        return self._call("GET", f"/containers/{urllib.parse.quote(name_or_id)}/json")

    def inspect_image(self, ref: str) -> dict:
        return self._call("GET", f"/images/{urllib.parse.quote(ref, safe='')}/json")

    def pull_image(
        self,
        repository: str,
        tag: str,
        auth_header: str | None = None,
        on_progress: Callable[[dict], None] | None = None,
        timeout: float = 900.0,
    ) -> None:
        """Pull one tag, reporting progress as the daemon streams it.

        Docker answers 200 and then reports the failure inside the stream, so a
        pull that could not find the tag looks like a success until the body is
        read to the end. The ``error`` key is what actually decides it.
        """
        query = urllib.parse.urlencode({"fromImage": repository, "tag": tag})
        headers = {"X-Registry-Auth": auth_header} if auth_header else None
        error: str | None = None
        for event in self._stream("POST", f"/images/create?{query}", headers, timeout):
            if event.get("error"):
                error = str(event.get("error"))
            elif on_progress:
                on_progress(event)
        if error:
            raise DockerError(f"Pull of {repository}:{tag} failed: {error[:400]}")

    def create_container(self, name: str, spec: dict) -> str:
        query = urllib.parse.urlencode({"name": name})
        result = self._call("POST", f"/containers/create?{query}", body=spec)
        return result["Id"]

    def start_container(self, name_or_id: str) -> None:
        self._call("POST", f"/containers/{urllib.parse.quote(name_or_id)}/start")

    def stop_container(self, name_or_id: str, seconds: int = 20) -> None:
        query = urllib.parse.urlencode({"t": seconds})
        try:
            self._call(
                "POST",
                f"/containers/{urllib.parse.quote(name_or_id)}/stop?{query}",
                timeout=seconds + 30,
            )
        except DockerError as exc:
            # 304 means it had already stopped, which is the state we wanted.
            if exc.status != 304:
                raise

    def remove_container(self, name_or_id: str, force: bool = True) -> None:
        query = urllib.parse.urlencode({"force": "true" if force else "false", "v": "false"})
        try:
            self._call("DELETE", f"/containers/{urllib.parse.quote(name_or_id)}?{query}")
        except DockerError as exc:
            if exc.status != 404:
                raise

    def rename_container(self, name_or_id: str, new_name: str) -> None:
        query = urllib.parse.urlencode({"name": new_name})
        self._call("POST", f"/containers/{urllib.parse.quote(name_or_id)}/rename?{query}")

    def connect_network(self, network: str, container: str, config: dict | None = None) -> None:
        self._call(
            "POST",
            f"/networks/{urllib.parse.quote(network)}/connect",
            body={"Container": container, "EndpointConfig": config or {}},
        )
