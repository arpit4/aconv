"""Tests for aconv.

Fixtures are generated on the fly with ffmpeg's lavfi source, so the repository
needs no binary test files. Run with:

    python3 -m unittest test_aconv -v
"""

import argparse
import contextlib
import errno
import io
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
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


class FailureReasonTest(unittest.TestCase):
    def test_short_output_is_kept_whole(self):
        self.assertEqual(
            aconv.failure_reason("Header missing\nInvalid data found"),
            "Header missing; Invalid data found")

    def test_long_output_is_cut_to_the_first_meaningful_line(self):
        err = "\n".join(["[mp3 @ 0x0] Header missing"] + ["decode error"] * 50)
        self.assertEqual(aconv.failure_reason(err), "[mp3 @ 0x0] Header missing")

    def test_leading_blank_lines_are_not_meaningful(self):
        self.assertEqual(aconv.failure_reason("\n   \nreal error\n"), "real error")

    def test_empty_output_still_yields_a_reason(self):
        self.assertEqual(aconv.failure_reason(""), "ffmpeg reported no error output")


class StreamCopyDecisionTest(unittest.TestCase):
    """The remux-or-encode decision, with the codec probe stubbed out."""

    def stub_probe(self, value):
        original = aconv.probe_audio_codecs
        aconv.probe_audio_codecs = lambda path: value
        self.addCleanup(setattr, aconv, "probe_audio_codecs", original)

    def test_matching_codec_is_remuxed(self):
        self.stub_probe(("flac",))
        self.assertTrue(aconv.should_stream_copy(Path("song.mka"), "flac"))

    def test_mismatched_codec_is_encoded(self):
        self.stub_probe(("vorbis",))
        self.assertFalse(aconv.should_stream_copy(Path("song.mka"), "flac"))

    def test_probe_failure_falls_back_to_encode(self):
        self.stub_probe(None)
        self.assertFalse(aconv.should_stream_copy(Path("song.mka"), "flac"))

    def test_a_mixed_codec_source_is_encoded(self):
        """ffmpeg picks the kept stream itself, so any mismatch means encode."""
        self.stub_probe(("flac", "vorbis"))
        self.assertFalse(aconv.should_stream_copy(Path("song.mka"), "flac"))

    def test_an_unmapped_target_never_probes(self):
        def explode(path):
            raise AssertionError("probed a file for a target with no codec map")
        original = aconv.probe_audio_codecs
        aconv.probe_audio_codecs = explode
        self.addCleanup(setattr, aconv, "probe_audio_codecs", original)

        self.assertFalse(aconv.should_stream_copy(Path("song.flac"), "mka"))

    def test_stream_copy_args_copy_the_audio_stream(self):
        args = argparse.Namespace(bitrate=None, quality=None, sample_rate=None)

        remux = aconv.build_ffmpeg_args(args, "flac", stream_copy=True)
        encode = aconv.build_ffmpeg_args(args, "flac")

        copy_at = remux.index("-c:a")
        self.assertEqual(remux[copy_at:copy_at + 2], ["-c:a", "copy"])
        self.assertNotIn("-c:a", encode)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required")
class ResolveOptionsTest(TempDirTestCase):
    """The copy/move/skip decision and destination resolution, in-process.

    resolve_options() only inspects names and paths, nothing is converted,
    so empty files are enough, but check_ffmpeg() still runs inside it.
    """

    def setUp(self):
        super().setUp()
        # macOS keeps temporary directories behind a /var -> /private/var
        # symlink, and resolve_options() resolves the paths it is given, so
        # resolve ours too or none of the path assertions would compare equal.
        self.tmp = self.tmp.resolve()
        self.source = self.tmp / "music"
        self.source.mkdir()
        (self.source / "song.wav").touch()
        (self.source / "song.mp3").touch()

    def resolve(self, *cli_args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            options = aconv.resolve_options(list(cli_args))
        return options, out.getvalue()

    def test_on_existing_copy_reserves_the_kept_destination(self):
        options, _ = self.resolve(str(self.source), "mp3", "--on-existing", "copy")

        dest = self.tmp / "music_mp3"
        self.assertEqual(options.choice, 'c')
        self.assertEqual(options.keep_plan, [(self.source / "song.mp3", dest / "song.mp3")])
        self.assertEqual(options.convert_plan,
                         [(self.source / "song.wav", dest / "song_wav.mp3")])

    def test_on_existing_move_needs_no_confirmation(self):
        options, _ = self.resolve(str(self.source), "mp3", "--on-existing", "move")

        self.assertEqual(options.choice, 'm')
        self.assertEqual(options.keep_plan,
                         [(self.source / "song.mp3", self.tmp / "music_mp3" / "song.mp3")])

    def test_on_existing_skip_reserves_nothing(self):
        options, _ = self.resolve(str(self.source), "mp3", "--on-existing", "skip")

        self.assertEqual(options.choice, 's')
        self.assertEqual(options.keep_plan, [])
        self.assertEqual(options.convert_plan,
                         [(self.source / "song.wav", self.tmp / "music_mp3" / "song.mp3")])

    def test_no_input_defaults_to_skip(self):
        options, output = self.resolve(str(self.source), "mp3", "--no-input")

        self.assertEqual(options.choice, 's')
        self.assertIn("skipping files already in the target format", output)

    def test_default_destination_sits_beside_the_source(self):
        options, _ = self.resolve(str(self.source), "mp3", "--no-input")
        self.assertEqual(options.dest_dir, self.tmp / "music_mp3")

    def test_a_file_source_names_its_destination_after_the_file(self):
        track = self.tmp / "loose.wav"
        track.touch()

        options, _ = self.resolve(str(track), "mp3", "--no-input")

        self.assertEqual(options.dest_dir, self.tmp / "loose_mp3")
        self.assertEqual(options.convert_plan, [(track, self.tmp / "loose_mp3" / "loose.mp3")])

    def test_dest_flag_overrides_the_default(self):
        options, _ = self.resolve(str(self.source), "mp3",
                                  "--dest", str(self.tmp / "elsewhere"), "--no-input")
        self.assertEqual(options.dest_dir, self.tmp / "elsewhere")

    def test_skip_existing_filters_both_plans(self):
        dest = self.tmp / "music_mp3"
        dest.mkdir()
        (dest / "song.mp3").touch()

        options, output = self.resolve(str(self.source), "mp3",
                                       "--on-existing", "copy", "--skip-existing")

        self.assertEqual(options.keep_plan, [])
        self.assertEqual(options.convert_plan,
                         [(self.source / "song.wav", dest / "song_wav.mp3")])
        self.assertIn("leaving 1 existing output file(s) alone", output)


class ExecuteTest(TempDirTestCase):
    """The copy/move pass and the dry-run report, in-process.

    Plans are built by hand so no ffmpeg is needed: an empty convert plan
    makes execute() stop right after the pass under test.
    """

    def options(self, **overrides):
        base = dict(dest_dir=self.tmp / "out", target_format="mp3", extra_args=[],
                    remux_sources=set(), remux_args=[],
                    choice='s', keep_plan=[], convert_plan=[], dry_run=False,
                    workers=1, bitrate=None, quality=None, sample_rate=None,
                    progress='bar', emit=None)
        base.update(overrides)
        return argparse.Namespace(**base)

    def execute(self, options):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(out):
            aconv.execute(options)
        return caught.exception.code, out.getvalue()

    def test_copy_pass_keeps_the_original(self):
        source = self.tmp / "song.mp3"
        source.write_bytes(b"original")
        options = self.options(choice='c',
                               keep_plan=[(source, self.tmp / "out" / "song.mp3")])

        code, output = self.execute(options)

        self.assertEqual(code, 0)
        self.assertIn("Copying 1 files", output)
        self.assertEqual((self.tmp / "out" / "song.mp3").read_bytes(), b"original")
        self.assertTrue(source.exists(), "copy removed the original")

    def test_move_pass_removes_the_original(self):
        source = self.tmp / "song.mp3"
        source.write_bytes(b"original")
        options = self.options(choice='m',
                               keep_plan=[(source, self.tmp / "out" / "song.mp3")])

        code, output = self.execute(options)

        self.assertEqual(code, 0)
        self.assertIn("Moving 1 files", output)
        self.assertEqual((self.tmp / "out" / "song.mp3").read_bytes(), b"original")
        self.assertFalse(source.exists(), "move left the original in place")

    def test_dry_run_writes_nothing(self):
        source = self.tmp / "song.mp3"
        source.write_bytes(b"original")
        options = self.options(choice='c', dry_run=True,
                               keep_plan=[(source, self.tmp / "out" / "song.mp3")],
                               convert_plan=[(self.tmp / "song.wav",
                                              self.tmp / "out" / "song_wav.mp3")])

        code, output = self.execute(options)

        self.assertEqual(code, 0)
        self.assertFalse((self.tmp / "out").exists(), "dry run created the destination")
        self.assertIn("1 file(s) to copy, 1 to convert.", output)


class AtomicMoveTest(TempDirTestCase):
    """The cross-filesystem branch of the move pass.

    shutil.move degrades to copy-then-delete when the destination sits on
    another filesystem, so an interrupt mid-move used to lose the file: gone
    from the source, half-written at the destination. os.replace cannot cross
    filesystems in a test, so the EXDEV refusal is injected instead.
    """

    def setUp(self):
        super().setUp()
        self.source = self.tmp / "song.mp3"
        self.source.write_bytes(b"original")
        self.dest_dir = self.tmp / "out"
        self.dest_dir.mkdir()
        self.dest = self.dest_dir / "song.mp3"

    def force_exdev(self):
        """Make the first os.replace fail the way a cross-device rename does."""
        real_replace = os.replace
        calls = []

        def cross_device_replace(src, dst):
            if not calls:
                calls.append(src)
                raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
            return real_replace(src, dst)

        os.replace = cross_device_replace
        self.addCleanup(setattr, os, "replace", real_replace)
        return calls

    def test_cross_filesystem_move_completes(self):
        calls = self.force_exdev()

        aconv.move_file(self.source, self.dest)

        self.assertEqual(len(calls), 1, "the EXDEV fallback was never exercised")
        self.assertEqual(self.dest.read_bytes(), b"original")
        self.assertFalse(self.source.exists(), "move left the original in place")
        self.assertEqual([p.name for p in self.dest_dir.iterdir()], ["song.mp3"],
                         "the staging file was left behind")

    def test_interrupted_copy_keeps_the_source_and_no_half_destination(self):
        self.force_exdev()

        def interrupted_copy(fsrc, fdst, length=0):
            # Write part of the data first, so that a leaked staging file
            # would be a half-file rather than an empty one.
            fdst.write(b"orig")
            raise KeyboardInterrupt

        real_copy = shutil.copyfileobj
        shutil.copyfileobj = interrupted_copy
        self.addCleanup(setattr, shutil, "copyfileobj", real_copy)

        with self.assertRaises(KeyboardInterrupt):
            aconv.move_file(self.source, self.dest)

        self.assertEqual(self.source.read_bytes(), b"original", "the source was damaged")
        self.assertFalse(self.dest.exists(), "a half-file appeared under the final name")
        self.assertEqual(list(self.dest_dir.iterdir()), [],
                         "the staging file was not cleaned up")


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

    def test_failure_summary_lists_exactly_the_failed_files(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "good.wav")
        (source / "bad_one.flac").write_text("this is not audio at all\n")
        (source / "bad_two.wav").write_text("nor is this\n")

        result = run_aconv("music", "mp3", cwd=self.tmp)

        self.assertEqual(result.returncode, 1, result.stdout)
        lines = result.stdout.splitlines()
        self.assertIn("2 file(s) failed to convert:", lines, result.stdout)
        listed = lines[lines.index("2 file(s) failed to convert:") + 1:]
        self.assertEqual(
            [line.split(": ", 1)[0] for line in listed],
            [f"  {(source / 'bad_one.flac').resolve()}",
             f"  {(source / 'bad_two.wav').resolve()}"])
        for line in listed:
            self.assertNotEqual(line.split(": ", 1)[1].strip(), "",
                                "a failure was listed without a reason")

    def test_successful_run_prints_no_failure_summary(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "a.wav")

        result = run_aconv("music", "mp3", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Conversion complete!", result.stdout)
        self.assertNotIn("failed to convert", result.stdout)

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
class StreamCopyTest(TempDirTestCase):
    """Cross-container remuxes: FLAC inside Matroska converted to .flac."""

    def setUp(self):
        super().setUp()
        self.tmp = self.tmp.resolve()
        self.source = self.tmp / "music"
        self.source.mkdir()
        make_tone(self.source / "song.mka", extra=["-c:a", "flac"])

    def resolve(self, *cli_args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            options = aconv.resolve_options(list(cli_args))
        return options, out.getvalue()

    def test_flac_in_mka_is_remuxed_to_flac(self):
        result = run_aconv("music", "flac", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("remuxing without re-encoding", result.stdout)
        _, codec = probe(self.tmp / "music_flac" / "song.flac", "stream=codec_name")
        self.assertEqual(codec, "flac")

    def test_resolve_marks_the_matching_source_for_remux(self):
        options, _ = self.resolve(str(self.source), "flac", "--no-input")

        self.assertEqual(options.remux_sources, {self.source / "song.mka"})
        copy_at = options.remux_args.index("-c:a")
        self.assertEqual(options.remux_args[copy_at:copy_at + 2], ["-c:a", "copy"])

    def test_bitrate_forces_a_real_encode(self):
        options, _ = self.resolve(str(self.source), "flac", "--no-input",
                                  "--bitrate", "320k")
        self.assertEqual(options.remux_sources, set())

    def test_sample_rate_forces_a_real_encode(self):
        options, _ = self.resolve(str(self.source), "flac", "--no-input",
                                  "--sample-rate", "22050")
        self.assertEqual(options.remux_sources, set())

    def test_probe_failure_falls_back_to_a_normal_encode(self):
        original = aconv.probe_audio_codecs
        aconv.probe_audio_codecs = lambda path: None
        self.addCleanup(setattr, aconv, "probe_audio_codecs", original)

        options, _ = self.resolve(str(self.source), "flac", "--no-input")

        self.assertEqual(options.remux_sources, set())
        self.assertEqual(options.convert_plan,
                         [(self.source / "song.mka", self.tmp / "music_flac" / "song.flac")])

    def test_dry_run_reports_the_remux(self):
        result = run_aconv("music", "flac", "--dry-run", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("remuxing", result.stdout)
        self.assertNotIn("converting", result.stdout)
        self.assertFalse((self.tmp / "music_flac").exists(), "dry run created the destination")


class CancelControlTest(TempDirTestCase):
    """The registry that lets a stdin-control cancel reach ffmpeg itself.

    interrupt_main() alone stops nothing that is already running: the ffmpeg
    children never see a signal through a pipe, and the pending interrupt is
    not delivered while the main thread waits untimed on a future.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(aconv._cancel_event.clear)

    def test_the_sweep_terminates_a_registered_process(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        with aconv._live_lock:
            aconv._live_processes.add(process)
        self.addCleanup(aconv._live_processes.discard, process)

        aconv.cancel_active_conversions()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("cancel_active_conversions left the process running")

    def test_convert_file_refuses_to_start_after_a_cancel(self):
        aconv._cancel_event.set()

        error = aconv.convert_file(self.tmp / "in.wav", self.tmp / "out.mp3")

        self.assertEqual(error, "cancelled before it started")
        self.assertFalse((self.tmp / "out.mp3").exists())

    def test_a_cancel_line_on_stdin_reaches_a_running_conversion(self):
        """The wiring the end-to-end tests cannot pin down.

        Whether a real encode outlives the cancel is a race against the
        machine, so the claim that a cancel reaches what is *already running*
        is made here instead, against a process that will not finish on its
        own.
        """
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        with aconv._live_lock:
            aconv._live_processes.add(process)
        self.addCleanup(aconv._live_processes.discard, process)

        # interrupt_main() would raise in the test runner's own main thread;
        # the sweep is what this test is about, so stub the interrupt out.
        original_interrupt = aconv._thread.interrupt_main
        aconv._thread.interrupt_main = lambda: None
        self.addCleanup(setattr, aconv._thread, "interrupt_main", original_interrupt)
        # start_stdin_control installs handlers on the way past; leave the
        # runner's own signal disposition as it found it.
        for number in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, number, signal.getsignal(number))
        original_stdin = sys.stdin
        sys.stdin = io.StringIO("cancel\n")
        self.addCleanup(setattr, sys, "stdin", original_stdin)

        aconv.start_stdin_control()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("a cancel line on stdin never reached the running process")


class InterruptTest(TempDirTestCase):
    """Ctrl-C has to stop the batch, not just stop reporting on it.

    ThreadPoolExecutor.shutdown(wait=True) drains the queue instead of dropping
    it, so an interrupted run used to keep starting a fresh ffmpeg for every
    remaining file with the progress bar already gone.
    """

    def _plan(self, count):
        return [(self.tmp / f"in{i}.wav", self.tmp / f"out{i}.mp3") for i in range(count)]

    def test_interrupt_drops_the_queued_conversions(self):
        # 40 files at 0.05s each is ~2s of work, interrupted after ~0.15s. With
        # the queue dropped only a handful ever start; without, all 40 do.
        plan = self._plan(40)
        started = []

        def slow_convert(source_file, dest_file, extra_args=None):
            started.append(source_file)
            if len(started) == 1:
                # Ctrl-C arriving mid-batch, which is the case that matters.
                # It has to be delayed rather than sent from here: signalling
                # inline interrupts the main thread while it is still submitting,
                # which never reaches the code under test.
                threading.Timer(0.15, os.kill, [os.getpid(), signal.SIGINT]).start()
            time.sleep(0.05)
            return None

        original = aconv.convert_file
        aconv.convert_file = slow_convert
        self.addCleanup(setattr, aconv, "convert_file", original)

        with self.assertRaises(KeyboardInterrupt):
            aconv.run_conversions(plan, [], workers=1, weights=None)

        # The interrupt unwinds the main thread while the pool is still settling,
        # so counting immediately would pass either way. Wait for the worker
        # threads to go quiet first: that is where the queue used to drain.
        self.assertTrue(self._wait_until_quiet(started), "the pool never settled")
        self.assertLess(len(started), 15,
                        f"the batch kept converting after the interrupt: "
                        f"{len(started)} of {len(plan)} files were started")

    @staticmethod
    def _wait_until_quiet(started, settle=0.15, rounds=3, timeout=10):
        """Wait until `started` has stopped growing for `rounds` consecutive checks."""
        deadline = time.monotonic() + timeout
        quiet = 0
        while time.monotonic() < deadline:
            before = len(started)
            time.sleep(settle)
            if len(started) == before:
                quiet += 1
                if quiet == rounds:
                    return True
            else:
                quiet = 0
        return False

    def test_interrupt_drops_the_queued_duration_probes(self):
        """The measuring pass runs its own pool, with the same drain problem."""
        probed = []

        def slow_probe(path):
            probed.append(path)
            if len(probed) == 1:
                threading.Timer(0.15, os.kill, [os.getpid(), signal.SIGINT]).start()
            time.sleep(0.05)
            return 1.0

        original = aconv.probe_duration
        aconv.probe_duration = slow_probe
        self.addCleanup(setattr, aconv, "probe_duration", original)

        files = [self.tmp / f"in{i}.wav" for i in range(40)]
        with self.assertRaises(KeyboardInterrupt):
            aconv.measure_durations(files, workers=1)

        self.assertTrue(self._wait_until_quiet(probed), "the pool never settled")
        self.assertLess(len(probed), 15,
                        f"kept probing after the interrupt: {len(probed)} of {len(files)}")

    def test_uninterrupted_runs_convert_everything(self):
        plan = self._plan(6)
        converted = []

        def fake_convert(source_file, dest_file, extra_args=None):
            converted.append(source_file)
            return None

        original = aconv.convert_file
        aconv.convert_file = fake_convert
        self.addCleanup(setattr, aconv, "convert_file", original)

        success, failures = aconv.run_conversions(plan, [], workers=2, weights=None)

        self.assertEqual(success, 6)
        self.assertEqual(failures, [])
        self.assertEqual(len(converted), 6)

    def test_failures_are_counted_but_do_not_stop_the_batch(self):
        plan = self._plan(4)

        def flaky_convert(source_file, dest_file, extra_args=None):
            if source_file.name == "in2.wav":
                return "synthetic"
            return None

        original = aconv.convert_file
        aconv.convert_file = flaky_convert
        self.addCleanup(setattr, aconv, "convert_file", original)

        success, failures = aconv.run_conversions(plan, [], workers=2, weights=None)

        self.assertEqual(success, 3)
        self.assertEqual(failures, [(self.tmp / "in2.wav", "synthetic")])


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
class JsonlProtocolTest(TempDirTestCase):
    """The --progress jsonl contract: pure JSON on stdout, humans on stderr.

    The event stream is what a GUI or any other machine consumer parses, so
    these tests pin the field names and the ordering guarantees, not just
    that something JSON-shaped comes out.
    """

    def setUp(self):
        super().setUp()
        # run_aconv resolves the paths it prints, so resolve ours too or none
        # of the source/dest assertions would compare equal on macOS.
        self.tmp = self.tmp.resolve()

    def events(self, result):
        """Parse stdout as one JSON object per line; anything else fails."""
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                self.fail(f"stdout is not pure JSON: {line!r}")
        return events

    def test_full_event_stream_for_a_real_conversion(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "a.wav", frequency=440)
        make_tone(source / "b.flac", frequency=550)
        make_tone(source / "keep.mp3", frequency=660)

        result = run_aconv("music", "mp3", "--progress", "jsonl",
                           "--on-existing", "copy", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events(result)
        self.assertEqual(events[0],
                         {"event": "hello", "protocol": 1, "version": aconv.__version__})
        kinds = [e["event"] for e in events]
        scan = events[kinds.index("scan")]
        self.assertEqual((scan["found"], scan["to_convert"], scan["already_in_format"]),
                         (3, 2, 1))
        plan = events[kinds.index("plan")]
        self.assertEqual((plan["keep"], plan["convert"], plan["remux"], plan["on_existing"]),
                         (1, 2, 0, "copy"))
        self.assertEqual(plan["dest"], str(self.tmp / "music_mp3"))
        self.assertEqual(events[kinds.index("keep_done")]["count"], 1)
        probes = [e for e in events if e["event"] == "probe"]
        self.assertEqual(probes[-1], {"event": "probe", "done": 2, "total": 2})
        done_for = [e["source"] for e in events if e["event"] == "file_done"]
        self.assertEqual(sorted(done_for),
                         sorted([str(source / "a.wav"), str(source / "b.flac")]))
        for e in events:
            if e["event"] == "file_done":
                self.assertIsInstance(e["seconds"], float)
        # Every file_start precedes its file_done.
        for src in done_for:
            start_at = next(i for i, e in enumerate(events)
                            if e["event"] == "file_start" and e["source"] == src)
            done_at = next(i for i, e in enumerate(events)
                           if e["event"] == "file_done" and e["source"] == src)
            self.assertLess(start_at, done_at)
        self.assertEqual(events[-1], {"event": "done", "converted": 2, "failed": 0})
        self.assertIn("Conversion complete!", result.stderr,
                      "the human summary did not go to stderr")

    def test_ffmpeg_failure_emits_file_failed(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "good.wav")
        (source / "broken.flac").write_text("this is not audio at all\n")

        result = run_aconv("music", "mp3", "--progress", "jsonl", cwd=self.tmp)

        self.assertEqual(result.returncode, 1, result.stderr)
        events = self.events(result)
        failed = [e for e in events if e["event"] == "file_failed"]
        self.assertEqual([e["source"] for e in failed], [str(source / "broken.flac")])
        self.assertNotEqual(failed[0]["error"].strip(), "",
                            "file_failed carried no ffmpeg stderr")
        self.assertEqual(events[-1], {"event": "done", "converted": 1, "failed": 1})

    def test_plan_counts_convert_and_remux_disjointly(self):
        # A consumer totals the batch as convert + remux, so a remuxed file
        # must not be counted under both fields.
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "song.mka", extra=["-c:a", "flac"])
        make_tone(source / "other.wav")

        result = run_aconv("music", "flac", "--progress", "jsonl", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events(result)
        plan = next(e for e in events if e["event"] == "plan")
        self.assertEqual((plan["convert"], plan["remux"]), (1, 1))
        actions = sorted(e["action"] for e in events if e["event"] == "file_start")
        self.assertEqual(actions, ["convert", "remux"])

    def test_dry_run_emits_plan_items_and_writes_nothing(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "song.wav")
        make_tone(source / "keep.mp3")

        result = run_aconv("music", "mp3", "--progress", "jsonl", "--dry-run",
                           "--on-existing", "copy", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        items = [e for e in self.events(result) if e["event"] == "plan_item"]
        self.assertEqual(
            sorted((e["action"], e["source"], e["dest"]) for e in items),
            sorted([("copy", str(source / "keep.mp3"),
                     str(self.tmp / "music_mp3" / "keep.mp3")),
                    ("convert", str(source / "song.wav"),
                     str(self.tmp / "music_mp3" / "song.mp3"))]))
        self.assertFalse((self.tmp / "music_mp3").exists(), "dry run created the destination")

    def test_missing_source_emits_an_error_event(self):
        result = run_aconv("--progress", "jsonl", cwd=self.tmp)

        self.assertEqual(result.returncode, 1)
        events = self.events(result)
        self.assertEqual(events[0]["event"], "hello")
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("source is required", events[-1]["message"])

    def test_stdin_control_cancel_stops_the_run(self):
        source = self.tmp / "music"
        source.mkdir()
        # Long enough that the batch cannot finish before the cancel lands.
        for i in range(5):
            make_tone(source / f"tone{i}.wav", duration=20)

        process = subprocess.Popen(
            [sys.executable, ACONV, "music", "mp3", "--progress", "jsonl",
             "--stdin-control", "--workers", "1"],
            cwd=str(self.tmp), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        events = []
        try:
            for line in process.stdout:
                events.append(json.loads(line))
                if events[-1]["event"] == "file_start":
                    process.stdin.write("cancel\n")
                    process.stdin.flush()
                    break
            events.extend(json.loads(line) for line in process.stdout)
            code = process.wait(timeout=120)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            stderr = process.stderr.read()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

        self.assertEqual(code, 130, stderr)
        kinds = [e["event"] for e in events]
        self.assertIn("interrupted", kinds)
        self.assertIn("dropped", events[kinds.index("interrupted")])
        self.assertNotIn("done", kinds, "the batch ran to completion despite the cancel")
        # Whatever was mid-flight when the cancel landed must have finished
        # whole or been removed, never left truncated.
        dest = self.tmp / "music_mp3"
        if dest.exists():
            for output in dest.iterdir():
                returncode, duration = probe(output, "format=duration")
                self.assertEqual(returncode, 0, f"{output.name} was left unreadable")
                self.assertAlmostEqual(float(duration), 20.0, delta=1.0,
                                       msg=f"{output.name} was left truncated")

    @unittest.skipUnless(os.name == "posix", "SIGTERM delivery is POSIX-only")
    def test_sigterm_cleans_up_like_a_cancel(self):
        # A controller that gives up on the stdin channel falls back to
        # terminating the process; that must behave like a cancel (drop the
        # batch, exit 130, leave nothing truncated) rather than kill aconv
        # and leave an orphaned encoder writing a partial file.
        #
        # A batch rather than one long file: how much of a single encode
        # survives the signal is a race against the machine, but a queue that
        # cannot drain in the signal's window makes the outcome the same
        # everywhere.
        source = self.tmp / "music"
        source.mkdir()
        for i in range(5):
            make_tone(source / f"tone{i}.wav", duration=20)

        process = subprocess.Popen(
            [sys.executable, ACONV, "music", "mp3", "--progress", "jsonl",
             "--stdin-control", "--workers", "1"],
            cwd=str(self.tmp), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        events = []
        try:
            for line in process.stdout:
                events.append(json.loads(line))
                if events[-1]["event"] == "file_start":
                    process.send_signal(signal.SIGTERM)
                    break
            events.extend(json.loads(line) for line in process.stdout)
            code = process.wait(timeout=60)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            stderr = process.stderr.read()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

        self.assertEqual(code, 130, stderr)
        self.assertNotIn("done", [e["event"] for e in events],
                         "the batch ran to completion despite the SIGTERM")
        # Whatever the signal caught mid-encode is either finished whole or
        # gone; a truncated output would make a later --skip-existing resume
        # trust a broken file.
        dest = self.tmp / "music_mp3"
        if dest.exists():
            for output in dest.iterdir():
                returncode, duration = probe(output, "format=duration")
                self.assertEqual(returncode, 0, f"{output.name} was left unreadable")
                self.assertAlmostEqual(float(duration), 20.0, delta=1.0,
                                       msg=f"{output.name} was left truncated")

    @unittest.skipUnless(os.name == "posix", "preexec_fn is POSIX-only")
    def test_stdin_control_cancel_works_with_sigint_ignored(self):
        # A shell starts background jobs with SIGINT ignored, the child
        # inherits that through exec, and interrupt_main() is then a silent
        # no-op: without a restored handler the cancel channel does nothing
        # and the batch runs to completion.
        source = self.tmp / "music"
        source.mkdir()
        for i in range(3):
            make_tone(source / f"tone{i}.wav", duration=20)

        process = subprocess.Popen(
            [sys.executable, ACONV, "music", "mp3", "--progress", "jsonl",
             "--stdin-control", "--workers", "1"],
            cwd=str(self.tmp), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
        events = []
        try:
            for line in process.stdout:
                events.append(json.loads(line))
                if events[-1]["event"] == "file_start":
                    process.stdin.write("cancel\n")
                    process.stdin.flush()
                    break
            events.extend(json.loads(line) for line in process.stdout)
            code = process.wait(timeout=60)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            stderr = process.stderr.read()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

        self.assertEqual(code, 130, stderr)
        self.assertNotIn("done", [e["event"] for e in events],
                         "the batch ran to completion despite the cancel")

    def test_default_mode_emits_no_json_and_is_unchanged(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "a.wav")

        result = run_aconv("music", "mp3", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("{", result.stdout)
        self.assertIn("Found 1 audio files. Converting to .mp3...", result.stdout)
        self.assertIn(f"Destination: {self.tmp / 'music_mp3'}", result.stdout)
        self.assertIn("Conversion complete! 1/1 files successfully converted.",
                      result.stdout)

    def test_progress_none_keeps_the_human_output_without_a_bar(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "a.wav")

        result = run_aconv("music", "mp3", "--progress", "none", cwd=self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Conversion complete! 1/1 files successfully converted.",
                      result.stdout)
        self.assertEqual(result.stderr, "", "something other than the bar wrote to stderr")
        self.assertTrue((self.tmp / "music_mp3" / "a.mp3").is_file())

    def test_jsonl_never_imports_tqdm(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "a.wav")
        script = (
            "import sys\n"
            "import aconv\n"
            "options = aconv.resolve_options([sys.argv[1], 'mp3', '--progress', 'jsonl'])\n"
            "aconv.execute(options)\n"
            "assert 'tqdm' not in sys.modules, 'a jsonl run imported tqdm'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(source)],
            cwd=str(Path(ACONV).parent), stdin=subprocess.DEVNULL,
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


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
    def test_version_is_reported_without_needing_a_source(self):
        result = run_aconv("--version", cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"aconv {aconv.__version__}")

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

    def test_stdin_control_requires_jsonl(self):
        result = run_aconv("music", "mp3", "--stdin-control", cwd=self.tmp)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--stdin-control requires --progress jsonl", result.stderr)


if __name__ == "__main__":
    unittest.main()
