"""Path budgets exercised with real isolated export folders."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from batch.common import ResourceError
from batch.exporter import export, verify_outputs
from batch.path_budget import PATH_LIMIT, fit_slug, path_units, truncate_units
from batch.result_paths import (EXTENSIONS, bounded_output_stem,
                                minimum_export_leaf_units, output_paths,
                                output_stem, unused_paths, version_suffix)


IDENTITY = "local:" + "f" * 64
TITLE = "Zjawisko_Gibbsa_w_aproksymacji_sygnału_prostokątnego"


def report(title=TITLE):
    return {"identity": IDENTITY, "title": title, "duration": 4,
            "processed_until": 2, "segments": []}


class PathBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def root_at_units(self, units):
        root = self.base / ("a" * (units - path_units(self.base) - 1))
        self.assertEqual(path_units(root), units)
        root.mkdir()
        return root

    def assert_budget(self, paths):
        for path in paths.values():
            self.assertLessEqual(path_units(path.resolve()), PATH_LIMIT)
            self.assertIn("local-" + "f" * 20, path.name)
            self.assertIn("_transkrypcja", path.name)

    def test_utf16_budget_keeps_complete_characters_and_small_fallbacks(self):
        self.assertEqual(path_units("A😀ą"), 4)
        self.assertEqual(truncate_units("A😀ą", 2), "A")
        self.assertEqual(truncate_units("A😀ą", 3), "A😀")
        self.assertEqual(fit_slug(" . ", 1), "n")
        self.assertEqual(fit_slug(" . ", 7), "nagrani")
        self.assertEqual(fit_slug("😀", 1), "n")
        self.assertEqual(fit_slug("😀ą", 2), "😀")

    def test_minimum_leaf_reserves_identity_partial_and_actual_version(self):
        self.assertEqual(minimum_export_leaf_units(IDENTITY), 70)
        self.assertEqual(minimum_export_leaf_units(IDENTITY, version=1), 57)
        self.assertEqual(minimum_export_leaf_units(IDENTITY, partial=False, version=1), 47)
        self.assertEqual(minimum_export_leaf_units(IDENTITY, version=10000), 71)

    def test_short_folder_keeps_existing_named_export_format(self):
        paths = export(self.base, report("Rozmowa / przykład"), "one")
        self.assertEqual(paths["json"].name,
                         "Rozmowa _ przykład__local-" + "f" * 20 + "_transkrypcja.json")
        self.assert_budget(paths)
        self.assertTrue(verify_outputs(self.base, IDENTITY, "one"))

    def test_beside_source_root135_full_partial_and_repeat_preserve_old_bytes(self):
        root = self.root_at_units(135)
        source = root / (TITLE + ".m4a")
        source.write_bytes(b"untouched local recording")
        folder = root / (TITLE[:19] + "__" + "f" * 12)
        self.assertEqual(path_units(folder), 169)
        full = export(folder, report(), "full")
        partial = export(folder, report(), "partial", complete=False)
        before = {path: path.read_bytes() for path in (*full.values(), *partial.values())}
        repeated = export(folder, report(), "partial-two", complete=False)
        self.assert_budget(full)
        self.assert_budget(partial)
        self.assert_budget(repeated)
        self.assertTrue(all("_czesciowa__wersja-2." in path.name for path in repeated.values()))
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(source.read_bytes(), b"untouched local recording")
        self.assertTrue(verify_outputs(folder, IDENTITY, "full"))
        self.assertTrue(verify_outputs(folder, IDENTITY, "partial-two", partial=True))

    def test_version9999_and_non_bmp_title_stay_inside_budget(self):
        folder = self.root_at_units(169)
        stem = output_stem("😀" * 100, IDENTITY) + "_czesciowa"
        suffix = version_suffix(9999)
        bounded = bounded_output_stem(folder, stem, suffix, IDENTITY)
        path = folder / (bounded + suffix + ".json")
        self.assertEqual(path_units(path), PATH_LIMIT)
        self.assertEqual(path.name, "n__local-" + "f" * 20 +
                         "_transkrypcja_czesciowa__wersja-9999.json")
        actual = export(folder, report("😀" * 100), "emoji", complete=False)
        self.assert_budget(actual)
        self.assertTrue(verify_outputs(folder, IDENTITY, "emoji", partial=True))

    def test_one_existing_extension_reserves_the_whole_version(self):
        stem = output_stem("Rozmowa", IDENTITY)
        existing = self.base / (stem + ".txt")
        existing.write_bytes(b"user-owned previous text")
        paths = unused_paths(self.base, stem, identity=IDENTITY)
        self.assertTrue(all("__wersja-2." in path.name for path in paths.values()))
        self.assertEqual(existing.read_bytes(), b"user-owned previous text")

    def test_existing_manifest_names_are_never_shortened_on_read(self):
        folder = self.root_at_units(169)
        names = {ext: "a" * 45 + "__local-" + "f" * 20 + "_transkrypcja." + ext
                 for ext in EXTENSIONS}
        manifest = {"outputs": dict(names)}
        paths = output_paths(folder, manifest)
        self.assertEqual({ext: path.name for ext, path in paths.items()}, names)
        self.assertEqual(manifest, {"outputs": names})

    def test_truly_impossible_partial_folder_fails_before_writing(self):
        folder = self.root_at_units(183)
        existing = folder / "keep.txt"
        existing.write_bytes(b"keep existing user output")
        with self.assertRaisesRegex(ResourceError, "zbyt długa"):
            export(folder, report(), "impossible", complete=False)
        self.assertEqual(list(folder.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"keep existing user output")


if __name__ == "__main__":
    unittest.main()
