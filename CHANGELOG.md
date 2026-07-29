# Changelog

## v0.2.0 (unreleased)

Fixes both known issues from v0.1.0.

- **Remuxing instead of re-encoding.** When a source file's audio is already a
  codec the target container stores natively (FLAC inside `.mka` converted to
  `.flac`, raw AAC converted to `.m4a`), the stream is now copied bit-for-bit
  instead of decoded and re-encoded. Lossy audio no longer loses a generation
  of quality on such conversions, and they finish in milliseconds. The codec
  map is deliberately conservative (only pairings every mainstream player
  accepts), any of `--bitrate`, `--quality` or `--sample-rate` forces a real
  encode, and a failed codec probe falls back to encoding rather than failing
  the file. The dry run reports which files would be remuxed.
- **`--on-existing move` is now atomic across filesystems** (a v0.1.0 known
  issue). Same-filesystem moves use an atomic rename; across filesystems the
  copy is staged under a temporary name in the destination directory, synced
  to disk, renamed into place, and only then is the original removed. An
  interrupt at any point leaves either the intact source or a complete
  destination, never neither.
- **Failures are summarized after the bar** (a v0.1.0 known issue). Per-file
  errors still print the moment they happen, but a partially failed run now
  ends with a grouped block listing every failed file with a one-line reason,
  instead of letting a dozen failures on a 500-file library scroll away with
  only a count surviving.
- **Machine-readable progress for wrappers.** `--progress jsonl` writes one
  JSON object per line to stdout and moves every human-readable print to
  stderr, so stdout stays parseable. The first event is always `hello` with
  `protocol: 1`, a number that only changes when the stream's shape changes
  incompatibly; per-file events report each start, finish and failure (with
  the captured ffmpeg stderr), and `done` closes every completed run. jsonl
  never prompts, since a prompt would deadlock the consumer, and never imports
  `tqdm`, so it runs from a bare checkout. `--stdin-control` adds a cancel
  channel: the line `cancel`, or EOF from a dead controller, stops the run
  exactly like Ctrl-C, exiting 130, including terminating the encode in
  flight, which no signal on the far side of a pipe would otherwise reach,
  and the same cleanup runs on SIGTERM. The default bar mode's output is
  byte-identical to before, and exit codes stay the ground truth throughout.
- **An optional Tk GUI.** `aconv_gui.py` runs straight from a source checkout
  (`python3 aconv_gui.py`, standard library only) and is a pure consumer of
  the jsonl protocol; the CLI stays the primary tool. It shows a copyable
  install command when ffmpeg is missing (and checks the Homebrew and
  `/usr/local` bins, since Finder-launched apps get a minimal PATH), previews
  dry runs, reports failures live with the raw ffmpeg output, weights the bar
  by audio length, mirrors the CLI's typed move confirmation, and offers a
  `--skip-existing` resume after a cancelled or failed run.
- **main() split into resolve_options() and execute()**, so the decision logic
  (the copy/move/skip choice, destination resolution, `--skip-existing`
  filtering) is testable in-process without a pty. Pure refactor; the CLI
  behaves identically.
- 119 tests across the CLI and GUI suites, up from 41.

## v0.1.0

First tagged release. The tool converts audio between formats with ffmpeg,
mirrors the source directory tree, and runs conversions in parallel.

Highlights since the initial commit:

- **Destination planning that cannot lose a file.** Files already in the target
  format that you choose to copy or move reserve their destination before any
  conversion is assigned one, so converting `song.wav` can no longer overwrite
  the `song.mp3` you asked to keep. Collision keys are case-insensitive, which
  matters on macOS and Windows, and the disambiguating suffix is re-checked
  rather than applied once.
- **Failures are visible.** A conversion that fails has its truncated output
  removed instead of being left to look like finished work, and a partially
  failed batch exits non-zero.
- **Ctrl-C stops the batch.** Queued work is dropped rather than drained, and
  the duration-measuring pass cancels too.
- **Cover art and tags survive.** Art is copied into containers that support it
  and dropped for the rest, which is what stops files with embedded art from
  failing outright on `.ogg`. Metadata is carried over explicitly, and mp3 gets
  id3v2.3 tags for player compatibility.
- **Scriptable.** `--dry-run`, `--skip-existing`, `--on-existing` and
  `--no-input` mean cron and CI never hit a prompt, and a fully specified
  command line no longer prompts on a terminal either.
- **Quality control.** `--bitrate`, `--quality` and `--sample-rate`, with the
  first two mutually exclusive because passing both let ffmpeg silently ignore
  one.
- **Progress weighted by audio length**, so a long podcast does not stall the bar.
- 40 tests, run on Linux and macOS across Python 3.9 to 3.13.

### Known issues

- `--on-existing move` is not atomic across filesystems. `shutil.move` falls
  back to copy-then-delete, so an interrupt mid-move can leave a file in
  neither place. Moves within one filesystem are safe.
- Per-file errors scroll past inside the progress output and only a count
  survives at the end, which is awkward on a large library.
