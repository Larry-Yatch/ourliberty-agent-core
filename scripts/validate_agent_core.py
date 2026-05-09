#!/usr/bin/env python3
"""
validate_agent_core.py — Pre-sync validation for ourliberty-agent-core repo.

Checks:
  1. HANDSHAKE-SCHEMA.json is valid JSON Schema draft-07
  2. Every agent directory has at minimum IDENTITY.md, SOUL.md, CLAUDE.md
  3. No files matching secret patterns are present
  4. .gitignore exists and contains key entries

Exit 0 if all checks pass, exit 2 if any fail (with specific error messages).
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Required files in every agent directory
REQUIRED_AGENT_FILES = ["IDENTITY.md", "SOUL.md", "CLAUDE.md"]

# File patterns that indicate secrets — must never be committed
SECRET_FILE_PATTERNS = [
    "auth-tokens.json",
    "webhook-secrets.json",
    ".env",
    ".env.local",
    ".env.production",
]

# Template files that look like .env but are intentionally committed as
# documentation. Allowed ONLY if every KEY=value line is commented (see content scan).
ENV_TEMPLATE_ALLOWLIST = [
    "config/.env.example",
]

# Content patterns that indicate leaked secrets
SECRET_CONTENT_PATTERNS = [
    r"sk-ant-[a-zA-Z0-9_-]{20,}",  # Anthropic API keys
    r"Bearer\s+[a-zA-Z0-9_-]{20,}",  # Bearer tokens (not in comments/docs)
    r"VAPI_API_KEY=[a-zA-Z0-9_-]{10,}",  # VAPI key with value
    r"SUPABASE_ACCESS_TOKEN=[a-zA-Z0-9_-]{10,}",  # Supabase token with value
    r"STRIPE_SECRET_KEY=sk_[a-zA-Z0-9_-]{10,}",  # Stripe secret key with value
    r'"token":\s*"[0-9]+:[a-zA-Z0-9_-]{30,}"',  # Telegram bot tokens
]

# Required entries in .gitignore
REQUIRED_GITIGNORE_ENTRIES = [
    "*.env",
    "auth-tokens.json",
    "webhook-secrets.json",
    "inboxes/",
    "outboxes/",
    "blackboard/",
    "logs/",
    "memory/",
    "audit-log/",
]

# File extensions to scan for secret content
SCANNABLE_EXTENSIONS = {
    ".json",
    ".md",
    ".py",
    ".sh",
    ".yml",
    ".yaml",
    ".txt",
    ".mjs",
    ".js",
}


class ValidationResult:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str):
        self.errors.append(msg)
        print(f"  ERROR: {msg}", file=sys.stderr)

    def warn(self, msg: str):
        self.warnings.append(msg)
        print(f"  WARN:  {msg}", file=sys.stderr)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


def validate_handshake_schema(repo_dir: str, result: ValidationResult):
    """Check that HANDSHAKE-SCHEMA.json is valid JSON Schema draft-07."""
    print("[validate] Checking HANDSHAKE-SCHEMA.json...", file=sys.stderr)

    schema_path = os.path.join(repo_dir, "shared", "HANDSHAKE-SCHEMA.json")
    if not os.path.isfile(schema_path):
        result.error("shared/HANDSHAKE-SCHEMA.json not found")
        return

    try:
        with open(schema_path) as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        result.error(f"HANDSHAKE-SCHEMA.json is not valid JSON: {e}")
        return

    # Check it declares JSON Schema draft-07
    schema_version = schema.get("$schema", "")
    if "draft-07" not in schema_version and "draft/2020-12" not in schema_version:
        result.warn(
            f"HANDSHAKE-SCHEMA.json $schema is '{schema_version}' "
            f"(expected draft-07 or later)"
        )

    # Check it has basic required properties
    if "type" not in schema:
        result.error("HANDSHAKE-SCHEMA.json missing 'type' property")
    if "properties" not in schema:
        result.error("HANDSHAKE-SCHEMA.json missing 'properties' property")

    print("  HANDSHAKE-SCHEMA.json: OK", file=sys.stderr)


def validate_agent_directories(repo_dir: str, result: ValidationResult):
    """Check every agent directory has required files."""
    print("[validate] Checking agent directories...", file=sys.stderr)

    agents_dir = os.path.join(repo_dir, "agents")
    if not os.path.isdir(agents_dir):
        result.error("agents/ directory not found")
        return

    agent_dirs = [
        d
        for d in os.listdir(agents_dir)
        if os.path.isdir(os.path.join(agents_dir, d)) and not d.startswith(".")
    ]

    if not agent_dirs:
        result.error("No agent directories found in agents/")
        return

    for agent_name in sorted(agent_dirs):
        agent_path = os.path.join(agents_dir, agent_name)
        for req_file in REQUIRED_AGENT_FILES:
            filepath = os.path.join(agent_path, req_file)
            if not os.path.isfile(filepath):
                result.error(f"agents/{agent_name}/{req_file} is missing")
            else:
                # Check file is not empty
                if os.path.getsize(filepath) == 0:
                    result.warn(f"agents/{agent_name}/{req_file} is empty")

        print(f"  Agent {agent_name}: checked", file=sys.stderr)


def validate_no_secrets(repo_dir: str, result: ValidationResult):
    """Check no files matching secret patterns are present, and no secret content."""
    print("[validate] Scanning for secrets...", file=sys.stderr)

    # Check for secret files
    for root, dirs, files in os.walk(repo_dir):
        # Skip .git directory
        if ".git" in root.split(os.sep):
            continue

        for fname in files:
            filepath_abs = os.path.join(root, fname)
            relpath_check = os.path.relpath(filepath_abs, repo_dir)
            # Check filename patterns
            for pattern in SECRET_FILE_PATTERNS:
                if fname == pattern:
                    # Allow .env.example
                    if fname.endswith(".example"):
                        continue
                    result.error(f"Secret file detected: {relpath_check}")
            # Env-template allowlist: these files are permitted IF every KEY=value
            # line is commented out (documentation only). Real secrets rejected.
            if relpath_check in ENV_TEMPLATE_ALLOWLIST:
                try:
                    with open(filepath_abs) as tf:
                        for lineno, line in enumerate(tf, 1):
                            stripped = line.strip()
                            if not stripped or stripped.startswith("#"):
                                continue
                            # Non-comment, non-blank line — must not look like KEY=nonempty
                            if "=" in stripped:
                                k, _, v = stripped.partition("=")
                                if v.strip():
                                    result.error(
                                        f"Real secret in {relpath_check}:{lineno} — "
                                        f"non-commented assignment: {k}=..."
                                    )
                except Exception as e:
                    result.warn(f"Could not scan {relpath_check}: {e}")

            # Check file content for leaked secrets
            filepath = os.path.join(root, fname)
            _, ext = os.path.splitext(fname)
            if ext.lower() in SCANNABLE_EXTENSIONS:
                try:
                    with open(filepath, encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    for pattern in SECRET_CONTENT_PATTERNS:
                        matches = re.findall(pattern, content)
                        if matches:
                            relpath = os.path.relpath(filepath, repo_dir)
                            # Allow matches in .env.example (they're empty templates)
                            if relpath.endswith(".env.example"):
                                continue
                            result.error(
                                f"Potential secret in {relpath}: "
                                f"matched pattern '{pattern}' ({len(matches)} occurrence(s))"
                            )
                except OSError:
                    pass

    print("  Secret scan: complete", file=sys.stderr)


def validate_gitignore(repo_dir: str, result: ValidationResult):
    """Check .gitignore exists and contains key entries."""
    print("[validate] Checking .gitignore...", file=sys.stderr)

    gitignore_path = os.path.join(repo_dir, ".gitignore")
    if not os.path.isfile(gitignore_path):
        result.error(".gitignore not found")
        return

    with open(gitignore_path) as f:
        gitignore_content = f.read()

    for entry in REQUIRED_GITIGNORE_ENTRIES:
        if entry not in gitignore_content:
            result.warn(f".gitignore missing recommended entry: {entry}")

    print("  .gitignore: OK", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Validate ourliberty-agent-core repo before sync"
    )
    parser.add_argument(
        "--repo-dir",
        default=".",
        help="Path to the ourliberty-agent-core repo (default: current directory)",
    )
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)
    if not os.path.isdir(repo_dir):
        print(f"ERROR: {repo_dir} is not a directory", file=sys.stderr)
        sys.exit(2)

    print(f"[validate] Validating repo at {repo_dir}", file=sys.stderr)
    result = ValidationResult()

    validate_handshake_schema(repo_dir, result)
    validate_agent_directories(repo_dir, result)
    validate_no_secrets(repo_dir, result)
    validate_gitignore(repo_dir, result)

    print(file=sys.stderr)
    if result.ok:
        warn_count = len(result.warnings)
        warn_msg = f" ({warn_count} warning(s))" if warn_count else ""
        print(f"[validate] PASSED{warn_msg}", file=sys.stderr)
        sys.exit(0)
    else:
        print(
            f"[validate] FAILED — {len(result.errors)} error(s), "
            f"{len(result.warnings)} warning(s)",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
