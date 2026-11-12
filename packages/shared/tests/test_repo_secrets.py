"""No credential is ever committed (Phase 9 security pass).

Scans every file git tracks for the shapes of real keys and tokens. Runs in CI on every push,
so a key pasted into a test, a doc or a config fails the build before it is merged.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

PATTERNS = {
    "Groq key": r"gsk_[A-Za-z0-9]{20,}",
    "Google API key": r"AIza[0-9A-Za-z_\-]{35}",
    "Meta access token": r"EAA[A-Za-z0-9]{50,}",
    "OpenAI-style key": r"sk-[A-Za-z0-9]{32,}",
    "Private key": r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "Service account JSON": r'"private_key_id":\s*"[0-9a-f]{20,}"',
    "AWS access key": r"AKIA[0-9A-Z]{16}",
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".wav", ".onnx", ".woff", ".woff2"}
SKIP_FILES = {"uv.lock", "pnpm-lock.yaml"}  # hashes, not secrets


GIT = shutil.which("git")


def tracked_files() -> list[Path]:
    if GIT is None:
        pytest.skip("git is not installed")
    try:
        out = subprocess.run(  # noqa: S603 - fixed git command, no user input
            [GIT, "ls-files", "-z"], cwd=REPO, capture_output=True, check=True, text=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [REPO / p for p in out.split("\0") if p]


def test_no_credentials_in_tracked_files() -> None:
    found = []
    for path in tracked_files():
        if path.suffix in SKIP_SUFFIXES or path.name in SKIP_FILES or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name, pattern in PATTERNS.items():
            if re.search(pattern, text):
                found.append(f"{path.relative_to(REPO)}: looks like a {name}")
    assert not found, "credentials in the repo:\n" + "\n".join(found)


def test_env_file_is_ignored_and_not_tracked() -> None:
    names = {p.relative_to(REPO).as_posix() for p in tracked_files()}
    assert ".env" not in names
    assert ".env.example" in names
    assert GIT is not None
    ignored = subprocess.run([GIT, "check-ignore", "-q", ".env"], cwd=REPO, check=False)  # noqa: S603
    assert ignored.returncode == 0
