#!/usr/bin/env python3
"""Install every released solc linux-amd64 binary under the pipeline's naming.

Fetched from the official Solidity binary index.
The index (`list.json`) maps each release version to a filename of the form
`solc-linux-amd64-v0.8.29+commit.ab55807c`; the autoprove pipeline (and the
LLM prompts) instead expect `solcX.Y`, where X.Y are the minor/patch numbers
of `0.X.Y` (so 0.8.29 -> solc8.29). We download each release, verify it
against the sha256 published in the index, install it as `solcX.Y`, and
point a bare `solc` symlink at a sensible default.

Run at image build time (the base image's system python3 is enough — only
stdlib is used). Requires outbound HTTPS and ca-certificates.
"""

import hashlib
import json
import os
import shutil
import urllib.request

BASE = "https://binaries.soliditylang.org/linux-amd64"
BIN_DIR = "/usr/local/bin"
DEFAULT_VERSION = "0.8.29"  # what `solc` (unversioned) points at, if present

# The binary index is CDN-fronted and rejects the default `Python-urllib`
# User-Agent with a 403, so send an explicit one.
_HEADERS = {"User-Agent": "autoprover-image-build"}


def _open(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS))


def _solc_name(version: str) -> str:
    # "0.8.29" -> "solc8.29"
    _, minor, patch = version.split(".")
    return f"solc{minor}.{patch}"


def main() -> None:
    with _open(f"{BASE}/list.json") as resp:
        index = json.load(resp)

    # builds[] carries the sha256 per filename; releases{} maps version->filename.
    sha_by_path = {b["path"]: b.get("sha256", "") for b in index["builds"]}
    releases: dict[str, str] = index["releases"]

    for version, path in releases.items():
        dest = os.path.join(BIN_DIR, _solc_name(version))
        with _open(f"{BASE}/{path}") as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out)

        want = sha_by_path.get(path, "").removeprefix("0x")
        if want:
            got = hashlib.sha256(open(dest, "rb").read()).hexdigest()
            if got != want:
                raise SystemExit(f"checksum mismatch for {path}: {got} != {want}")

        os.chmod(dest, 0o755)

    # Bare `solc` -> default version if available, else the newest release.
    default = DEFAULT_VERSION if DEFAULT_VERSION in releases else max(
        releases, key=lambda v: tuple(int(p) for p in v.split("."))
    )
    link = os.path.join(BIN_DIR, "solc")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(os.path.join(BIN_DIR, _solc_name(default)), link)

    print(f"installed {len(releases)} solc binaries; default `solc` -> {default}")


if __name__ == "__main__":
    main()
