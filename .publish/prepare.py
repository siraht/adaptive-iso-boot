#!/usr/bin/env python3
"""Reconstruct and verify the exact Adaptive ISO Boot v0.2.0 source history.

This script is intentionally dependency-free. It rebuilds the checksum-verified
32-commit base repository from the staged Git bundle, applies the 16 granular
cross-platform commits as an mbox series, and fails closed if any identity,
commit-count, subject, or version invariant differs from the publication
manifest. Committer dates are pinned to author dates so every runner produces
the same commit objects and final SHA.
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
from pathlib import Path

BASE_BUNDLE_SHA256 = "96cc45c22b69032634cc30bf4c2932e07bd9ac140b4e6d65bbddce3c3c2fa6f0"
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


def _decode_exact(encoded: str) -> bytes:
    return base64.b64decode(encoded, validate=True)


def _repair_one_extra_character(chunks: list[str], expected_sha: str) -> tuple[bytes, int] | None:
    """Recover a single inserted Base64 character in the final staged chunk.

    The first six bundle chunks end on Base64 quantum boundaries. The historical
    final chunk is one character longer than a valid encoding, so we can decode
    the stable prefix once and try each possible character removal from only the
    final chunk. A candidate is accepted solely when the decoded bytes match the
    pinned SHA-256.
    """

    if not chunks or len(chunks[-1]) % 4 != 1:
        return None
    prefix_text = "".join(chunks[:-1])
    if len(prefix_text) % 4:
        return None
    try:
        prefix = _decode_exact(prefix_text)
    except binascii.Error:
        return None

    prefix_digest = hashlib.sha256(prefix)
    tail = chunks[-1]
    for index in range(len(tail)):
        candidate_text = tail[:index] + tail[index + 1 :]
        try:
            candidate = _decode_exact(candidate_text)
        except binascii.Error:
            continue
        digest = prefix_digest.copy()
        digest.update(candidate)
        if digest.hexdigest() == expected_sha:
            return prefix + candidate, index
    return None


def decode_chunks(paths: list[Path], *, label: str, expected_sha: str) -> bytes:
    if not paths:
        raise PublicationError(f"no staged {label} chunks were found")
    chunks = [path.read_text(encoding="ascii").strip() for path in paths]
    encoded = "".join(chunks)
    decode_error: Exception | None = None
    try:
        decoded = _decode_exact(encoded)
    except binascii.Error as exc:
        decoded = b""
        decode_error = exc
    else:
        if sha256(decoded) == expected_sha:
            return decoded

    repaired = _repair_one_extra_character(chunks, expected_sha)
    if repaired is not None:
        decoded, index = repaired
        print(
            f"recovered staged {label} by removing one checksum-proven character "
            f"at final-chunk offset {index}",
            file=sys.stderr,
        )
        return decoded

    detail = f": {decode_error}" if decode_error is not None else ""
    raise PublicationError(
        f"staged {label} could not be decoded to pinned SHA-256 {expected_sha}{detail}"
    )


def git_value(repo: Path, *args: str) -> str:
    return run("git", *args, cwd=repo, capture=True)


def require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise PublicationError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def verify_base(repo: Path) -> None:
    require_equal(git_value(repo, "rev-parse", "HEAD"), BASE_HEAD, "base HEAD")
    require_equal(int(git_value(repo, "rev-list", "--count", "HEAD")), BASE_COMMIT_COUNT, "base commit count")
    require_equal(git_value(repo, "rev-parse", "refs/tags/v0.1.0"), V010_TAG_OBJECT, "v0.1.0 tag object")


def verify_final(repo: Path) -> str:
    commit_count = int(git_value(repo, "rev-list", "--count", "HEAD"))
    require_equal(commit_count, EXPECTED_FINAL_COMMIT_COUNT, "final commit count")

    subject_text = git_value(repo, "log", "--format=%s", "--reverse", f"{BASE_HEAD}..HEAD")
    subjects = tuple(subject_text.splitlines()) if subject_text else ()
    require_equal(subjects, EXPECTED_SUBJECTS, "applied commit subjects")

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
    bundle_chunks = sorted((root / ".bootstrap/bundle").glob("chunk-*"))
    patch_chunks = sorted((root / ".publish/patch").glob("chunk-*"))

    bundle_bytes = decode_chunks(
        bundle_chunks,
        label="base bundle",
        expected_sha=BASE_BUNDLE_SHA256,
    )
    patch_gzip = decode_chunks(
        patch_chunks,
        label="patch",
        expected_sha=PATCH_GZIP_SHA256,
    )
    try:
        patch_bytes = gzip.decompress(patch_gzip)
    except OSError as exc:
        raise PublicationError(f"patch gzip could not be decompressed: {exc}") from exc

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
        run("git", "config", "user.email", "73152895+siraht@users.noreply.github.com", cwd=output)
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
    parser.add_argument("--output", type=Path, required=True, help="Destination checkout directory")
    arguments = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    output = arguments.output.resolve()
    try:
        final_sha = prepare(root, output)
    except (PublicationError, OSError) as exc:
        print(f"publication preparation failed: {exc}", file=sys.stderr)
        return 1

    print(f"verified_source={output}")
    print(f"final_sha={final_sha}")
    print(f"commit_count={EXPECTED_FINAL_COMMIT_COUNT}")
    print(f"version={EXPECTED_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
