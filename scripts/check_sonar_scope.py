"""Validate test classification locally or in the exact Git trees being pushed."""
import argparse
from pathlib import Path
import re
import subprocess
import sys

CONFIG = ".sonarcloud.properties"
SCOPE_KEYS = ("sonar.tests", "sonar.exclusions")
OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def test_module(name):
    return "/" not in name and name.startswith("test_") and name.endswith(".py")


def scope_errors(text, files):
    properties = {key: value for key, value in (
        line.split("=", 1) for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#"))}
    expected = {name for name in files if test_module(name)}
    errors = []
    for key in SCOPE_KEYS:
        paths = [value.strip() for value in properties.get(key, "").split(",") if value.strip()]
        missing, extra = expected - set(paths), set(paths) - expected
        if missing:
            errors.append(key + ": missing test modules: " + ", ".join(sorted(missing)))
        if extra:
            errors.append(key + ": unexpected paths: " + ", ".join(sorted(extra)))
        if len(paths) != len(set(paths)):
            errors.append(key + ": duplicate paths")
    if properties.get("sonar.sources") != ".":
        errors.append("sonar.sources must retain the complete production scope (.)")
    return errors


def git(repo, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=repo, encoding="utf-8")


def revision_scope(repo, revision):
    if revision != "HEAD" and not OBJECT_ID.fullmatch(revision):
        raise ValueError("Expected HEAD or a full Git object ID.")
    files = git(repo, "ls-tree", "-r", "--name-only", "-z", revision).split("\0")
    text = git(repo, "show", revision + ":" + CONFIG)
    return scope_errors(text, files)


def push_revisions(lines):
    revisions = []
    for line in lines:
        fields = line.split()
        if len(fields) != 4 or not OBJECT_ID.fullmatch(fields[1]):
            raise ValueError("Malformed Git pre-push input; no push was validated.")
        revision = fields[1]
        if set(revision) != {"0"} and revision not in revisions:
            revisions.append(revision)
    return revisions


def main(argv=None, *, repo=None, input_stream=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--revision", help="Validate a committed tree, ignoring working changes.")
    mode.add_argument("--pre-push", action="store_true", help="Read ref updates from Git on stdin.")
    args = parser.parse_args(argv)
    repo = Path(repo) if repo is not None else Path(__file__).resolve().parent.parent
    try:
        if args.pre_push:
            revisions = push_revisions(input_stream if input_stream is not None else sys.stdin)
        else:
            revisions = [args.revision] if args.revision else []
        errors = [revision + ": " + error for revision in revisions for error in revision_scope(repo, revision)]
        if not args.pre_push and not args.revision:
            errors = scope_errors((repo / CONFIG).read_text(encoding="utf-8"),
                                  [path.name for path in repo.glob("test_*.py")])
        if errors:
            raise ValueError("\n".join(errors))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("Sonar scope check FAILED:\n" + str(error), file=sys.stderr)
        return 1
    print("Sonar scope check passed. This checks test classification, not the full Sonar Quality Gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
