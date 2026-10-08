"""Process-level launcher checks; remote Hub/compute/Argo remain fixtures."""

import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
COMMAND = [sys.executable, "-m", "e2x_course_hub.cps.argo_app"]


def environment():
    values = os.environ.copy()
    for name in list(values):
        if name.startswith(("ARGO_USER_", "JUPYTERHUB_")):
            del values[name]
    values["PYTHONPATH"] = str(ROOT)
    ca_file = ssl.get_default_verify_paths().cafile
    if not ca_file:
        raise RuntimeError("these process tests require the system CA bundle")
    values.update({
        "ARGO_USER_SOURCE": "cps",
        "ARGO_USER_OAUTH_CLIENT_ID": "service-cps-argo-ui",
        "ARGO_USER_OAUTH_CLIENT_SECRET": "launcher-secret-not-for-logs",
        "ARGO_USER_COOKIE_SECRET": "launcher-cookie-secret-not-for-logs-" * 2,
        "ARGO_USER_PUBLIC_ORIGIN": "https://argo.example.invalid",
        "ARGO_USER_HUB_API_URL": "https://hub.example.invalid/hub/api",
        "ARGO_USER_HUB_AUTHORIZATION_URL": "https://hub.example.invalid/hub/api/oauth2/authorize",
        "ARGO_USER_COMPUTE_URL": "https://compute.example.invalid",
        "ARGO_USER_NATIVE_ARGO_URL": "https://native.example.invalid/argo",
        "ARGO_USER_HUB_CA_FILE": ca_file,
        "ARGO_USER_COMPUTE_CA_FILE": ca_file,
        "ARGO_USER_NATIVE_ARGO_CA_FILE": ca_file,
    })
    return values


class LauncherTest(unittest.TestCase):
    def run_failure(self, *, changes=None, remove=(), arguments=(), message):
        env = environment()
        env.update(changes or {})
        for name in remove:
            env.pop(name)
        result = subprocess.run(COMMAND + list(arguments), cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("launcher-secret-not-for-logs", result.stderr + result.stdout)
        self.assertNotIn("launcher-cookie-secret-not-for-logs", result.stderr + result.stdout)
        for name in ("ARGO_USER_OAUTH_CLIENT_SECRET", "ARGO_USER_COOKIE_SECRET"):
            if env.get(name):
                self.assertNotIn(env[name], result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_help_runs_without_credentials(self):
        result = subprocess.run(COMMAND + ["--help"], cwd=ROOT,
                                env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT)},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--host", result.stdout)
        self.assertIn("--port", result.stdout)
        self.assertIn("--shutdown-seconds", result.stdout)

    def test_missing_dedicated_config_does_not_fall_back_to_admin_credentials(self):
        self.run_failure(changes={"JUPYTERHUB_CLIENT_ID": "service-admin",
                                  "JUPYTERHUB_API_TOKEN": "launcher-secret-not-for-logs"},
                         remove=("ARGO_USER_OAUTH_CLIENT_SECRET",),
                         message="ARGO_USER_OAUTH_CLIENT_SECRET is required")

    def test_invalid_cookie_secret_fails_without_disclosure(self):
        self.run_failure(changes={"ARGO_USER_COOKIE_SECRET": "private-short-cookie"},
                         message="cookie secret")

    def test_plaintext_remote_origin_is_rejected(self):
        self.run_failure(changes={"ARGO_USER_COMPUTE_URL": "http://compute.example.invalid"},
                         message="compute_url must be a fixed HTTPS URL")

    def test_ca_contents_are_validated_before_listening(self):
        with tempfile.NamedTemporaryFile() as ca:
            ca.write(b"not a CA certificate\n")
            ca.flush()
            for variable in ("HUB", "COMPUTE", "NATIVE_ARGO"):
                with self.subTest(variable=variable):
                    self.run_failure(changes={f"ARGO_USER_{variable}_CA_FILE": ca.name},
                                     message=variable.lower() + "_ca_file must contain")

    def test_listen_address_must_be_an_ip_literal(self):
        for host in ("localhost", "https://host.invalid", "fe80::1%eth0", "224.0.0.1"):
            with self.subTest(host=host):
                self.run_failure(arguments=("--host", host), message="host must be")

    def test_port_is_bounded_for_cli_and_environment(self):
        for port in ("0", "-1", "65536", "invalid"):
            with self.subTest(port=port):
                self.run_failure(arguments=("--port", port), message="port")
                self.run_failure(changes={"ARGO_USER_LISTEN_PORT": port}, message="port")

    def test_shutdown_deadline_is_bounded_for_cli_and_environment(self):
        for seconds in ("0", "-1", "121", "invalid"):
            with self.subTest(seconds=seconds):
                self.run_failure(arguments=("--shutdown-seconds", seconds), message="shutdown")
                self.run_failure(changes={"ARGO_USER_SHUTDOWN_SECONDS": seconds},
                                 message="shutdown")

    def test_occupied_port_fails_clearly_without_traceback(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            result = subprocess.run(COMMAND + ["--port", str(port)], cwd=ROOT,
                                    env=environment(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("could not bind", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("launcher-secret-not-for-logs", result.stderr)

    def test_process_serves_factory_routes_and_sigterm_closes_idle_connection(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        env = environment()
        # Explicit CLI options override invalid defaults from the environment.
        env.update({"ARGO_USER_LISTEN_HOST": "not-an-address",
                    "ARGO_USER_LISTEN_PORT": "invalid", "ARGO_USER_SHUTDOWN_SECONDS": "invalid"})
        process = subprocess.Popen(COMMAND + ["--host", "127.0.0.1", "--port", str(port),
                                             "--shutdown-seconds", "1"],
                                   cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        idle = None
        connection = None
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail("launcher exited: " + process.communicate()[1])
                try:
                    idle = socket.create_connection(("127.0.0.1", port), timeout=0.1)
                    break
                except OSError:
                    time.sleep(0.02)
            self.assertIsNotNone(idle, "launcher never bound its socket")
            connection = HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/argo/")
            response = connection.getresponse()
            self.assertEqual(response.status, 302)
            self.assertTrue(response.getheader("Location").startswith(
                "https://hub.example.invalid/hub/api/oauth2/authorize?"))
            state = parse_qs(urlsplit(response.getheader("Location")).query)["state"][0]
            cookies = SimpleCookie()
            for header, value in response.getheaders():
                if header.lower() == "set-cookie":
                    cookies.load(value)
            cookies["service-cps-argo-ui-oauth-state"] = "mismatched-state"
            cookie_header = "; ".join(name + "=" + morsel.value
                                      for name, morsel in cookies.items())
            response.read()
            # Access logs must not disclose callback codes or OAuth state queries.
            connection.request("GET", "/argo/oauth_callback?code=private-code&state=private-state")
            rejected = connection.getresponse()
            self.assertEqual(rejected.status, 403)
            rejected.read()
            connection.request("GET", "/argo/oauth_callback?" + urlencode({
                "code": "private-mismatched-code", "state": state,
            }), headers={"Cookie": cookie_header})
            rejected = connection.getresponse()
            self.assertEqual(rejected.status, 403)
            rejected.read()
            process.terminate()
            stdout, stderr = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertNotIn("private-code", stdout + stderr)
            self.assertNotIn("private-state", stdout + stderr)
            self.assertNotIn("private-mismatched-code", stdout + stderr)
            self.assertNotIn(state, stdout + stderr)
            self.assertNotIn("launcher-secret-not-for-logs", stdout + stderr)
            self.assertNotIn("Traceback", stderr)
        finally:
            if connection is not None:
                connection.close()
            if idle is not None:
                idle.close()
            if process.poll() is None:
                process.kill()
            process.communicate()


if __name__ == "__main__":
    unittest.main()
