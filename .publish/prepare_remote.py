#!/usr/bin/env python3
"""Reconstruct and verify the exact Adaptive ISO Boot v0.2.0 source history.

The 32-commit v0.1/V2 planning baseline is downloaded from an immutable,
SHA-256-pinned bundle. The 16 granular cross-platform commits are reconstructed
from the patch chunks stored in this branch. The script fails closed on every
identity, history, subject, version, or cleanliness mismatch.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

BASE_BUNDLE_URL = (
    "https://aiboot-bootstrap-96cc45c22b69.netlify.app/"
    "adaptive-iso-boot-v2-final.git.bundle"
)
BASE_BUNDLE_SHA256 = "96cc45c22b69032634cc30bf4c2932e07bd9ac140b4e6d65bbddce3c3c2fa6f0"
BASE_BUNDLE_MAX_BYTES = 32 * 1024 * 1024
BASE_HEAD = "283d91c970f099d1fdbf4fc072140c6cb2444f12"
BASE_COMMIT_COUNT = 32
V010_TAG_OBJECT = "971e5d026ee4b630f032f321d83c815ea00391fe"
PATCH_GZIP_SHA256 = "b9b16f7688ee507fe5a5633dc6a3d0e9651ea853277b9bb2e00be7731577bfb2"
EXPECTED_FINAL_COMMIT_COUNT = 48
EXPECTED_VERSION = "0.2.0"
EXPECTED_SUBJECTS = (
    "Make filesystem operations portable across hosts",
    "Discover mounted Ventoy drives cross-platform",
    "Add one-command Ventoy checker and synchronizer",
    "Expose drive discovery and safe sync commands",
    "Report cross-platform capabilities and drive health",
    "Add images with verified copy and automatic setup",
    "Expose one-command image add workflow",
    "Package native executables for Windows macOS and Linux",
    "Make the release quality gate cross-platform",
    "Test and release native builds on all major desktops",
    "Fix portable release checksum generation",
    "Remove obsolete one-time publication handoff",
    "Create redacted support bundles for failed boots",
    "Expose privacy-safe support bundle command",
    "Allow diagnostics from read-only Ventoy media",
    "Document the cross-platform zero-friction workflow",
)


class PublicationError(RuntimeError):
    """A checksum, history, or source invariant did not match."""


def run(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode != 0:
        detail = ""
        if capture:
            detail = f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        raise PublicationError(f"command failed ({result.returncode}): {' '.join(args)}{detail}")
    return result.stdout.strip() if capture else ""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise PublicationError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def download_base_bundle() -> bytes:
    request = urllib.request.Request(
        BASE_BUNDLE_URL,
        headers={"User-Agent": "adaptive-iso-boot-publisher/0.2.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read(BASE_BUNDLE_MAX_BYTES + 1)
    except Exception as exc:
        raise PublicationError(f"could not download pinned base bundle: {exc}") from exc
    if len(payload) > BASE_BUNDLE_MAX_BYTES:
        raise PublicationError("pinned base bundle exceeded the 32 MiB safety cap")
    require_equal(sha256(payload), BASE_BUNDLE_SHA256, "base bundle SHA-256")
    return payload


def decode_patch(root: Path) -> bytes:
    paths = sorted((root / ".publish/patch").glob("chunk-*"))
    if not paths:
        raise PublicationError("no staged cross-platform patch chunks were found")
    encoded = "".join(path.read_text(encoding="ascii").strip() for path in paths)
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise PublicationError(f"staged patch is not valid Base64: {exc}") from exc
    require_equal(sha256(compressed), PATCH_GZIP_SHA256, "patch gzip SHA-256")
    try:
        return gzip.decompress(compressed)
    except OSError as exc:
        raise PublicationError(f"patch gzip could not be decompressed: {exc}") from exc


def git_value(repo: Path, *args: str) -> str:
    return run("git", *args, cwd=repo, capture=True)


def verify_base(repo: Path) -> None:
    require_equal(git_value(repo, "rev-parse", "HEAD"), BASE_HEAD, "base HEAD")
    require_equal(
        int(git_value(repo, "rev-list", "--count", "HEAD")),
        BASE_COMMIT_COUNT,
        "base commit count",
    )
    require_equal(
        git_value(repo, "rev-parse", "refs/tags/v0.1.0"),
        V010_TAG_OBJECT,
        "v0.1.0 tag object",
    )


def verify_final(repo: Path) -> str:
    require_equal(
        int(git_value(repo, "rev-list", "--count", "HEAD")),
        EXPECTED_FINAL_COMMIT_COUNT,
        "final commit count",
    )
    subject_text = git_value(repo, "log", "--format=%s", "--reverse", f"{BASE_HEAD}..HEAD")
    require_equal(tuple(subject_text.splitlines()), EXPECTED_SUBJECTS, "applied commit subjects")

    init_text = (repo / "src/adaptive_iso_boot/__init__.py").read_text(encoding="utf-8")
    pyproject_text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    if f'__version__ = "{EXPECTED_VERSION}"' not in init_text:
        raise PublicationError("package __version__ is not 0.2.0")
    if f'version = "{EXPECTED_VERSION}"' not in pyproject_text:
        raise PublicationError("pyproject version is not 0.2.0")
    if git_value(repo, "status", "--porcelain"):
        raise PublicationError("reconstructed repository is unexpectedly dirty")
    return git_value(repo, "rev-parse", "HEAD")


def prepare(root: Path, output: Path) -> str:
    bundle_bytes = download_base_bundle()
    patch_bytes = decode_patch(root)

    if output.exists():
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="aiboot-publish-") as temporary:
        temporary_path = Path(temporary)
        bundle_path = temporary_path / "base.bundle"
        patch_path = temporary_path / "cross-platform.mbox"
        bundle_path.write_bytes(bundle_bytes)
        patch_path.write_bytes(patch_bytes)

        run("git", "bundle", "verify", str(bundle_path), cwd=root)
        run("git", "clone", str(bundle_path), str(output), cwd=root)
        verify_base(output)
        run("git", "config", "user.name", "Adaptive ISO Boot Release", cwd=output)
        run(
            "git",
            "config",
            "user.email",
            "73152895+siraht@users.noreply.github.com",
            cwd=output,
        )
        run(
            "git",
            "am",
            "--3way",
            "--committer-date-is-author-date",
            str(patch_path),
            cwd=output,
        )

    return verify_final(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        final_sha = prepare(root, arguments.output.resolve())
    except (PublicationError, OSError) as exc:
        print(f"publication preparation failed: {exc}", file=sys.stderr)
        return 1
    print(f"verified_source={arguments.output.resolve()}")
    print(f"final_sha={final_sha}")
    print(f"commit_count={EXPECTED_FINAL_COMMIT_COUNT}")
    print(f"version={EXPECTED_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
