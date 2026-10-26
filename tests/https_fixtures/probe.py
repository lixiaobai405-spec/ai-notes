"""Verify the real frontend through internal TCP, using an IP TLS identity.

Python/OpenSSL does not send SNI for numeric server_hostname values. Keeping
check_hostname enabled verifies the IP SAN while connecting to Docker DNS.
Only this process loads the throwaway CA; no system trust store is modified.
"""

import argparse
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import sys
import time


SITE_IP = "47.111.12.85"
HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
           "Referrer-Policy": "same-origin"}


class Failure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Failure(code)


def context():
    value = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    value.minimum_version = ssl.TLSVersion.TLSv1_2
    value.load_verify_locations(cafile="/fixtures/ca.crt")
    return value


class Connection(http.client.HTTPSConnection):
    def __init__(self):
        super().__init__("frontend", port=443, timeout=12, context=context())
        self.client_ip = None
        self.fingerprint = None

    def connect(self):
        raw = socket.create_connection(("frontend", 443), timeout=self.timeout)
        self.client_ip = raw.getsockname()[0]
        # A numeric identity enables IP SAN verification and suppresses SNI.
        require(ipaddress.ip_address(SITE_IP).version == 4, "fixture_identity_not_ip")
        self.sock = self._context.wrap_socket(raw, server_hostname=SITE_IP)
        self.fingerprint = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()


def request(path, *, headers=None, method="GET"):
    connection = Connection()
    try:
        connection.request(method, path, headers={"Host": SITE_IP, **(headers or {})})
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body, connection.client_ip, connection.fingerprint
    finally:
        connection.close()


def headers_valid(headers):
    lowered = {key.lower(): value for key, value in headers.items()}
    return all(lowered.get(key.lower()) == value for key, value in HEADERS.items())


def wait_ready():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            response = request("/")
            if response[0] == 200:
                return response
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.3)
    raise Failure("fixture_frontend_not_ready")


def assert_forwarded(payload, client_ip, identity):
    headers = payload["headers"]
    require(headers.get("authorization") == f"Bearer fixture-{identity}", "fixture_authorization_not_preserved")
    require(headers.get("origin") == f"https://{SITE_IP}", "fixture_origin_not_preserved")
    require(headers.get("x-csrf-token") == f"fixture-csrf-{identity}", "fixture_csrf_not_preserved")
    require(headers.get("x-forwarded-for") == client_ip, "fixture_forged_forwarded_for_trusted")
    require(headers.get("x-forwarded-proto") == "https", "fixture_forwarded_protocol_invalid")
    require(headers.get("x-forwarded-host") == SITE_IP, "fixture_forwarded_host_invalid")


def synthetic_headers(identity):
    return {"Authorization": f"Bearer fixture-{identity}", "Origin": f"https://{SITE_IP}",
            "X-CSRF-Token": f"fixture-csrf-{identity}", "X-Forwarded-For": "198.51.100.7, 203.0.113.9",
            "X-Forwarded-Proto": "http", "X-Forwarded-Host": "attacker.invalid"}


def upload(size, identity):
    connection = Connection()
    digest = hashlib.sha256()
    chunk = bytes(range(256)) * 256
    started = time.monotonic()
    try:
        connection.putrequest("POST", "/api/fixture/upload", skip_host=True)
        for name, value in {"Host": SITE_IP, "Content-Type": "application/octet-stream",
                            "Content-Length": str(size), **synthetic_headers(identity)}.items():
            connection.putheader(name, value)
        connection.endheaders()
        remaining = size
        while remaining:
            portion = chunk[:min(remaining, len(chunk))]
            digest.update(portion)
            connection.send(portion)
            remaining -= len(portion)
        response = connection.getresponse()
        require(response.status == 200, "fixture_upload_status_invalid")
        payload = json.loads(response.read())
        require(payload.get("bytes") == size and payload.get("sha256") == digest.hexdigest(),
                "fixture_upload_content_changed")
        assert_forwarded(payload, connection.client_ip, identity)
        return {"bytes": size, "sha256": digest.hexdigest(), "seconds": round(time.monotonic() - started, 3)}
    finally:
        connection.close()


def sse():
    connection = Connection()
    started = time.monotonic()
    heartbeat_times = []
    ids = []
    first_seconds = None
    try:
        connection.request("GET", "/api/fixture/events", headers={"Host": SITE_IP})
        response = connection.getresponse()
        require(response.status == 200 and response.getheader("Content-Type", "").startswith("text/event-stream"),
                "fixture_sse_response_invalid")
        require(headers_valid(dict(response.getheaders())), "fixture_sse_security_headers_missing")
        while True:
            line = response.readline()
            if not line:
                break
            elapsed = time.monotonic() - started
            if first_seconds is None:
                first_seconds = elapsed
                require(first_seconds < 5, "fixture_sse_initial_buffering")
            if line.startswith(b": heartbeat "):
                heartbeat_times.append(elapsed)
            if line.startswith(b"id: "):
                ids.append(line.decode("ascii").strip()[4:])
        duration = time.monotonic() - started
        require(duration >= 63 and ids == ["1", "2"], "fixture_sse_not_long_lived")
        require(len(heartbeat_times) == 32, "fixture_sse_heartbeats_missing")
        gaps = [heartbeat_times[0], *[right - left for left, right in zip(heartbeat_times, heartbeat_times[1:])]]
        require(max(gaps) < 6 and min(gaps) > 0.5, "fixture_sse_heartbeat_buffering")
    finally:
        connection.close()
    status, headers, body, _client, _fingerprint = request(
        "/api/fixture/events?reconnect=1", headers={"Last-Event-ID": "2"})
    require(status == 200 and b"id: 3\n" in body and b'"last_event_id": "2"' in body,
            "fixture_sse_reconnect_failed")
    return {"method": "GET", "duration_seconds": round(duration, 3),
            "first_byte_seconds": round(first_seconds, 3), "heartbeats": len(heartbeat_times),
            "max_heartbeat_gap_seconds": round(max(gaps), 3), "last_event_id_reconnect": "passed"}


def run(fingerprint_only=False):
    root_status, root_headers, index, _client_ip, fingerprint = wait_ready()
    result = {"status": "ok", "certificate_sha256": fingerprint,
              "tls_ip_identity": SITE_IP, "tls_sni": "omitted_for_numeric_identity"}
    if fingerprint_only:
        return result
    # A different numeric identity must fail with the same process-local CA.
    raw = socket.create_connection(("frontend", 443), timeout=10)
    try:
        try:
            with context().wrap_socket(raw, server_hostname="203.0.113.254"):
                raise Failure("fixture_tls_wrong_ip_accepted")
        except ssl.SSLCertVerificationError:
            pass
    finally:
        raw.close()
    require(root_status == 200 and headers_valid(root_headers), "fixture_security_headers_missing")
    connection = http.client.HTTPConnection("frontend", 80, timeout=10)
    try:
        connection.request("GET", "/notebooks/fixture/deep?mode=probe", headers={"Host": SITE_IP})
        response = connection.getresponse()
        require(response.status in (301, 302, 307, 308) and response.getheader("Location") ==
                f"https://{SITE_IP}/notebooks/fixture/deep?mode=probe", "fixture_https_redirect_invalid")
        response.read()
    finally:
        connection.close()
    deep = request("/notebooks/fixture/deep")
    require(deep[0] == 200 and deep[2] == index and headers_valid(deep[1]), "fixture_spa_fallback_invalid")
    assets = sorted(set(re.findall(rb'(?:src|href)=["\'](/assets/[^"\']+)["\']', index)))
    require(bool(assets), "fixture_spa_assets_missing")
    for asset in assets[:4]:
        status, headers, body, _ip, _fingerprint = request(asset.decode("ascii"))
        require(status == 200 and bool(body) and body != index and headers_valid(headers), "fixture_static_asset_invalid")
    for path in ("/docs", "/docs/", "/docs/oauth2-redirect", "/redoc", "/redoc/", "/openapi.json"):
        status, headers, _body, _ip, _fingerprint = request(path)
        require(status == 404 and headers_valid(headers), "fixture_api_docs_exposed")
    for identity in ("a", "b"):
        status, headers, body, client_ip, _fingerprint = request(
            "/api/fixture/inspect", headers=synthetic_headers(identity))
        require(status == 200 and headers_valid(headers), "fixture_proxy_status_invalid")
        assert_forwarded(json.loads(body), client_ip, identity)
    result["checks"] = {name: "passed" for name in (
        "ip_certificate_without_sni", "wrong_ip_rejected", "http_https_redirect", "spa_deep_link_and_assets",
        "api_docs_404", "security_headers", "synthetic_identity_header_separation", "forwarded_headers_cleaned")}
    result["uploads"] = [upload(20 * 1024 * 1024, "image"), upload(50 * 1024 * 1024, "pdf")]
    result["sse"] = sse()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fingerprint-only", action="store_true")
    args = parser.parse_args()
    try:
        output = run(args.fingerprint_only)
    except Failure as error:
        output = {"status": "failed", "code": str(error)}
    except Exception:
        output = {"status": "failed", "code": "fixture_probe_transport_failed"}
    print(json.dumps(output, sort_keys=True))
    # The orchestrator reads structured failures; never print request material.
    sys.exit(0)
