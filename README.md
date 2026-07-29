# Offline Audio File Format Converter

[![tests](https://github.com/arpit4/aconv/actions/workflows/tests.yml/badge.svg)](https://github.com/arpit4/aconv/actions/workflows/tests.yml)

A fast, offline command-line tool for converting audio files between formats
(e.g. `.m4a` to `.mp3`) using `ffmpeg`. It mirrors your folder structure, converts
files in parallel, and never lets one output quietly overwrite another. Works
interactively or unattended from cron and CI.

## Features

- **Interactive Mode**: Run the script with no arguments to be prompted for inputs.
- **Fast & Parallel**: Converts files concurrently (defaults to half your CPU cores to avoid oversubscribing ffmpeg, which is itself multi-threaded).
- **Quality Control**: Set bitrate, VBR quality, and sample rate via flags.
- **Smart Directory Handling**: Replicates the original folder structure in the destination.
- **Collision-Safe**: Files that would map to the same output name (e.g. `song.wav` and `song.flac` both mapping to `song.mp3`) are disambiguated instead of silently overwritten. Comparison is case-insensitive, so `SONG.wav` and `song.flac` are also kept apart on macOS and Windows.
- **Existing File Detection**: Automatically detects files that are already in the target format and offers to copy, move, or skip them without re-encoding. Copied and moved files reserve their destination first, so a conversion can never overwrite one.
- **Tags and Cover Art**: Metadata is carried over, and embedded cover art is kept whenever the target container supports it.
- **Lossless Remuxing**: A file whose audio is already a codec the target container stores natively (e.g. FLAC inside `.mka` going to `.flac`) has its stream copied bit-for-bit instead of re-encoded: no generation loss, and near-instant. Any quality flag forces a real encode.
- **Honest Exit Status**: Exits non-zero if any file failed, lists every failure with a one-line reason at the end of the run, and never leaves a truncated output file behind.
- **Format Filtering**: Optionally filter conversions to a specific source extension.
- **Scriptable**: `--dry-run`, `--skip-existing`, `--on-existing` and `--no-input` mean cron and CI never hit a prompt.
- **Clean Progress Bar**: Shows conversion progress via `tqdm`, weighted by audio length so a long podcast does not stall the bar.

## Requirements

1. **Python 3.7 or newer** (no third-party packages beyond `tqdm`). CI covers
   3.9 through 3.13 on Linux and macOS.
2. **ffmpeg**: Must be installed and accessible in your system's PATH.
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`
   - Windows: Install via `winget install ffmpeg` or download from [ffmpeg.org](https://ffmpeg.org).
   - `ffprobe`, which ships alongside ffmpeg, is used to weight the progress bar
     by audio length. Without it the bar simply counts files instead.
3. **Python Packages**:
   - Install dependencies using `pip install -r requirements.txt` (currently requires `tqdm`).

## Installation

1. Clone or download this repository.
2. Open a terminal in the project directory.
3. (Optional but recommended) Set up a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
4. Install requirements:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

The two positional arguments are the source and the target format:

```bash
python3 aconv.py <source> <format> [options]
```

The source is a directory or a single file. Anything you leave out is asked for,
so running with no arguments walks you through it:

```bash
python3 aconv.py
```

### Examples

**Convert a folder to mp3:**
```bash
python3 aconv.py /path/to/my_music mp3
```
*(Creates a new folder named `my_music_mp3` next to `my_music`, mirroring its
directory tree. Files already in mp3 are handled separately, see below.)*

**Convert a single file:**
```bash
python3 aconv.py /path/to/song.m4a flac
```
*(Writes `song_flac/song.flac`. A file you name directly is converted whatever
its extension, since ffmpeg reads more containers than a folder scan looks for.)*

**Choose the destination, or convert in place:**
```bash
python3 aconv.py /path/to/my_music wav --dest /path/to/destination_folder
python3 aconv.py /path/to/my_music wav --dest /path/to/my_music
```

**Only convert the `.m4a` files in a mixed folder:**
```bash
python3 aconv.py /path/to/my_music mp3 --ext m4a
```

**Convert to 320k CBR mp3 with 4 workers:**
```bash
python3 aconv.py /path/to/my_music mp3 --bitrate 320k --workers 4
```

**Convert to VBR mp3 (quality 2) at 44.1 kHz:**
```bash
python3 aconv.py /path/to/my_music mp3 --quality 2 --sample-rate 44100
```

**See the plan without writing anything:**
```bash
python3 aconv.py /path/to/my_music mp3 --dry-run
```

**Resume an interrupted run, leaving finished outputs alone:**
```bash
python3 aconv.py /path/to/my_music mp3 --skip-existing
```

### Files Already in the Target Format

Converting a folder to mp3 when some of it is already mp3 would mean re-encoding
lossy audio, so those files are set aside and you choose what happens to them:
`copy` them across, `move` them (which removes the originals), or `skip` them.

On a terminal you are asked. `--on-existing` answers up front instead:

```bash
python3 aconv.py /path/to/my_music mp3 --on-existing copy
```

An interactive `move` asks for a typed confirmation first. `--on-existing move`
does not, because passing the flag is itself the confirmation.

A move is atomic, including across filesystems: an interrupt mid-move leaves
either the intact original or a complete destination file, never neither.

### Unattended Runs

Nothing prompts when there is no terminal, so cron and CI runs never stall: the
tool converts every audio file it finds and skips anything already in the target
format. Pass `source` and `format` as arguments, and `--on-existing` if skipping
is not what you want.

On a terminal, a fully specified command line also runs as given. The optional
extension filter is only offered when you are already being prompted for the
source and format. To rule prompts out entirely, even on a terminal:

```bash
python3 aconv.py /path/to/my_music mp3 --no-input
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--dest` | Destination directory | `<source>_<format>` next to the source |
| `--ext` | Only convert this source extension (e.g. `m4a`) | all audio files |
| `--workers` | Number of parallel conversions | half the CPU cores |
| `--bitrate` | Target audio bitrate / CBR (e.g. `320k`). Cannot be combined with `--quality` | ffmpeg default |
| `--quality` | VBR quality (ffmpeg `-q:a`, e.g. `2` for mp3). Cannot be combined with `--bitrate` | ffmpeg default |
| `--sample-rate` | Target sample rate in Hz (e.g. `44100`) | source rate |
| `--on-existing` | `copy`, `move` or `skip` files already in the target format, without prompting | prompt on a terminal, otherwise `skip` |
| `--skip-existing` | Leave outputs that already exist alone instead of re-encoding them | re-encode everything |
| `--dry-run` | Print the plan and exit without writing anything | off |
| `--no-input` | Never prompt; use the defaults and fail if a required value is missing | off |

### Exit Status

`0` when every file converted, `1` on a usage error or if any file failed to convert,
`2` on invalid arguments, `130` if interrupted with Ctrl-C.

Ctrl-C drops everything still queued and waits only for the conversions already
running, so it stops in about the time one file takes rather than finishing the
batch. Any half-written output is removed, so a later `--skip-existing` run
resumes from the last file that actually completed.

## How It Works

1. Scans the source for audio files, skipping the destination if it sits inside
   the source tree. A single named file is taken as-is.
2. Sets aside anything already in the target format, to be copied, moved or
   skipped rather than re-encoded.
3. Maps every remaining file to a destination path, mirroring the source tree.
   Two files that would land on the same name are disambiguated, and the copied
   or moved files claim their paths first so a conversion cannot overwrite one.
4. Probes each file's audio codec with `ffprobe`. A file whose audio the target
   container stores natively is remuxed (the stream copied bit-for-bit) instead
   of re-encoded, unless a quality flag asks for a transformation. A failed probe
   just means a normal encode.
5. Measures the audio length of each file with `ffprobe`, to weight the progress
   bar. If any file cannot be measured, the bar counts files instead.
6. Runs one `ffmpeg` process per file, several at a time. A conversion that fails
   has its half-written output removed and listed in the summary at the end, and
   the run finishes with a non-zero exit status.

## Notes

- **Codec is chosen by ffmpeg from the target extension.** The container's default
  codec applies, and these defaults can vary between ffmpeg versions. For example,
  recent ffmpeg defaults the `.ogg` container to the **FLAC** codec (lossless), in
  which case `--bitrate` has no effect. Prefer an extension that maps unambiguously
  to your intended codec (e.g. `.mp3`, `.opus`) when bitrate matters.
- **Which files a directory scan picks up.** Audio-only containers (`.mp3`, `.m4a`,
  `.flac`, `.wav`, `.ogg`, `.opus`, `.aiff`, `.mka`, `.wv`, `.ape` and friends), so
  that scanning a folder of home videos does not quietly rip their soundtracks. A
  file named directly on the command line is converted whatever its extension, since
  ffmpeg reads far more containers than that list covers.
- **A destination inside the source tree is excluded from the scan**, so re-running
  the same command does not start converting its own output.
- **Cover art.** Art is copied as-is into `.mp3`, `.flac`, `.m4a`, `.m4b` and `.mp4`
  outputs. For any other target it is dropped, because ffmpeg would otherwise try to
  re-encode the picture with the container's default video codec and fail (converting
  a file with embedded art to `.ogg` fails outright without this).

## Tests

```bash
python3 -m unittest test_aconv -v
```

Fixtures are generated with ffmpeg's `lavfi` source, so no binary test files are
needed. Tests that shell out to ffmpeg are skipped when it is not installed.

## Changelog

See [CHANGELOG.md](CHANGELOG.md), including the known issues for the current
release.

## License

MIT, see [LICENSE](LICENSE).
