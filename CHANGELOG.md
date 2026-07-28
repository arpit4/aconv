# Changelog

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
