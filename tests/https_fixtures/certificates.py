"""Generate or inspect throwaway IP certificates inside a network-none helper."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys


DIRECTORY = Path("/data/fixture")


def state():
    der = ssl.PEM_cert_to_DER_cert((DIRECTORY / "server.crt").read_text(encoding="ascii"))
    return {"status": "ok", "certificate_sha256": hashlib.sha256(der).hexdigest(),
            "marker": (DIRECTORY / "marker").read_text(encoding="ascii")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--marker")
    arguments = parser.parse_args()
    if arguments.inspect:
        print(json.dumps(state(), sort_keys=True))
        return 0
    if not shutil.which("openssl") or not arguments.marker:
        return 1
    os.umask(0o077)
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=False)
    def openssl(*command):
        result = subprocess.run(["openssl", *command], cwd=DIRECTORY, capture_output=True, timeout=20)
        if result.returncode:
            raise RuntimeError("fixture_openssl_failed")
    openssl("version")
    openssl("req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "2", "-nodes",
            "-subj", "/CN=AI Notes isolated test CA", "-keyout", "ca.key", "-out", "ca.crt",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    openssl("req", "-newkey", "rsa:2048", "-sha256", "-nodes", "-subj", "/CN=isolated-ip",
            "-keyout", "server.key", "-out", "server.csr")
    (DIRECTORY / "leaf.ext").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=IP:47.111.12.85\n", encoding="ascii")
    openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-CAcreateserial", "-sha256", "-days", "1", "-extfile", "leaf.ext", "-out", "server.crt")
    (DIRECTORY / "marker").write_text(arguments.marker, encoding="ascii")
    # The probe receives only the public CA, never the CA or server private key.
    shutil.copyfile(DIRECTORY / "ca.crt", "/fixtures/ca.crt")
    os.chmod("/fixtures/ca.crt", 0o644)
    print(json.dumps(state(), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"status": "failed", "code": "fixture_temporary_certificate_failed"}))
        sys.exit(1)
