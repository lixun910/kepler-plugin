#!/usr/bin/env python
"""Check the plugin's dataset upload against the server's routes, with no server.

`upload_dataset` is three calls — reserve a row, PUT the bytes, commit — and the
shape is forced by the platform: Vercel refuses a function request body over
4.5 MB, so a Parquet file cannot go through the app. That makes the upload the
one place in this plugin where the client and the server have to agree on a
protocol rather than on a URL, and the failure mode when they disagree is a
`next dev` deployment that works and a real one that does not.

So this stands a stub in front of `ApiClient` and reads what it actually sends.
It is not a substitute for the plugin's smoke test — nothing here builds a map —
and it does not need Auth0, Neon, S3 or Stripe, which is the point: it can run
in a checkout on a laptop with no credentials at all.

    .venv/bin/python scripts/check_upload_contract.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from kepler_mcp.api import ApiClient, ApiError, PARQUET_CONTENT_TYPE  # noqa: E402
from kepler_mcp.config import Settings  # noqa: E402

PASSED = 0
FAILED = 0


def check(ok: bool, what: str, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok    {what}")
    else:
        FAILED += 1
        print(f"  FAIL  {what}" + (f"\n        {detail}" if detail else ""))


class Stub:
    """A server that answers the three calls and records what it was sent."""

    def __init__(self, *, fail_commits: int = 0, put_status: int = 200, presign_status: int = 201):
        self.presign_calls: list[dict] = []
        self.put_calls: list[dict] = []
        self.commit_calls: list[dict] = []
        #: How many commits answer `upload_missing` before one succeeds. Set it
        #: past the client's retry budget to reach the give-up path.
        self.fail_commits = fail_commits
        self.put_status = put_status
        self.presign_status = presign_status
        self.commits = 0
        self.presign_count = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "Stub":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # noqa: D102 - quieter test output
                pass

            def _read(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                raw = self._read()
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {"__unparsed__": raw[:200].decode("utf-8", "replace")}

                if self.path == "/api/datasets":
                    stub.presign_count += 1
                    stub.presign_calls.append(
                        {"body": body, "authorization": self.headers.get("Authorization")}
                    )
                    if stub.presign_status != 201:
                        self._send(stub.presign_status, {"error": "payment required", "code": "payment_required"})
                        return
                    self._send(
                        201,
                        {
                            "dataset": {
                                "id": "ds-1",
                                "table": body.get("table"),
                                "ready": False,
                                # The stub points the PUT back at itself, so the
                                # bytes land somewhere this script can read.
                                "uploadUrl": f"{stub.url}/s3/ds-1",
                                "uploadMethod": "PUT",
                            }
                        },
                    )
                    return

                if self.path == "/api/datasets/ds-1/commit":
                    stub.commits += 1
                    stub.commit_calls.append(
                        {"body": body, "authorization": self.headers.get("Authorization")}
                    )
                    if stub.commits <= stub.fail_commits:
                        self._send(
                            409,
                            {
                                "error": "No upload arrived at the URL this dataset was given.",
                                "code": "upload_missing",
                            },
                        )
                        return
                    self._send(
                        200,
                        {
                            "dataset": {
                                "id": "ds-1",
                                "table": body.get("__table__") or "places",
                                "rowCount": body.get("rowCount"),
                                "ready": True,
                            }
                        },
                    )
                    return

                self._send(404, {"error": "no such route", "code": "not_found"})

            def do_PUT(self) -> None:
                raw = self._read()
                stub.put_calls.append(
                    {
                        "path": self.path,
                        "bytes": raw,
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                    }
                )
                if stub.put_status != 200:
                    body = b"<Error><Code>SignatureDoesNotMatch</Code></Error>"
                    self.send_response(stub.put_status)
                    self.send_header("Content-Type", "application/xml")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler


class Tokens:
    """A token provider that never mints anything."""

    def get_token(self, force_refresh: bool = False) -> str:
        return "stub-token"


def client_for(stub: Stub) -> ApiClient:
    settings = Settings(server_url=stub.url)
    return ApiClient(settings, Tokens())  # type: ignore[arg-type]


PAYLOAD = b"PAR1" + b"\x00" * 64 + b"PAR1"


def main() -> int:
    print("== the three calls, in order")
    with Stub() as stub:
        api = client_for(stub)
        result = api.upload_dataset(
            PAYLOAD, table="places", label="Places", kind="point", row_count=42
        )

        check(len(stub.presign_calls) == 1, "one reserve call, not one per retry")
        body = stub.presign_calls[0]["body"]
        check(body.get("table") == "places", "the reserve names the table the config references", str(body))
        check(body.get("kind") == "point", "and the kind the viewer draws it as", str(body))
        check(body.get("label") == "Places", "and the label a person sees", str(body))
        check(
            body.get("rowCount") == 42,
            "the row count goes up front too, so an abandoned row still lists honestly",
            str(body),
        )
        check("file" not in body, "no multipart part — the bytes never enter the app")

        check(len(stub.put_calls) == 1, "the bytes go up in one PUT")
        put = stub.put_calls[0]
        check(put["bytes"] == PAYLOAD, "and they arrive byte for byte", f"{len(put['bytes'])} bytes")
        check(
            put["authorization"] is None,
            "the PUT carries no Authorization — a bearer token beside a presigned "
            "signature makes S3 try header-based SigV4 and fail",
            str(put["authorization"]),
        )
        check(
            put["content_type"] == PARQUET_CONTENT_TYPE,
            "and names the content type the server signed for",
            str(put["content_type"]),
        )
        check(put["path"] == "/s3/ds-1", "straight at storage, not at the app", put["path"])

        check(len(stub.commit_calls) == 1, "one commit")
        check(
            stub.commit_calls[0]["body"].get("rowCount") == 42,
            "which carries the row count the server cannot compute",
            str(stub.commit_calls[0]["body"]),
        )
        check(
            stub.commit_calls[0]["authorization"] == "Bearer stub-token",
            "and does carry the token — the commit is the app's route, not S3's",
            str(stub.commit_calls[0]["authorization"]),
        )
        check(result.get("id") == "ds-1", "the dataset id comes back", str(result))

    print("== a PUT that did not land")
    with Stub(fail_commits=1) as stub:
        api = client_for(stub)
        result = api.upload_dataset(PAYLOAD, table="places", kind="point", row_count=7)
        check(
            len(stub.put_calls) == 2,
            "a missing upload is retried by sending the bytes again, not by "
            "committing again",
            str(len(stub.put_calls)),
        )
        check(len(stub.commit_calls) == 2, "and the commit is tried once more", str(len(stub.commit_calls)))
        check(result.get("id") == "ds-1", "which then succeeds", str(result))
        check(stub.presign_count == 1, "without reserving a second row", str(stub.presign_count))

    print("== a PUT that never lands")
    with Stub(fail_commits=99) as stub:
        api = client_for(stub)
        try:
            api.upload_dataset(PAYLOAD, table="places", kind="point", row_count=7)
        except ApiError as exc:
            check(exc.code == "upload_missing", "the refusal keeps the server's code", str(exc.code))
            check(
                "never reached storage" in str(exc),
                "and says what happened rather than that a name is taken",
                str(exc),
            )
        else:
            check(False, "a permanent upload_missing is raised", "no error")
        check(len(stub.put_calls) == 2, "the retry is bounded at two PUTs", str(len(stub.put_calls)))
        check(stub.presign_count == 1, "and still reserves one row", str(stub.presign_count))

    print("== storage refusing the bytes")
    with Stub(put_status=403) as stub:
        api = client_for(stub)
        try:
            api.upload_dataset(PAYLOAD, table="places", kind="point", row_count=1)
        except ApiError as exc:
            check("Storage refused" in str(exc), "a rejected PUT is reported as storage's refusal", str(exc))
            check("403" in str(exc), "with the status", str(exc))
        else:
            check(False, "a rejected PUT raises", "no error")
        check(len(stub.commit_calls) == 0, "and nothing is committed", str(len(stub.commit_calls)))

    print("== a refusal on the reserve call")
    with Stub(presign_status=402) as stub:
        api = client_for(stub)
        try:
            api.upload_dataset(PAYLOAD, table="places", kind="point", row_count=1)
        except ApiError as exc:
            check(exc.code == "payment_required", "the 402's code survives to the caller", str(exc.code))
            check(
                "free allowance" in str(exc),
                "and the message names the fix rather than the status",
                str(exc),
            )
        else:
            check(False, "a 402 raises", "no error")
        check(len(stub.put_calls) == 0, "no bytes are sent for a dataset that was refused", str(len(stub.put_calls)))

    print("== an empty dataset")
    with Stub() as stub:
        api = client_for(stub)
        try:
            api.upload_dataset(b"", table="places", kind="point")
        except ApiError as exc:
            check("empty" in str(exc), "refused here rather than stored as zero bytes", str(exc))
        else:
            check(False, "an empty payload raises", "no error")
        check(stub.presign_count == 0, "and never reaches the server", str(stub.presign_count))

    print()
    if FAILED:
        print(f"{FAILED} check(s) failed, {PASSED} passed.")
        return 1
    print(f"All checks passed. ({PASSED})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
