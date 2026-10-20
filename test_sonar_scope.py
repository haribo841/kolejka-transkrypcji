"""A dirty working fix must not allow an invalid committed Sonar scope to pass."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.check_sonar_scope import main, push_revisions, revision_scope, scope_errors


def configuration(names):
    paths = ",".join(names)
    return f"sonar.sources=.\nsonar.tests={paths}\nsonar.exclusions={paths}\n"


class SonarScopeTests(unittest.TestCase):
    def test_missing_test_fails_both_scope_lists(self):
        errors = scope_errors(configuration(["test_one.py"]), ["test_one.py", "test_new.py", "batch/app.py"])
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("test_new.py" in error for error in errors))

    def test_production_exclusion_or_duplicate_is_rejected(self):
        for names, message in ((["test_one.py", "batch/app.py"], "unexpected paths"),
                               (["test_one.py", "test_one.py"], "duplicate paths")):
            with self.subTest(names=names):
                self.assertTrue(any(message in error for error in
                                    scope_errors(configuration(names), ["test_one.py", "batch/app.py"])))

    def test_push_deletions_and_duplicate_ref_targets_do_not_add_scans(self):
        revision = "a" * 40
        lines = [f"refs/heads/master {revision} refs/heads/master {'b' * 40}\n",
                 f"refs/heads/alias {revision} refs/heads/alias {'0' * 40}\n",
                 f"(delete) {'0' * 40} refs/heads/old {'c' * 40}\n"]
        self.assertEqual(push_revisions(lines), [revision])

    def test_malformed_updates_are_rejected_before_a_git_command_can_run(self):
        for line in ("", "ref --help ref 0", "ref ../other ref 0", "ref " + "a" * 40):
            with self.subTest(line=line), self.assertRaises(ValueError):
                push_revisions([line])

    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = Path(temporary.name).resolve()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True, capture_output=True)
        (repo / "test_one.py").write_text("# Offline fixture\n", encoding="utf-8")
        (repo / "test_new.py").write_text("# Offline fixture\n", encoding="utf-8")
        config = repo / ".sonarcloud.properties"
        config.write_text(configuration(["test_one.py"]), encoding="utf-8")
        subprocess.run(["git", "add", "--", config.name, "test_one.py", "test_new.py"],
                       cwd=repo, check=True, capture_output=True)
        tree = subprocess.check_output(["git", "write-tree"], cwd=repo, text=True).strip()
        config.write_text(configuration(["test_one.py", "test_new.py"]), encoding="utf-8")
        return repo, tree

    def test_committed_tree_is_checked_even_with_a_valid_unstaged_fix(self):
        repo, bad_tree = self.fixture()
        errors = revision_scope(repo, bad_tree)
        self.assertTrue(any("test_new.py" in error for error in errors))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([], repo=repo), 0)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(["--revision", bad_tree], repo=repo), 1)
        self.assertIn("missing test modules", stderr.getvalue())

    def test_pre_push_blocks_invalid_revision_and_accepts_corrected_tree(self):
        repo, bad_tree = self.fixture()
        subprocess.run(["git", "add", "--", ".sonarcloud.properties"], cwd=repo, check=True, capture_output=True)
        good_tree = subprocess.check_output(["git", "write-tree"], cwd=repo, text=True).strip()
        for revision, expected in ((bad_tree, 1), (good_tree, 0)):
            update = io.StringIO(f"refs/heads/master {revision} refs/heads/master {'0' * 40}\n")
            with self.subTest(revision=revision), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--pre-push"], repo=repo, input_stream=update), expected)

    def test_missing_configuration_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()):
            self.assertEqual(main([], repo=Path(temporary)), 1)


if __name__ == "__main__":
    unittest.main()
