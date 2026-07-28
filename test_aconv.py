"""Tests for aconv.

Fixtures are generated on the fly with ffmpeg's lavfi source, so the repository
needs no binary test files. Run with:

    python3 -m unittest test_aconv -v
"""

import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import aconv

try:
    import pty
except ImportError:  # Windows
    pty = None

ACONV = str(Path(__file__).parent / "aconv.py")
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_tone(path, frequency=440, duration=1, extra=()):
    """Write a short sine tone to `path`."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
           "-i", f"sine=frequency={frequency}:duration={duration}"]
    cmd.extend(extra)
    cmd.append(str(path))
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)


def make_tone_with_art(path):
    """Write a short tone carrying an embedded cover image."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
         "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg",
         "-disposition:v", "attached_pic", "-c:a", "aac", str(path)],
        check=True, stdin=subprocess.DEVNULL)


def probe(path, entries):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return result.returncode, result.stdout.strip()


def run_aconv(*cli_args, cwd):
    """Run the CLI without a TTY, the way a script or CI job would."""
    return subprocess.run([sys.executable, ACONV, *cli_args], cwd=str(cwd),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)


def run_aconv_interactive(*cli_args, cwd, feed, timeout=60):
    """Run the CLI on a pty so the interactive prompts are actually exercised.

    A pty never reports EOF on its own, so `feed` must end with EOT ("\\x04")
    to exercise the closed-stdin path. The deadline turns a prompt that is
    waiting for input nobody will send into a failure rather than a hang.
    """
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, ACONV, *cli_args], cwd=str(cwd),
                               stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = b""
    deadline = time.monotonic() + timeout
    try:
        os.write(master, feed.encode())
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 1.0)[0]:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break  # the child closed its end of the pty
            if not chunk:
                break
            output += chunk
        else:
            process.kill()
            raise AssertionError(
                f"aconv did not finish within {timeout}s. Output so far:\n"
                + output.decode("utf-8", errors="replace"))
    finally:
        os.close(master)
    return process.wait(timeout=30), output.decode("utf-8", errors="replace")


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class ReserveDestTest(unittest.TestCase):
    def test_distinct_paths_are_left_alone(self):
        used = set()
        self.assertEqual(aconv.reserve_dest(Path("/out/a.mp3"), used), Path("/out/a.mp3"))
        self.assertEqual(aconv.reserve_dest(Path("/out/b.mp3"), used), Path("/out/b.mp3"))

    def test_collision_uses_the_hint(self):
        used = set()
        aconv.reserve_dest(Path("/out/song.mp3"), used, "wav")
        self.assertEqual(
            aconv.reserve_dest(Path("/out/song.mp3"), used, "flac"),
            Path("/out/song_flac.mp3"))

    def test_collision_is_rechecked_after_renaming(self):
        """song.flac and song_flac.flac must not both land on song_flac.mp3."""
        used = set()
        aconv.reserve_dest(Path("/out/song.mp3"), used, "wav")
        first = aconv.reserve_dest(Path("/out/song.mp3"), used, "flac")
        second = aconv.reserve_dest(Path("/out/song_flac.mp3"), used, "flac")
        self.assertEqual(first, Path("/out/song_flac.mp3"))
        self.assertNotEqual(second, first)

    def test_comparison_is_case_insensitive(self):
        """macOS and Windows filesystems fold case, so the planner must too."""
        used = set()
        aconv.reserve_dest(Path("/out/SONG.mp3"), used, "wav")
        self.assertEqual(
            aconv.reserve_dest(Path("/out/song.mp3"), used, "flac"),
            Path("/out/song_flac.mp3"))


class PlanDestinationsTest(TempDirTestCase):
    def test_kept_files_reserve_their_destination_first(self):
        """A conversion output must never overwrite a copied or moved original."""
        source = self.tmp / "music"
        source.mkdir()
        keep = source / "song.mp3"
        convert = source / "song.wav"
        keep.touch()
        convert.touch()

        keep_plan, convert_plan = aconv.plan_destinations(
            source, self.tmp / "out", [keep], [convert], "mp3")

        self.assertEqual(keep_plan, [(keep, self.tmp / "out" / "song.mp3")])
        self.assertEqual(convert_plan, [(convert, self.tmp / "out" / "song_wav.mp3")])

    def test_skipped_files_do_not_reserve_anything(self):
        source = self.tmp / "music"
        source.mkdir()
        convert = source / "song.wav"
        convert.touch()

        keep_plan, convert_plan = aconv.plan_destinations(
            source, self.tmp / "out", [], [convert], "mp3")

        self.assertEqual(keep_plan, [])
        self.assertEqual(convert_plan, [(convert, self.tmp / "out" / "song.mp3")])

    def test_directory_structure_is_preserved(self):
        source = self.tmp / "music"
        (source / "album").mkdir(parents=True)
        track = source / "album" / "track.wav"
        track.touch()

        _, convert_plan = aconv.plan_destinations(source, self.tmp / "out", [], [track], "mp3")

        self.assertEqual(convert_plan, [(track, self.tmp / "out" / "album" / "track.mp3")])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required")
class ConversionTest(TempDirTestCase):
    def test_converts_a_directory(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "a.wav")

        result = run_aconv("music", "mp3", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "music_mp3" / "a.mp3").is_file())

    def test_case_insensitive_collision_keeps_both_outputs(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "SONG.wav", frequency=440)
        make_tone(source / "song.flac", frequency=880)

        result = run_aconv("music", "mp3", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = sorted(p.name for p in (self.tmp / "music_mp3").iterdir())
        self.assertEqual(len(outputs), 2, f"one output was overwritten: {outputs}")

    def test_failed_conversion_leaves_no_output_and_exits_non_zero(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "good.wav")
        (source / "broken.flac").write_text("this is not audio at all\n")

        result = run_aconv("music", "mp3", cwd=self.tmp)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue((self.tmp / "music_mp3" / "good.mp3").is_file())
        self.assertFalse((self.tmp / "music_mp3" / "broken.mp3").exists(),
                         "a broken conversion left an output file behind")

    def test_cover_art_source_converts_to_a_container_without_art(self):
        """ffmpeg picks theora for .ogg video streams, which fails; art must be dropped."""
        source = self.tmp / "music"
        source.mkdir()
        make_tone_with_art(source / "cover.m4a")

        result = run_aconv("music", "ogg", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.tmp / "music_ogg" / "cover.ogg").is_file())

    def test_cover_art_is_kept_for_mp3(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone_with_art(source / "cover.m4a")

        result = run_aconv("music", "mp3", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _, codec = probe(self.tmp / "music_mp3" / "cover.mp3",
                         "stream=codec_name:stream=codec_type")
        self.assertIn("mjpeg", codec)

    def test_metadata_is_preserved(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "tagged.flac",
                  extra=["-metadata", "title=My Song", "-metadata", "artist=Someone"])

        result = run_aconv("music", "mp3", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        _, tags = probe(self.tmp / "music_mp3" / "tagged.mp3", "format_tags")
        self.assertIn("My Song", tags)
        self.assertIn("Someone", tags)

    def test_already_in_target_format_is_skipped_without_a_tty(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "already.mp3")

        result = run_aconv("music", "mp3", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipping files already in the target format", result.stdout)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required")
class ScriptableFlagsTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.source = self.tmp / "music"
        self.source.mkdir()
        make_tone(self.source / "song.wav", frequency=440)
        make_tone(self.source / "song.mp3", frequency=660)
        self.original = (self.source / "song.mp3").read_bytes()

    def test_on_existing_copy_without_a_tty(self):
        result = run_aconv("music", "mp3", "--on-existing", "copy", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stdout)
        dest = self.tmp / "music_mp3"
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["song.mp3", "song_wav.mp3"])
        self.assertEqual((dest / "song.mp3").read_bytes(), self.original)

    def test_on_existing_move_needs_no_confirmation(self):
        """A script cannot answer a confirmation prompt, so the flag is the consent."""
        result = run_aconv("music", "mp3", "--on-existing", "move", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.source / "song.mp3").exists())
        self.assertEqual((self.tmp / "music_mp3" / "song.mp3").read_bytes(), self.original)

    def test_dry_run_writes_nothing(self):
        result = run_aconv("music", "mp3", "--on-existing", "copy", "--dry-run", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.tmp / "music_mp3").exists(), "dry run created the destination")
        self.assertIn("copying", result.stdout)
        self.assertIn("converting", result.stdout)
        self.assertIn("song_wav.mp3", result.stdout)

    def test_skip_existing_leaves_finished_outputs_alone(self):
        first = run_aconv("music", "mp3", cwd=self.tmp)
        self.assertEqual(first.returncode, 0, first.stderr)
        output = self.tmp / "music_mp3" / "song.mp3"
        output.write_bytes(b"sentinel")

        second = run_aconv("music", "mp3", "--skip-existing", cwd=self.tmp)

        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("--skip-existing", second.stdout)
        self.assertEqual(output.read_bytes(), b"sentinel", "an existing output was re-encoded")

    def test_skip_existing_also_covers_copies(self):
        first = run_aconv("music", "mp3", "--on-existing", "copy", cwd=self.tmp)
        self.assertEqual(first.returncode, 0, first.stderr)
        copied = self.tmp / "music_mp3" / "song.mp3"
        copied.write_bytes(b"sentinel")

        second = run_aconv("music", "mp3", "--on-existing", "copy", "--skip-existing", cwd=self.tmp)

        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertEqual(copied.read_bytes(), b"sentinel", "an existing copy was overwritten")

    def test_skip_existing_does_not_move_onto_an_existing_destination(self):
        first = run_aconv("music", "mp3", "--on-existing", "copy", cwd=self.tmp)
        self.assertEqual(first.returncode, 0, first.stderr)

        second = run_aconv("music", "mp3", "--on-existing", "move", "--skip-existing", cwd=self.tmp)

        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertTrue((self.source / "song.mp3").exists(),
                        "the original was moved onto a destination that already existed")

    def test_no_input_requires_the_positional_arguments(self):
        result = run_aconv("--no-input", cwd=self.tmp)

        self.assertEqual(result.returncode, 1)
        self.assertIn("source is required", result.stdout)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required")
class SourceSelectionTest(TempDirTestCase):
    def test_a_named_file_converts_whatever_its_extension(self):
        """ffmpeg reads more containers than the directory scan lists."""
        make_tone(self.tmp / "book.m4b", extra=["-c:a", "aac"])

        result = run_aconv("book.m4b", "mp3", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.tmp / "book_mp3" / "book.mp3").is_file())

    def test_destination_inside_the_source_is_not_rescanned(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "a.wav")

        first = run_aconv("music", "flac", "--dest", "music/out", cwd=self.tmp)
        second = run_aconv("music", "flac", "--dest", "music/out", cwd=self.tmp)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        produced = sorted(str(p.relative_to(self.tmp)) for p in source.rglob("*") if p.is_file())
        self.assertEqual(produced, ["music/a.wav", "music/out/a.flac"],
                         "the destination was picked up as a source")

    def test_converting_in_place_still_finds_the_sources(self):
        """--dest pointing at the source must not exclude the whole tree."""
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "a.wav")

        result = run_aconv("music", "mp3", "--dest", "music", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((source / "a.mp3").is_file())
        self.assertTrue((source / "a.wav").is_file())

    def test_durations_measure_the_progress_bar_weights(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "short.wav", duration=1)
        make_tone(source / "long.wav", duration=3)

        durations = aconv.measure_durations(
            [source / "short.wav", source / "long.wav"], workers=2)

        self.assertIsNotNone(durations, "ffprobe is present, so durations should be available")
        self.assertAlmostEqual(durations[0], 1.0, places=1)
        self.assertAlmostEqual(durations[1], 3.0, places=1)

    def test_durations_are_all_or_nothing(self):
        (self.tmp / "broken.wav").write_text("not audio\n")
        self.assertIsNone(aconv.measure_durations([self.tmp / "broken.wav"], workers=1))


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required")
@unittest.skipUnless(pty is not None, "pty is not available on this platform")
class InteractiveExistingFilesTest(TempDirTestCase):
    """The copy and move paths for files already in the target format.

    These need a TTY, and they are where the destination collision used to
    destroy the very file the user asked to keep.
    """

    def setUp(self):
        super().setUp()
        self.source = self.tmp / "music"
        self.source.mkdir()
        make_tone(self.source / "song.wav", frequency=440)
        make_tone(self.source / "song.mp3", frequency=660)
        self.original = (self.source / "song.mp3").read_bytes()

    def test_copied_original_is_not_overwritten_by_a_conversion(self):
        # Uppercase on purpose: the answers are case-insensitive.
        code, output = run_aconv_interactive("music", "mp3", cwd=self.tmp, feed="\nC\n")

        self.assertEqual(code, 0, output)
        dest = self.tmp / "music_mp3"
        self.assertEqual(sorted(p.name for p in dest.iterdir()),
                         ["song.mp3", "song_wav.mp3"])
        self.assertEqual((dest / "song.mp3").read_bytes(), self.original,
                         "the copied original was overwritten by a conversion")

    def test_moved_original_survives(self):
        code, output = run_aconv_interactive("music", "mp3", cwd=self.tmp, feed="\nM\nYES\n")

        self.assertEqual(code, 0, output)
        dest = self.tmp / "music_mp3"
        self.assertEqual(sorted(p.name for p in dest.iterdir()),
                         ["song.mp3", "song_wav.mp3"])
        self.assertEqual((dest / "song.mp3").read_bytes(), self.original,
                         "the moved original was overwritten and is now lost")
        self.assertFalse((self.source / "song.mp3").exists(), "move left the original in place")

    def test_move_without_confirmation_falls_back_to_skip(self):
        code, output = run_aconv_interactive("music", "mp3", cwd=self.tmp, feed="\nm\nno\n")

        self.assertEqual(code, 0, output)
        self.assertIn("Move cancelled", output)
        self.assertTrue((self.source / "song.mp3").exists(), "a cancelled move removed the original")
        self.assertEqual(sorted(p.name for p in (self.tmp / "music_mp3").iterdir()), ["song.mp3"])

    def test_a_full_command_line_does_not_ask_about_the_extension_filter(self):
        code, output = run_aconv_interactive("music", "mp3", cwd=self.tmp, feed="s\n")

        self.assertEqual(code, 0, output)
        self.assertNotIn("specific source extension", output)

    def test_a_bare_invocation_still_asks_for_everything(self):
        code, output = run_aconv_interactive(cwd=self.tmp, feed="music\nmp3\n\ns\n")

        self.assertEqual(code, 0, output)
        self.assertIn("Enter the source directory", output)
        self.assertIn("specific source extension", output)
        # song.mp3 was skipped, so it reserves nothing and the conversion of
        # song.wav is free to take the plain name.
        self.assertEqual([p.name for p in (self.tmp / "music_mp3").iterdir()], ["song.mp3"])

    def test_no_input_never_prompts_on_a_tty(self):
        code, output = run_aconv_interactive("music", "mp3", "--no-input", cwd=self.tmp, feed="")

        self.assertEqual(code, 0, output)
        self.assertNotIn("Would you like to", output)
        self.assertIn("skipping files already in the target format", output)

    def test_closed_stdin_on_a_tty_does_not_traceback(self):
        # EOT at a prompt is what raised an unhandled EOFError before. One EOT
        # ends one read, so send one for each of the two prompts on this path.
        code, output = run_aconv_interactive("music", "mp3", cwd=self.tmp, feed="\x04\x04")

        self.assertNotIn("Traceback", output)
        self.assertNotIn("EOFError", output)
        self.assertEqual(code, 0, output)


class CliValidationTest(TempDirTestCase):
    def test_bitrate_and_quality_are_mutually_exclusive(self):
        source = self.tmp / "music"
        source.mkdir()

        result = run_aconv("music", "mp3", "--bitrate", "320k", "--quality", "2", cwd=self.tmp)

        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with", result.stderr)

    def test_zero_workers_is_rejected(self):
        result = run_aconv("music", "mp3", "--workers", "0", cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--workers must be a positive integer", result.stdout)

    def test_missing_source_is_reported(self):
        result = run_aconv("nope", "mp3", cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stdout)


if __name__ == "__main__":
    unittest.main()
