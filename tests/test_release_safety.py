from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCAL_RUNTIME_PATHS = (
    "case-set.json",
    "inputs/L3A_CASE_001.json",
    "outputs/L3A_CASE_001.json",
    "traces/trace.jsonl",
    "dist/submission.zip",
    ".env",
)


def _release_candidates() -> set[str]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=True,
        capture_output=True,
    )
    return {
        value.decode("utf-8").replace("\\", "/")
        for value in result.stdout.split(b"\0")
        if value
    }


def _forbidden(path: str) -> bool:
    item = PurePosixPath(path)
    root = item.parts[0] if item.parts else ""
    runtime_root = root in {"inputs", "outputs", "traces", "dist"}
    return (
        path == "case-set.json"
        or (runtime_root and path != f"{root}/.gitkeep")
        or item.name
        in {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
        or item.suffix.lower() in {".csv", ".parquet", ".feather", ".arrow", ".sqlite3"}
        or path == ".env"
        or (path.startswith(".env.") and path != ".env.example")
    )


def test_release_candidates_exclude_local_competition_payload() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("release inventory requires a Git checkout")
    offenders = sorted(path for path in _release_candidates() if _forbidden(path))
    assert not offenders, f"release-unsafe paths: {offenders}"


def test_local_competition_payload_is_ignored() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("release inventory requires a Git checkout")
    for path in LOCAL_RUNTIME_PATHS:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "--quiet", "--no-index", path]
        )
        assert result.returncode == 0, f"{path} is not ignored"


def test_example_environment_has_no_real_key() -> None:
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
