"""Automatic-discovery filters preserve ordinary folders and real TS media."""
from __future__ import annotations

import errno
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from batch.media_discovery import TS_SNIFF_BYTES, technical_directory, technical_source


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, relative, data=b"recording"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_arbitrarily_named_python_environment_excludes_descendants(self):
        marker = self.source("custom-python-3.15/pyvenv.cfg", b"home = C:/Python315\n")
        source = self.source("custom-python-3.15/samples/example.wav")
        self.assertIn("pyvenv.cfg", technical_directory(marker.parent))
        self.assertIn(str(marker.parent), technical_source(source))

    def test_conda_environment_requires_history_file(self):
        source = self.source("runtime/conda-meta/example.wav")
        self.assertIsNone(technical_directory(self.root / "runtime"))
        self.assertIsNone(technical_source(source))
        self.source("runtime/conda-meta/history", b"# conda revisions\n")
        self.assertIn("Conda", technical_directory(self.root / "runtime"))
        self.assertIn("Conda", technical_source(source))

    def test_exact_package_directories_without_environment_marker(self):
        for name in ("site-packages", "dist-packages", "SITE-PACKAGES"):
            with self.subTest(name=name):
                source = self.source(f"runtime/{name}/scipy/io/tests/test.wav")
                self.assertIn("Pakiety Pythona", technical_source(source))
        self.assertIsNone(technical_directory(self.root / "missing/site-packages"))

    def test_ordinary_folder_names_and_near_package_names_remain_importable(self):
        for name in ("env", "venv", ".venv", "test", "build", "vendor", "site-packages-recordings", "dist-packages-backup"):
            with self.subTest(name=name):
                source = self.source(f"{name}/lesson.wav")
                self.assertIsNone(technical_directory(source.parent))
                self.assertIsNone(technical_source(source))

    def test_directory_named_like_environment_marker_is_not_a_marker(self):
        source = self.source("runtime/samples/lesson.wav")
        (self.root / "runtime/pyvenv.cfg").mkdir()
        (self.root / "runtime/conda-meta/history").mkdir(parents=True)
        self.assertIsNone(technical_source(source))

    def test_cmake_timestamp_file_is_excluded_by_content(self):
        source = self.source("build/CMakeFiles/Utilities.dir/compiler_depend.ts",
                             b"# CMAKE generated file: DO NOT EDIT!\n"
                             b"# Timestamp file for compiler generated dependencies management for Utilities.\n")
        self.assertIn("CMake", technical_source(source))
        plain = self.source("compiler_depend.ts", b"unrecognized input")
        self.assertIsNone(technical_source(plain))

    def test_typescript_sources_with_unicode_bom_and_declarations(self):
        samples = (
            "import { Component } from '@angular/core';\n",
            "import type Name from './name';\n",
            "import './side-effect';\n",
            "export interface Student { name: string; }\n",
            "declare namespace App { }\n",
            "type Student = { name: string };\n",
            "const title: string = 'Łódź';\n",
            "// komentarz\nexport default class App {}\n",
        )
        for index, sample in enumerate(samples):
            with self.subTest(sample=sample):
                source = self.source(f"src/{index}.TS", sample.encode("utf-8-sig"))
                self.assertIn("TypeScript", technical_source(source))

    def test_qt_translation_xml_in_utf8_and_utf16(self):
        text = '<?xml version="1.0"?>\n<!DOCTYPE TS>\n<TS version="2.1" language="pl_PL"><context /></TS>'
        for encoding in ("utf-8", "utf-16"):
            with self.subTest(encoding=encoding):
                source = self.source(f"qt/{encoding}.ts", text.encode(encoding))
                self.assertIn("Qt", technical_source(source))

    def test_unknown_binary_and_unrecognized_text_are_preserved(self):
        for index, data in enumerate((b"\x00\x01\x02not media", b"\xff\xfe\x00broken", b"generic text", b"")):
            with self.subTest(data=data):
                self.assertIsNone(technical_source(self.source(f"unknown/{index}.ts", data)))

    def test_transport_packet_sync_takes_precedence_over_text_syntax(self):
        for size, offset in ((188, 0), (192, 4), (204, 0)):
            with self.subTest(size=size):
                packet = bytearray(b" " * size)
                packet[offset] = 0x47
                packet[offset + 1:offset + 1 + len(b"\nconst sample = 1;")] = b"\nconst sample = 1;"
                source = self.source(f"packets/{size}.ts", bytes(packet) * 5)
                self.assertIsNone(technical_source(source))

    def test_sniff_is_bounded_and_partial_unicode_character_is_supported(self):
        header = b"const title = 'sample';\n"
        text = header + b" " * (TS_SNIFF_BYTES - len(header) - 1) + "Ł".encode("utf-8")
        source = self.source("bounded.ts", text)
        sizes = []
        original = Path.open

        class TrackedRead:
            def __enter__(self):
                self.stream = original(source, "rb")
                return self

            def read(self, size):
                sizes.append(size)
                return self.stream.read(size)

            def __exit__(self, *args):
                self.stream.close()

        with patch.object(Path, "open", return_value=TrackedRead()):
            self.assertIn("TypeScript", technical_source(source))
        self.assertEqual(sizes, [TS_SNIFF_BYTES])

    def test_inaccessible_environment_marker_is_reported_by_caller(self):
        source = self.source("ordinary/recording.wav")
        marker = source.parent / "pyvenv.cfg"
        original = Path.stat

        def inaccessible(path, *args, **kwargs):
            if path == marker:
                raise PermissionError(errno.EACCES, "denied", str(path))
            return original(path, *args, **kwargs)

        with patch.object(Path, "stat", inaccessible):
            with self.assertRaises(PermissionError):
                technical_source(source)

    def test_inaccessible_ts_file_is_reported_by_caller(self):
        source = self.source("ordinary/recording.ts")
        with patch.object(Path, "open", side_effect=PermissionError(errno.EACCES, "denied", str(source))):
            with self.assertRaises(PermissionError):
                technical_source(source)

    def test_real_ffmpeg_transport_stream_is_preserved(self):
        executable = shutil.which("ffmpeg")
        if not executable and Path(r"C:\ffmpeg\bin\ffmpeg.exe").is_file():
            executable = r"C:\ffmpeg\bin\ffmpeg.exe"
        if not executable:
            self.skipTest("FFmpeg required for a real MPEG-TS fixture")
        source = self.root / "real transport stream.ts"
        result = subprocess.run(
            [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
             "-c:a", "mp2", "-f", "mpegts", str(source)],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertGreater(source.stat().st_size, 5 * 188)
        self.assertIsNone(technical_source(source))


if __name__ == "__main__":
    unittest.main()
