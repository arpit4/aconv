"""Tests for aconv_gui.

Everything below the App layer is Tk-free by design, so almost all of this
suite runs headless. Conversion fixtures come from ffmpeg's lavfi source, the
same way test_aconv builds them. Run with:

    python3 -m unittest test_aconv_gui -v
"""

import os
import pathlib
import queue
import shutil
import sys
import time
import unittest
from pathlib import Path

import aconv_gui
from test_aconv import HAS_FFMPEG, TempDirTestCase, make_tone


def settings(**overrides):
    """A full settings dict: build_argv always sees every key, like the App sends."""
    base = dict(source="/in/music", format="mp3", dest=None, workers=None,
                bitrate=None, quality=None, sample_rate=None,
                on_existing=None, skip_existing=False, dry_run=False)
    base.update(overrides)
    return base


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


class BuildArgvTest(unittest.TestCase):
    def test_minimal_settings_produce_exactly_the_dictated_argv(self):
        # --on-existing is always passed, defaulting to skip, so the child
        # never depends on TTY detection.
        argv = aconv_gui.build_argv(settings())

        self.assertEqual(argv, [sys.executable, str(aconv_gui.ACONV),
                                "/in/music", "mp3",
                                "--progress", "jsonl", "--stdin-control",
                                "--on-existing", "skip"])

    def test_aconv_is_the_sibling_script(self):
        self.assertEqual(aconv_gui.ACONV.name, "aconv.py")
        self.assertTrue(aconv_gui.ACONV.is_file(),
                        f"ACONV does not point at the real script: {aconv_gui.ACONV}")

    def test_on_existing_passes_the_chosen_policy(self):
        argv = aconv_gui.build_argv(settings(on_existing="move"))
        self.assertEqual(flag_value(argv, "--on-existing"), "move")

    def test_none_and_false_settings_omit_their_flags(self):
        argv = aconv_gui.build_argv(settings())

        for flag in ("--dest", "--workers", "--bitrate", "--quality",
                     "--sample-rate", "--skip-existing", "--dry-run"):
            self.assertNotIn(flag, argv)

    def test_value_flags_carry_their_values(self):
        argv = aconv_gui.build_argv(settings(dest="/out/dir", workers=3,
                                             bitrate="320k", sample_rate=44100))

        self.assertEqual(flag_value(argv, "--dest"), "/out/dir")
        self.assertEqual(flag_value(argv, "--workers"), "3")
        self.assertEqual(flag_value(argv, "--bitrate"), "320k")
        self.assertEqual(flag_value(argv, "--sample-rate"), "44100")

    def test_quality_is_passed_when_bitrate_is_not(self):
        argv = aconv_gui.build_argv(settings(quality=2))
        self.assertEqual(flag_value(argv, "--quality"), "2")

    def test_boolean_settings_become_bare_flags(self):
        argv = aconv_gui.build_argv(settings(skip_existing=True, dry_run=True))
        self.assertIn("--skip-existing", argv)
        self.assertIn("--dry-run", argv)

    def test_bitrate_and_quality_together_raise(self):
        with self.assertRaises(ValueError):
            aconv_gui.build_argv(settings(bitrate="320k", quality=2))

    def test_every_element_is_a_string(self):
        # subprocess argv must not contain ints, so numbers are stringified.
        argv = aconv_gui.build_argv(settings(dest="/out", workers=4,
                                             quality=2, sample_rate=22050))
        for element in argv:
            self.assertIsInstance(element, str, f"non-string argv element: {element!r}")


class ParseEventTest(unittest.TestCase):
    def test_a_protocol_line_is_returned_as_a_dict(self):
        line = '{"event": "file_done", "source": "a.wav", "dest": "a.mp3", "seconds": 1.5}'
        self.assertEqual(aconv_gui.parse_event(line),
                         {"event": "file_done", "source": "a.wav",
                          "dest": "a.mp3", "seconds": 1.5})

    def test_a_trailing_newline_is_tolerated(self):
        self.assertEqual(aconv_gui.parse_event('{"event": "done"}\n'), {"event": "done"})

    def test_non_dict_json_is_rejected(self):
        self.assertIsNone(aconv_gui.parse_event('[{"event": "done"}]'))
        self.assertIsNone(aconv_gui.parse_event('"hello"'))
        self.assertIsNone(aconv_gui.parse_event('42'))

    def test_a_dict_without_an_event_key_is_rejected(self):
        self.assertIsNone(aconv_gui.parse_event('{"progress": 0.5}'))

    def test_garbage_lines_are_rejected_not_raised(self):
        self.assertIsNone(aconv_gui.parse_event("Converting 3 files..."))
        self.assertIsNone(aconv_gui.parse_event(""))


class TranslateErrorTest(unittest.TestCase):
    """Each mapped pattern must come back as one plain-English sentence, not
    the raw ffmpeg line; the "[x @ 0x...]" prefix is the tell."""

    def assertTranslated(self, stderr_text, keywords):
        message = aconv_gui.translate_error(stderr_text)
        self.assertTrue(message.strip(), "translate_error returned nothing")
        self.assertNotIn("\n", message, f"more than one line: {message!r}")
        self.assertNotIn("@ 0x", message,
                         f"the raw ffmpeg line leaked through untranslated: {message!r}")
        low = message.lower()
        self.assertTrue(any(keyword in low for keyword in keywords),
                        f"expected one of {keywords} in: {message!r}")
        return message

    def test_invalid_data(self):
        self.assertTranslated(
            "[mp3 @ 0x7f8a] Header missing\n"
            "broken.mp3: Invalid data found when processing input",
            ("corrupt", "invalid", "damaged", "not a recognized", "not audio",
             "not a valid"))

    def test_permission_denied(self):
        self.assertTranslated(
            "[out#0 @ 0x5601] Error opening output out/song.mp3: Permission denied",
            ("permission",))

    def test_no_space_left(self):
        self.assertTranslated(
            "[out#0 @ 0x5601] av_interleaved_write_frame(): No space left on device",
            ("space", "full", "disk"))

    def test_unsupported_codec(self):
        self.assertTranslated(
            "[ogg @ 0x55d3] Unsupported codec id 98304 in stream 1",
            ("codec", "support", "format"))

    def test_missing_file(self):
        self.assertTranslated(
            "[in#0 @ 0x7f00] Error opening input: No such file or directory",
            ("exist", "found", "missing", "no such file"))

    def test_unmapped_output_falls_back_to_the_first_non_blank_line(self):
        text = "\n   \nSomething nobody mapped happened\nsecond line"
        self.assertEqual(aconv_gui.translate_error(text),
                         "Something nobody mapped happened")


class FindFfmpegTest(unittest.TestCase):
    """The fallback prefixes exist because Finder-launched apps get a minimal
    PATH. Every lookup is stubbed so the test cannot depend on the machine."""

    HOMEBREW = "/opt/homebrew/bin"
    USR_LOCAL = "/usr/local/bin"
    COVERED = ("/opt/homebrew", "/usr/local")

    def stub_lookup(self, which_result=None, present=()):
        """Answer shutil.which and every plausible existence probe for the
        fallback prefixes; anything outside them hits the real filesystem."""
        self.present = set(present)

        def install(module, name, replacement_factory):
            real = getattr(module, name)
            setattr(module, name, replacement_factory(real))
            self.addCleanup(setattr, module, name, real)

        def fake_which_factory(real):
            def fake_which(cmd, mode=os.F_OK | os.X_OK, path=None):
                if path is not None:
                    hits = (os.path.join(directory, cmd)
                            for directory in str(path).split(os.pathsep))
                    return next((hit for hit in hits if hit in self.present), None)
                return which_result
            return fake_which
        install(shutil, "which", fake_which_factory)

        def probe_factory(real):
            def probe(path, *args, **kwargs):
                text = str(os.fspath(path))
                if text.startswith(self.COVERED):
                    return text in self.present
                return real(path, *args, **kwargs)
            return probe
        for name in ("exists", "isfile", "isdir"):
            install(os.path, name, probe_factory)
        install(os, "access", probe_factory)

        def method_factory(real):
            def probe(path, *args, **kwargs):
                text = str(path)
                if text.startswith(self.COVERED):
                    return text in self.present
                return real(path, *args, **kwargs)
            return probe
        for name in ("exists", "is_file", "is_dir"):
            install(pathlib.Path, name, method_factory)

    def test_ffmpeg_on_path_wins(self):
        self.stub_lookup(which_result="/somewhere/bin/ffmpeg")
        self.assertEqual(aconv_gui.find_ffmpeg(), "/somewhere/bin")

    def test_homebrew_prefix_is_probed_when_path_misses(self):
        self.stub_lookup(present={self.HOMEBREW, self.HOMEBREW + "/ffmpeg"})
        self.assertEqual(aconv_gui.find_ffmpeg(), self.HOMEBREW)

    def test_usr_local_prefix_is_probed_too(self):
        self.stub_lookup(present={self.USR_LOCAL, self.USR_LOCAL + "/ffmpeg"})
        self.assertEqual(aconv_gui.find_ffmpeg(), self.USR_LOCAL)

    def test_homebrew_is_checked_before_usr_local(self):
        self.stub_lookup(present={self.HOMEBREW, self.HOMEBREW + "/ffmpeg",
                                  self.USR_LOCAL, self.USR_LOCAL + "/ffmpeg"})
        self.assertEqual(aconv_gui.find_ffmpeg(), self.HOMEBREW)

    def test_nothing_found_returns_none(self):
        self.stub_lookup()
        self.assertIsNone(aconv_gui.find_ffmpeg())


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required")
class RunnerProtocolTest(TempDirTestCase):
    """The real aconv.py driven over the jsonl protocol, no Tk involved."""

    def start(self, argv):
        runner = aconv_gui.Runner(argv)
        runner.start()
        # A failed assertion must not leave a child converting in the
        # background; cancel is the only stop lever the API dictates.
        self.addCleanup(lambda: runner.cancel() if runner.running else None)
        return runner

    def drain(self, runner, timeout=120):
        """Collect events through the _exit sentinel; a silent child is a
        failure, not a hang."""
        events = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = runner.events.get(timeout=1.0)
            except queue.Empty:
                continue
            events.append(event)
            if event.get("event") == "_exit":
                return events
        raise AssertionError(f"no _exit sentinel within {timeout}s; got: {events}")

    def test_a_two_file_run_reports_the_full_event_stream(self):
        source = self.tmp / "music"
        source.mkdir()
        make_tone(source / "a.wav")
        make_tone(source / "b.wav", frequency=880)
        argv = aconv_gui.build_argv(settings(source=str(source), format="mp3",
                                             dest=str(self.tmp / "out")))

        runner = self.start(argv)
        events = self.drain(runner)

        self.assertEqual(events[0].get("event"), "hello", events[0])
        self.assertEqual(events[0].get("protocol"), 1)
        done_names = sorted(Path(e["source"]).name for e in events
                            if e.get("event") == "file_done")
        self.assertEqual(done_names, ["a.wav", "b.wav"])
        self.assertEqual(events[-2].get("event"), "done", events)
        self.assertEqual(events[-2].get("converted"), 2)
        self.assertEqual(events[-2].get("failed"), 0)
        self.assertEqual(events[-1]["event"], "_exit")
        self.assertIn("stderr_tail", events[-1])
        self.assertEqual(events[-1]["returncode"], 0, events[-1].get("stderr_tail"))
        self.assertFalse(runner.running)
        self.assertTrue((self.tmp / "out" / "a.mp3").is_file())

    def test_cancel_stops_the_run_with_exit_130(self):
        source = self.tmp / "music"
        source.mkdir()
        # Long tones and one worker keep the batch busy well past the cancel,
        # so the round trip always lands mid-run.
        for i in range(6):
            make_tone(source / f"tone{i}.wav", frequency=440 + i, duration=10)
        argv = aconv_gui.build_argv(settings(source=str(source), format="mp3",
                                             dest=str(self.tmp / "out"), workers=1))

        runner = self.start(argv)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                event = runner.events.get(timeout=1.0)
            except queue.Empty:
                continue
            self.assertNotEqual(event.get("event"), "_exit",
                                f"the run finished before it could be cancelled: {event}")
            if event.get("event") == "file_start":
                break
        else:
            self.fail("no file_start event within 60s")

        self.assertTrue(runner.running)
        runner.cancel()
        events = self.drain(runner)

        kinds = [e.get("event") for e in events]
        self.assertIn("interrupted", kinds, f"no interrupted event before exit: {kinds}")
        self.assertEqual(events[-1]["event"], "_exit")
        self.assertEqual(events[-1]["returncode"], 130, events[-1].get("stderr_tail"))
        self.assertFalse(runner.running)

    def test_a_missing_source_reports_an_error_event_and_exit_1(self):
        argv = aconv_gui.build_argv(settings(source=str(self.tmp / "nope"),
                                             format="mp3"))

        runner = self.start(argv)
        events = self.drain(runner, timeout=60)

        self.assertEqual(events[0].get("event"), "hello", events[0])
        errors = [e for e in events if e.get("event") == "error"]
        self.assertTrue(errors, f"no error event: {events}")
        self.assertTrue(errors[0].get("message"), errors[0])
        self.assertEqual(events[-1]["event"], "_exit")
        self.assertEqual(events[-1]["returncode"], 1, events)


class AppSmokeTest(unittest.TestCase):
    def test_the_window_builds_and_destroys(self):
        # Tk needs a display, which headless CI runners do not have, so the
        # probe has to run inside the test rather than at import time.
        try:
            import tkinter
        except ImportError as exc:
            self.skipTest(f"tkinter is not installed: {exc}")
        try:
            probe = tkinter.Tk()
        except tkinter.TclError as exc:
            self.skipTest(f"no display available: {exc}")
        probe.destroy()

        try:
            app = aconv_gui.App()
        except TypeError:
            # The dictated API does not pin the constructor, so accept the
            # other conventional shape: a root passed in.
            app = aconv_gui.App(tkinter.Tk())

        if isinstance(app, tkinter.Misc):
            window = app
        else:
            window = next((getattr(app, name) for name in ("root", "master", "window")
                           if isinstance(getattr(app, name, None), tkinter.Misc)), None)
        self.assertIsNotNone(window, "App exposes no Tk window to drive")
        window.update_idletasks()
        window.destroy()


class AppCancelPathsTest(unittest.TestCase):
    """App-layer regressions around cancelling. These drive real widgets, so
    they need a display, probed per test-class the same way AppSmokeTest does.
    """

    def setUp(self):
        try:
            import tkinter
        except ImportError as exc:
            self.skipTest(f"tkinter is not installed: {exc}")
        self.tkinter = tkinter
        try:
            self.app = aconv_gui.App()
        except tkinter.TclError as exc:
            self.skipTest(f"no display available: {exc}")
        self.app.root.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        try:
            self.app.root.destroy()
        except self.tkinter.TclError:
            pass

    def test_close_survives_the_child_finishing_during_the_dialog(self):
        # askyesno pumps the event loop, so _poll can retire the runner while
        # the question is open; Yes must then simply close the window instead
        # of cancelling a run that no longer exists.
        class Finished:
            running = True

            def cancel(self):
                raise AssertionError("cancelled a run that already finished")

        self.app.runner = Finished()

        def yes_and_retire(*_args, **_kwargs):
            self.app.runner = None
            return True

        original = aconv_gui.messagebox.askyesno
        aconv_gui.messagebox.askyesno = yes_and_retire
        self.addCleanup(setattr, aconv_gui.messagebox, "askyesno", original)

        self.app._on_close()

        with self.assertRaises(self.tkinter.TclError):
            self.app.root.winfo_exists()

    def test_cancelled_preview_reports_preview_cancelled(self):
        self.app._reset_run_state(dry_run=True)
        self.app.cancel_requested = True

        self.app._finish_run({"event": "_exit", "returncode": 130, "stderr_tail": ""})

        self.assertEqual(self.app.status_label.cget("text"), "Preview cancelled.")

    def test_force_cancel_never_touches_a_newer_run(self):
        class Stub:
            def __init__(self):
                self.running = True
                self.terminated = False
                self.process = self

            def terminate(self):
                self.terminated = True

        old, current = Stub(), Stub()
        self.app.runner = current

        self.app._force_cancel(old)
        self.assertFalse(old.terminated, "a stale timer terminated the wrong run")

        self.app._force_cancel(current)
        self.assertTrue(current.terminated)


if __name__ == "__main__":
    unittest.main()
