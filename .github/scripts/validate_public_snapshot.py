#!/usr/bin/env python3
"""Fail-closed validation for the generated public source snapshot."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


ALLOWED_GITHUB_FILES = {
    ".github/scripts/validate_public_snapshot.py",
    ".github/workflows/security-scan.yml",
}

FORBIDDEN_PATHS = {
    "AGENTS.md",
    "CONCEPTS.md",
    "DESIGN.md",
    "docs",
    ".agents",
    ".claude",
    ".codex",
    ".gstack",
    "artifacts",
    "build",
    "dist",
    "exports",
    "journal",
    "journals",
    "logs",
    "reports/private",
    "apps/electron-dashboard/.vite",
}

SENSITIVE_BASENAMES = {
    ".env",
    ".secret.local",
    "cookies",
    "google-services.json",
    "login data",
    "web data",
}

SENSITIVE_SUFFIXES = {
    ".crt",
    ".csr",
    ".db",
    ".jks",
    ".key",
    ".keystore",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".ppk",
    ".sqlite",
    ".sqlite3",
}

HIGH_RISK_PATTERNS = (
    (
        "openai-api-key",
        re.compile(r"(?:sk-proj-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,})"),
    ),
    (
        "github-token",
        re.compile(
            r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"
        ),
    ),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    ),
)

LOCAL_PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Za-z]:[\\/]+Users[\\/]+|/Users/|/home/)"
    r"(?P<username>[A-Za-z0-9][^\\/\s\"'<>:]*)"
)
ALLOWED_FIXTURE_USERS = {
    "alice",
    "example",
    "example-user",
    "test-user",
    "tester",
}

ACCOUNT_PATTERN = re.compile(
    r"(?i)\b(?:account(?:_?(?:no|number))?|acct)\b[^\r\n\d]{0,24}"
    r"(?P<number>\d{8,})(?:-\d{1,2})?"
)
ALLOWED_ACCOUNT_FIXTURES = {
    "12345678",
    "123456789",
    "20260729",
    "22222221",
    "22222278",
    "87654321",
}

KIS_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)\bKIS_(?:VTS|LIVE)_(?:APP_KEY|APP_SECRET|ACCOUNT_NO)"
    r"[\"']?[ \t]*(?:=|:)[ \t]*(?P<quote>[\"']?)"
    r"(?P<value>[^\s#\"']{12,})(?P=quote)"
)
ALLOWED_CREDENTIAL_FIXTURES = {
    "PS1234567890",
    "app-key-value",
    "bound-live-key",
    "bound-live-secret",
    "bridge-test-value-a",
    "bridge-test-value-b",
    "changed-live-secret",
    "drifted-secret",
    "existing-live-account",
    "existing-paper-account",
    "existing-paper-key",
    "existing-paper-secret",
    "file-account",
    "file-live-account",
    "file-live-account21",
    "file-live-key",
    "file-live-secret",
    "live-account78",
    "live-app-key",
    "live-app-secret",
    "live-bind-key",
    "live-bind-secret",
    "live-bridge-key",
    "live-bridge-secret",
    "live-key-rotated",
    "live-secret-value",
    r"live-secret-value\n",
    "live-state-key",
    r"live-state-key\n",
    "live-state-secret",
    "must-not-leak",
    "new-live-key",
    "new-live-secret",
    "old-live-key",
    "old-live-secret",
    "paper-account",
    "paper-account-test",
    "paper-secret",
    "process-live-key",
    "process-live-secret",
    "process-secret",
    "process-secret-value",
    r"process-secret-value\n",
    "secret-token",
    "secret-value",
    r"secret-value\n",
    "service-app-key",
    "service-app-secret",
    "stale-account99",
    "stale-process-account",
    "stale-process-key",
    "stale-process-secret",
    "stale-process-value-b",
    "stale-secret",
    "test-live-account",
    "test-live-account21",
    "test-live-account40",
    "wrong-cwd-key",
    "wrong-cwd-secret",
    "wrong-internal-key",
    "wrong-internal-secret",
    "{account_no}",
}


@dataclass(frozen=True, order=True)
class Finding:
    rule: str
    path: str


class SnapshotValidationError(RuntimeError):
    def __init__(self, findings: list[Finding]):
        self.findings = sorted(set(findings))
        details = "\n".join(
            f"{finding.rule}: {finding.path}" for finding in self.findings
        )
        super().__init__(f"Public snapshot validation failed:\n{details}")


def _is_under(path: str, candidate: str) -> bool:
    return path == candidate or path.startswith(candidate + "/")


def _decode_text(data: bytes) -> str | None:
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    for encoding in ("utf-8", "utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _path_findings(relative_path: str) -> list[Finding]:
    findings: list[Finding] = []
    lowered = relative_path.lower()
    pure = PurePosixPath(relative_path)
    basename = pure.name.lower()

    if any(_is_under(relative_path, forbidden) for forbidden in FORBIDDEN_PATHS):
        findings.append(Finding("forbidden-path", relative_path))

    if relative_path.startswith(".github/") and relative_path not in ALLOWED_GITHUB_FILES:
        findings.append(Finding("unexpected-github-file", relative_path))

    if basename == ".env.example":
        return findings

    if (
        basename in SENSITIVE_BASENAMES
        or basename.startswith(".env.")
        or pure.suffix.lower() in SENSITIVE_SUFFIXES
    ):
        findings.append(Finding("sensitive-file", relative_path))

    if re.search(
        r"(?i)(?:^|/)(?:stockbot-diagnostics-|.*(?:account|balance|order|fill|trade)s?[^/]*)"
        r".*\.(?:csv|json)$",
        lowered,
    ):
        findings.append(Finding("private-runtime-export", relative_path))

    return findings


def _content_findings(relative_path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []

    for rule, pattern in HIGH_RISK_PATTERNS:
        if pattern.search(text):
            findings.append(Finding(rule, relative_path))

    for match in LOCAL_PATH_PATTERN.finditer(text):
        if match.group("username").lower() not in ALLOWED_FIXTURE_USERS:
            findings.append(Finding("local-user-path", relative_path))
            break

    for match in ACCOUNT_PATTERN.finditer(text):
        if match.group("number") not in ALLOWED_ACCOUNT_FIXTURES:
            findings.append(Finding("account-number", relative_path))
            break

    for match in KIS_ASSIGNMENT_PATTERN.finditer(text):
        is_test_fixture = relative_path.startswith("tests/") or bool(
            re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", relative_path)
        )
        if not is_test_fixture or match.group("value") not in ALLOWED_CREDENTIAL_FIXTURES:
            findings.append(Finding("kis-credential", relative_path))
            break

    return findings


def validate_snapshot(root: Path) -> list[Path]:
    root = root.resolve()
    if not root.is_dir():
        raise SnapshotValidationError([Finding("missing-snapshot", ".")])

    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )
    if not files:
        raise SnapshotValidationError([Finding("empty-snapshot", ".")])

    findings: list[Finding] = []
    for path in files:
        relative_path = path.relative_to(root).as_posix()
        findings.extend(_path_findings(relative_path))
        text = _decode_text(path.read_bytes())
        if text is not None:
            findings.extend(_content_findings(relative_path, text))

    if findings:
        raise SnapshotValidationError(findings)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args(argv)

    try:
        files = validate_snapshot(args.snapshot)
    except SnapshotValidationError as error:
        print(error, file=sys.stderr)
        return 1

    print(f"Validated {len(files)} public snapshot files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
