#!/usr/bin/env python3
"""Send a long-reply request to the Pico and wait for the BMCU's answer.

docs/BMCU_MONITOR_STREAM_V2.md section 7.4. The token is read from the
environment (BMCU_CONTROL_TOKEN) or from pico/secrets.py, never from the
command line, so it does not land in shell history.

    python tools/long_reply_client.py --host 192.168.1.15 --link 0 \
        --type 0x0411 --match 01 --reply 01000000
    python tools/long_reply_client.py --host 192.168.1.15 --link 0 \
        --type 0x0411 --clear
    python tools/long_reply_client.py --host 192.168.1.15 --link 0 --status
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PATH = "/api/control/long-reply"


def load_token() -> str:
    token = os.environ.get("BMCU_CONTROL_TOKEN")
    if token:
        return token
    secrets = ROOT / "pico" / "secrets.py"
    if secrets.exists():
        found = re.search(r'^BMCU_CONTROL_TOKEN\s*=\s*["\']([^"\']+)["\']',
                          secrets.read_text(encoding="utf-8"), re.M)
        if found:
            return found.group(1)
    raise SystemExit("no token: set BMCU_CONTROL_TOKEN or add it to pico/secrets.py")


def call(host: str, token: str, method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request("http://%s%s" % (host, path), data=data,
                                     method=method)
    request.add_header("X-BMCU-Control-Token", token)
    if body is not None:
        request.add_header("Content-Type", "application/json")
        request.add_header("X-BMCU-Control-Action", "long-reply")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        text = error.read()
        try:
            return error.code, json.loads(text)
        except ValueError:
            return error.code, text.decode(errors="replace").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True)
    parser.add_argument("--link", type=int, required=True)
    parser.add_argument("--type", help="frame type, e.g. 0x0411")
    parser.add_argument("--match", default="", help="payload prefix, hex")
    parser.add_argument("--reply", default="", help="response payload, hex")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--status", action="store_true",
                        help="only read the last request's state")
    parser.add_argument("--wait-s", type=float, default=3.0)
    args = parser.parse_args()
    token = load_token()
    status_path = "%s?link=%d" % (PATH, args.link)

    if not args.status:
        if not args.type:
            parser.error("--type is required unless --status")
        body = {"link": args.link, "type": args.type}
        if args.clear:
            body["clear"] = True
        else:
            body["reply"] = args.reply
        if args.match:
            body["match"] = args.match
        code, answer = call(args.host, token, "POST", PATH, body)
        print("POST", code, json.dumps(answer))
        if code != 202:
            return 1
        sequence = answer["sequence"]
        deadline = time.monotonic() + args.wait_s
        while time.monotonic() < deadline:
            code, view = call(args.host, token, "GET", status_path)
            if code == 200 and view.get("sequence") == sequence and \
                    view.get("state") != "sent":
                break
            time.sleep(0.2)
    code, view = call(args.host, token, "GET", status_path)
    print("GET ", code, json.dumps(view))
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
