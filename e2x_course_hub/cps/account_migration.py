"""Plan by default; execute a protected review through the owning Source API."""

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .account_adoption import encode


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError("Review and token files must be owned private regular files (0600)")
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Private review file is too large")
    return data


def parse_json(raw):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate review field")
            result[key] = value
        return result

    def finite(value):
        raise ValueError("Nonfinite review value")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=finite)


def request(source_url, token, path, body):
    parsed = urllib.parse.urlsplit(source_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Exact HTTPS owning Source URL required")
    response = urllib.request.build_opener(NoRedirect()).open(
        urllib.request.Request(
            source_url.rstrip("/") + "/api/" + path,
            data=encode(body).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        ),
        timeout=30,
    )
    with response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Source review response is too large")
    return parse_json(raw)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--source-url", required=True, help="HTTPS console service URL including its prefix"
    )
    result.add_argument(
        "--token-file",
        required=True,
        type=Path,
        help="0600 file containing the actual owning admin Hub token",
    )
    result.add_argument(
        "--packet",
        required=True,
        type=Path,
        help="Private plan output, or exact reviewed packet input",
    )
    result.add_argument("--workspace")
    result.add_argument("--reason")
    result.add_argument("--provenance")
    result.add_argument("--execute", action="store_true")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        token = read_private(args.token_file).decode().strip()
        if not token or any(ch.isspace() for ch in token):
            raise ValueError("Nonempty private owning admin token required")
        if args.execute:
            if args.workspace or args.reason or args.provenance:
                raise ValueError("Execute uses only the exact reviewed packet, with no overrides")
            review = parse_json(read_private(args.packet))
            identifier = review["binding"]["id"]
            result = request(
                args.source_url,
                token,
                "workspace-account-adoptions/"
                + urllib.parse.quote(identifier, safe="")
                + "/execute",
                {"review": review},
            )
            if result != {"state": "ready", "provenance": "reviewed_legacy_adoption"}:
                raise ValueError("Source did not confirm reviewed adoption readiness")
            print("Reviewed adoption confirmed ready.")
        else:
            if not args.workspace or not args.reason or not args.provenance:
                raise ValueError("Plan requires workspace, reason and provenance")
            result = request(
                args.source_url,
                token,
                "workspace-account-adoptions/"
                + urllib.parse.quote(args.workspace, safe="")
                + "/plan",
                {"reason": args.reason, "provenance": args.provenance},
            )
            fd = os.open(args.packet, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(encode(result) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            print("Read-only review packet saved privately. No account or group was changed.")
        return 0
    except urllib.error.HTTPError as error:
        print(
            "Source rejected the request (HTTP %d); no completion is claimed." % error.code,
            file=sys.stderr,
        )
    except (OSError, ValueError, KeyError, TypeError):
        # Never echo tokens, a packet, private paths, identities or server bodies.
        print(
            "Migration request failed; check private inputs and owning Source "
            "preflight. No completion is claimed.",
            file=sys.stderr,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
