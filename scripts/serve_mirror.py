#!/usr/bin/env python3
"""Serve the pica package source from this machine, for a device that cannot reach GitHub.

A source is two things: an entry descriptor with its backend indexes, and the
artifacts those indexes name. The indexes are small and live in the repository;
the artifacts live in GitHub Releases. When the device can reach neither, both
have to come from somewhere it *can* reach — usually the LAN.

This serves exactly that, on one port:

    /r/index.json               the entry descriptor, unchanged
    /r/<mgr>/index.json         that backend's index, with download_url rewritten
    /r/repo.json                schema 2, same rewrite
    /latest/<file>              the artifact, fetched once and cached

Only `download_url` is rewritten; every other field is passed through as the
repository published it, so `sha256` still describes the bytes the client gets.
The rewrite is the whole trick: a mirror is an index that points at itself.

Point the device at `http://<this machine>:<port>/r`. pica accepts plain HTTP
for private ranges by default, so no `allow_insecure_http` is needed on a LAN.

    python3 serve_mirror.py                  # 0.0.0.0:8080, LAN address printed
    python3 serve_mirror.py --port 9000 --advertised 192.168.1.10
"""
import argparse
import hashlib
import http.server
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ROOT = "https://raw.githubusercontent.com/miaoermua/pica-pkg-repo/main/r"
USER_AGENT = "pica-mirror/1.0"
JSON_PATHS = ("index.json", "repo.json")


def lan_address():
    """This machine's address on the LAN, as the device would see it.

    A connected UDP socket picks the interface the default route uses without
    sending a packet. Falls back to the hostname, then to loopback.
    """
    for probe in ("192.168.1.1", "10.0.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 80))
                return sock.getsockname()[0]
        except OSError:
            continue
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


class Mirror:
    def __init__(self, root, base, cache_dir):
        self.root = root.rstrip("/")
        self.base = base
        self.cache_dir = cache_dir
        self.by_name = {}
        self.descriptor = None
        os.makedirs(cache_dir, exist_ok=True)

    def upstream(self, path):
        return f"{self.root}/{path}"

    def fetch_json(self, path, attempts=3):
        """Read one JSON file from upstream, retried.

        The indexes are tiny, but they travel the same flaky path the artifacts
        do — a proxy that drops one connection must not stop the server from
        starting.
        """
        last = None
        for attempt in range(attempts):
            try:
                request = urllib.request.Request(
                    self.upstream(path), headers={"User-Agent": USER_AGENT}
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except (urllib.error.URLError, OSError, ValueError) as err:
                last = err
                time.sleep(1 + attempt)
        raise last

    def load(self):
        """Read the descriptor and repo.json, and index the artifacts by filename."""
        self.descriptor = self.fetch_json("index.json")
        repo = self.fetch_json("repo.json")
        for pkg in repo.get("packages", []):
            self.by_name.setdefault(pkg["filename"], pkg)
        return self.descriptor

    def rewrite(self, index):
        """Point every entry at this mirror, leaving the rest of the entry alone."""
        for pkg in index.get("packages", []):
            pkg["download_url"] = f"{self.base}/latest/{pkg['filename']}"
        return index

    def json_for(self, path):
        if path == "index.json":
            return self.descriptor
        if path == "repo.json":
            return self.rewrite(self.fetch_json("repo.json"))
        return self.rewrite(self.fetch_json(path))

    def artifact(self, name):
        """Path to the cached artifact, downloading and verifying it on first use.

        Retried: a mirror that answers 502 because the first attempt lost its
        connection is worse than one that waits a second and tries again. A
        proxy in the path drops long downloads often enough to matter.
        """
        target = os.path.join(self.cache_dir, os.path.basename(name))
        entry = self.by_name.get(name)
        if entry is None:
            raise KeyError(name)
        if os.path.isfile(target) and self.verified(target, entry.get("sha256")):
            return target, entry

        upstream = entry.get("download_url")
        if not upstream:
            raise KeyError(name)
        last = None
        for attempt in range(3):
            try:
                self.download(upstream, target, entry.get("sha256"), name)
                return target, entry
            except (urllib.error.URLError, OSError, ValueError) as err:
                last = err
                time.sleep(1 + attempt)
        raise last

    def download(self, url, target, expected, name):
        partial = target + ".part"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=120) as response, open(partial, "wb") as handle:
                while chunk := response.read(1024 * 256):
                    handle.write(chunk)
            if not self.verified(partial, expected):
                raise ValueError(f"{name}: sha256 does not match the index")
            os.replace(partial, target)
        finally:
            if os.path.exists(partial):
                os.remove(partial)

    @staticmethod
    def verified(path, expected):
        if not expected:
            return True
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected.lower()


def make_handler(mirror):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003 - the base class names it
            sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

        def do_HEAD(self):
            self.handle_request(head=True)

        def do_GET(self):
            self.handle_request(head=False)

        def send_json(self, body, head):
            payload = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(payload)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            if not head:
                self.wfile.write(payload)

        def send_error_text(self, status, message):
            payload = json.dumps({"error": message}).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def handle_request(self, head):
            path = self.path.split("?")[0]
            if path == "/":
                self.send_response(200)
                body = (
                    f"pica mirror\n\nsource root: {mirror.base}/r\n"
                    f"entries: {len(mirror.by_name)}\n"
                ).encode()
                self.send_header("content-type", "text/plain; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                if not head:
                    self.wfile.write(body)
                return

            if path.startswith("/latest/"):
                name = path[len("/latest/"):]
                if "/" in name or name not in mirror.by_name:
                    self.send_error_text(404, f"no such artifact: {name}")
                    return
                entry = mirror.by_name[name]
                if head:
                    self.send_response(200)
                    self.send_header("content-type", "application/gzip")
                    if entry.get("csize"):
                        self.send_header("content-length", str(entry["csize"]))
                    self.end_headers()
                    return
                try:
                    target, _ = mirror.artifact(name)
                except (KeyError, ValueError, OSError, urllib.error.URLError) as err:
                    self.send_error_text(502, str(err))
                    return
                size = os.path.getsize(target)
                self.send_response(200)
                self.send_header("content-type", "application/gzip")
                self.send_header("content-length", str(size))
                self.end_headers()
                with open(target, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 256), b""):
                        self.wfile.write(chunk)
                return

            if not path.startswith("/r/"):
                self.send_error_text(404, f"no such route: {path}")
                return
            relative = path[len("/r/"):]
            if relative not in JSON_PATHS and not relative.endswith("/index.json"):
                self.send_error_text(404, f"no such index: {relative}")
                return
            try:
                body = mirror.json_for(relative)
            except (urllib.error.URLError, OSError, ValueError) as err:
                self.send_error_text(502, f"upstream {relative}: {err}")
                return
            self.send_json(body, head)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Serve the pica source over the LAN.")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="upstream source root")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--advertised", help="address the device should use; defaults to the LAN address")
    parser.add_argument("--cache-dir", default=".mirror-cache")
    args = parser.parse_args()

    host = args.advertised or lan_address()
    base = f"http://{host}:{args.port}"

    mirror = Mirror(args.root, base, args.cache_dir)
    print(f"reading {mirror.root}/index.json …", flush=True)
    descriptor = mirror.load()
    managers = ", ".join(descriptor.get("managers", {})) or "none"

    print(f"upstream : {mirror.root}")
    print(f"artifacts: {len(mirror.by_name)} ({managers})")
    print(f"\n在设备上执行：\n  pica setup\n  软件源根地址：{base}/r\n")
    print(f"或者直接写配置：\n  {{\"repos\":[{{\"name\":\"main\",\"url\":\"{base}/r\",\"backends\":[\"opkg\",\"pica\"]}}]}}\n")
    print(f"listening on http://{args.bind}:{args.port}/r", flush=True)

    server = http.server.ThreadingHTTPServer((args.bind, args.port), make_handler(mirror))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    sys.exit(main())
