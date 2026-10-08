"""Run the independent personal Argo Hub OAuth service."""

import argparse
import asyncio
import ipaddress
import logging
import os
import signal
import ssl
import sys

from tornado.httpclient import AsyncHTTPClient
from tornado.httpserver import HTTPServer

from .argo_service import ArgoServiceConfig, create_argo_service

LOG = logging.getLogger(__name__)


def _host(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise argparse.ArgumentTypeError("host must be an IPv4 or IPv6 listen address") from None
    if "%" in value or address.is_multicast:
        raise argparse.ArgumentTypeError("host must be an unscoped, non-multicast listen address")
    return str(address)


def _port(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def _shutdown_seconds(value):
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "shutdown seconds must be an integer from 1 to 120") from None
    if not 1 <= seconds <= 120:
        raise argparse.ArgumentTypeError("shutdown seconds must be an integer from 1 to 120")
    return seconds


def _parser():
    parser = argparse.ArgumentParser(
        prog="personal-argo-service",
        description="Run the read-only personal Argo service with dedicated ARGO_USER_* settings.",
    )
    parser.add_argument("--host", type=_host,
                        default=os.environ.get("ARGO_USER_LISTEN_HOST", "127.0.0.1"),
                        help="listen IP (ARGO_USER_LISTEN_HOST; default: 127.0.0.1)")
    parser.add_argument("--port", type=_port,
                        default=os.environ.get("ARGO_USER_LISTEN_PORT", "10202"),
                        help="listen port (ARGO_USER_LISTEN_PORT; default: 10202)")
    parser.add_argument("--shutdown-seconds", type=_shutdown_seconds,
                        default=os.environ.get("ARGO_USER_SHUTDOWN_SECONDS", "30"),
                        help="transfer drain deadline, 1-120 "
                             "(ARGO_USER_SHUTDOWN_SECONDS; default: 30)")
    return parser


def _validate_ca_bundles(config):
    # The factory checks paths. The executable must also fail before binding if
    # any configured bundle is unreadable or cannot load as trusted certificates.
    for name in ("hub_ca_file", "compute_ca_file", "native_argo_ca_file"):
        try:
            ssl.create_default_context(cafile=getattr(config, name))
        except (OSError, ssl.SSLError):
            raise ValueError(name + " must contain readable trusted CA certificates") from None


def _log_request(handler):
    # OAuth callback queries contain one-use codes and state. Do not include
    # request URLs, cookies or headers in this process's normal access logs.
    LOG.info("HTTP %s %s %.2fms", handler.request.method, handler.get_status(),
             handler.request.request_time() * 1000)


def _private_application_log(record):
    # HubOAuth and RequestHandler error diagnostics include callback state,
    # request URIs or upstream bodies even when access logging omits queries.
    # This dedicated process records the status/timing separately above.
    if record.name in ("tornado.application", "tornado.general"):
        record.msg = "Application request diagnostic; see HTTP status log"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
    return True


async def _shutdown(server, app, seconds):
    server.stop()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    limits = app.settings["argo_request_limits"]
    while limits.active_requests and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0, deadline - loop.time())))
    if limits.active_requests:
        LOG.warning("Transfer drain deadline reached; closing remaining connections")
    try:
        await asyncio.wait_for(server.close_all_connections(),
                               timeout=max(0.001, deadline - loop.time()))
    except asyncio.TimeoutError:
        LOG.warning("Connection close deadline reached")


async def serve(config, *, host, port, shutdown_seconds):
    """Bind only after validation; drain bounded transfers on SIGTERM or SIGINT."""
    app = create_argo_service(config)
    app.settings["log_function"] = _log_request
    server = HTTPServer(app, xheaders=False, max_body_size=64 * 1024,
                        max_header_size=16 * 1024, idle_connection_timeout=30,
                        body_timeout=30)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    previous_handlers = {}
    try:
        server.listen(port, address=host)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            loop.add_signal_handler(signum, stop.set)
        LOG.info("%s listening on %s:%d", config.service_name, host, port)
        await stop.wait()
    finally:
        try:
            await _shutdown(server, app, shutdown_seconds)
        finally:
            for signum, previous in previous_handlers.items():
                loop.remove_signal_handler(signum)
                signal.signal(signum, previous)
            app.settings["argo_http_client"].close()
            # HubOAuth uses the loop-local singleton for identity/token calls.
            AsyncHTTPClient().close()


def main(argv=None):
    parser = _parser()
    options = parser.parse_args(argv)
    try:
        config = ArgoServiceConfig.from_env()
        _validate_ca_bundles(config)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(_private_application_log)
    try:
        asyncio.run(serve(config, host=options.host, port=options.port,
                          shutdown_seconds=options.shutdown_seconds))
    except OSError:
        # Bind failures must be clear without printing configuration or secrets.
        print("personal-argo-service: could not bind the configured listening socket",
              file=sys.stderr)
        return 1
    except (NotImplementedError, RuntimeError):
        print("personal-argo-service: could not initialize the service event loop", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
