import _thread
import argparse
import contextlib
import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

__version__ = "0.1.0"

# Extensions picked up when scanning a directory. Audio-only containers, so that
# scanning a folder of home videos does not quietly rip their soundtracks; a file
# named directly on the command line is converted whatever its extension.
# ('.alac' is deliberately absent: ALAC is a codec that lives inside .m4a.)
AUDIO_EXTENSIONS = {
    '.mp3', '.mp2', '.m4a', '.m4b', '.aac', '.wav', '.flac', '.ogg', '.oga',
    '.opus', '.wma', '.aiff', '.aif', '.aifc', '.mka', '.caf', '.wv', '.ape',
    '.amr', '.dsf',
}

# Containers that can carry an embedded cover image. For every other target the
# attached picture has to be dropped, because ffmpeg otherwise tries to re-encode
# it with the container's default video codec and fails outright (for example
# .ogg defaults to theora, which is usually not built in).
ART_CAPABLE_FORMATS = {'mp3', 'flac', 'm4a', 'm4b', 'mp4'}

# Codecs each target container stores natively, so a matching source stream can
# be remuxed with -c:a copy instead of decoded and re-encoded. A lossy codec
# loses quality on every re-encode. Deliberately conservative: only pairings
# every mainstream player accepts. mp3-in-mp4 is legal but widely unsupported,
# and Ogg can carry FLAC and Opus but most software expects those under
# .flac/.opus, so all of them get a normal encode instead.
REMUX_CODECS = {
    'mp3': {'mp3'},
    'flac': {'flac'},
    'opus': {'opus'},
    'm4a': {'aac', 'alac'},
    'm4b': {'aac', 'alac'},
    'ogg': {'vorbis'},
    # WAV is little-endian only; the big-endian and companded pcm variants
    # would have to be converted anyway.
    'wav': {'pcm_u8', 'pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'pcm_f32le', 'pcm_f64le'},
}

# Verb and present participle for the three ways of handling files that are
# already in the target format, keyed by the first letter of the choice.
ON_EXISTING_WORDS = {'c': ('copy', 'copying'), 'm': ('move', 'moving'), 's': ('skip', 'skipping')}

# Above this many files, show progress while measuring durations. Below it the
# measuring pass is over before a bar would finish drawing.
MEASURE_PROGRESS_THRESHOLD = 50

def load_tqdm():
    """Import tqdm at the moment a bar is drawn.

    --progress jsonl and --progress none have to run from a bare checkout with
    nothing installed beyond the stdlib; only the bar needs tqdm.
    """
    try:
        from tqdm import tqdm
    except ImportError:
        print("tqdm not installed. Please run: pip install -r requirements.txt")
        sys.exit(1)
    return tqdm

def make_emitter():
    """The jsonl channel: one flushed JSON object per line on the real stdout.

    sys.stdout is re-pointed at stderr first, so every human print() in the
    program lands on stderr and stdout stays machine-parseable. Worker threads
    emit concurrently, so each line is serialised, written and flushed under
    one lock or the line protocol would interleave.
    """
    stream, sys.stdout = sys.stdout, sys.stderr
    lock = threading.Lock()

    def emit(event, **fields):
        line = json.dumps(dict(event=event, **fields))
        with lock:
            stream.write(line + "\n")
            stream.flush()

    return emit

# The ffmpeg processes currently running, so a cancel request can reach them.
# A controller on the far side of a pipe has no terminal: its ffmpeg
# grandchildren never see a SIGINT, and _thread.interrupt_main() alone is not
# delivered while the main thread waits untimed on a future. Without the
# sweep, a single long encode would run to completion after the cancel.
_cancel_event = threading.Event()
_live_processes = set()
_live_lock = threading.Lock()

def cancel_active_conversions():
    """Terminate the ffmpeg processes in flight and refuse to start new ones."""
    _cancel_event.set()
    with _live_lock:
        processes = list(_live_processes)
    for process in processes:
        process.terminate()

def start_stdin_control():
    """Watch stdin for a cancel request from the controlling process.

    The line "cancel", or EOF, which is what a dead controller looks like,
    terminates the conversions in flight and funnels into the same
    KeyboardInterrupt/exit-130 path as Ctrl-C. SIGTERM gets the same
    treatment, so a controller that gives up on the channel and terminates
    the process outright still leaves no orphaned ffmpeg behind.
    """
    def watch():
        while True:
            line = sys.stdin.readline()
            if not line or line.strip() == 'cancel':
                break
        cancel_active_conversions()
        _thread.interrupt_main()

    def on_sigterm(*_):
        cancel_active_conversions()
        raise KeyboardInterrupt

    # signal.signal is refused outside the main thread; embedders and the
    # test suite may call resolve_options from elsewhere, and they can live
    # without the SIGTERM courtesy.
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGTERM, on_sigterm)
        # A shell starts background jobs with SIGINT ignored, and
        # interrupt_main() is a silent no-op while SIGINT is SIG_IGN or
        # SIG_DFL. The cancel channel was explicitly requested, so restore
        # the raising handler it depends on.
        signal.signal(signal.SIGINT, signal.default_int_handler)
    threading.Thread(target=watch, daemon=True).start()

def prompt(message, default=None):
    """input() that returns `default` instead of raising when stdin hits EOF."""
    try:
        return input(message).strip()
    except EOFError:
        print()
        return default

def prompt_required(message, retry_message):
    """Prompt until a non-empty answer is given; exit if stdin closes first."""
    while True:
        value = prompt(message)
        if value is None:
            print("Error: no input available.")
            sys.exit(1)
        if value:
            return value
        message = retry_message

def check_ffmpeg(emit=None):
    try:
        subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, check=True, timeout=15)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        if emit:
            emit('error', message="ffmpeg is not installed or not found in PATH.")
        print("Error: ffmpeg is not installed or not found in PATH.")
        print("Please install it (e.g., 'brew install ffmpeg' on macOS) and try again.")
        sys.exit(1)

def is_within(path, directory):
    """True if `path` is `directory` or sits underneath it."""
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False

def find_audio_files(source_dir, ext_filter=None, exclude_dir=None):
    audio_files = []
    source_path = Path(source_dir)

    if ext_filter:
        ext_filter = ext_filter.lower()
        if not ext_filter.startswith('.'):
            ext_filter = '.' + ext_filter

    if source_path.is_file():
        # A file named explicitly on the command line is converted whatever its
        # extension: ffmpeg reads many more containers than AUDIO_EXTENSIONS
        # lists, and refusing a file the user pointed at is just obstructive.
        if ext_filter and source_path.suffix.lower() != ext_filter:
            return []
        return [source_path]

    for path in source_path.rglob('*'):
        # Never pick up our own output, so that a destination inside the source
        # tree does not get re-converted on the next run.
        if exclude_dir is not None and is_within(path, exclude_dir):
            continue
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            if ext_filter and path.suffix.lower() != ext_filter:
                continue
            audio_files.append(path)
    return audio_files

def probe_duration(path):
    """Length of `path` in seconds, or None if ffprobe cannot determine it."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        duration = float(result.stdout.decode('utf-8', errors='replace').strip())
    except ValueError:
        return None
    return duration if duration > 0 else None

def probe_audio_codecs(path):
    """Codec of every audio stream in `path`, or None if ffprobe cannot say."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a',
             '-show_entries', 'stream=codec_name', '-of', 'default=nw=1:nk=1', str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    codecs = tuple(result.stdout.decode('utf-8', errors='replace').split())
    return codecs or None

def should_stream_copy(source_file, target_format):
    """True when the audio in `source_file` already fits the target container.

    Every audio stream has to match, not just the first: ffmpeg chooses which
    stream to keep by its own heuristics, so a mixed-codec source could have
    the wrong one copied into a container that cannot hold it. A failed probe
    means a normal encode, slower but always safe, never a failed file.
    """
    codecs = REMUX_CODECS.get(target_format)
    if not codecs:
        return False
    found = probe_audio_codecs(source_file)
    return found is not None and all(codec in codecs for codec in found)

def plan_remuxes(convert_plan, target_format, workers, emit=None):
    """The sources in `convert_plan` that can be remuxed instead of re-encoded.

    Empty when ffprobe is missing or the target has no codec map: without a
    trustworthy answer every file gets the normal encode.
    """
    if target_format not in REMUX_CODECS or not shutil.which('ffprobe'):
        return set()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        tasks = {executor.submit(should_stream_copy, src, target_format): src
                 for src, _ in convert_plan}
        try:
            return {src for task, src in tasks.items() if task.result()}
        except KeyboardInterrupt:
            # Same reason as run_conversions: the queue would otherwise drain.
            dropped = sum(1 for task in tasks if task.cancel())
            if emit:
                emit('interrupted', dropped=dropped)
            raise

def measure_durations(files, workers, use_bar=True, emit=None):
    """Per-file durations, or None if any file cannot be measured.

    Used to weight the progress bar: counting files makes a 3 minute track and
    a 90 minute podcast advance it equally. All or nothing, because a partial
    set of weights would be more misleading than plain file counts.
    """
    if not shutil.which('ffprobe'):
        return None
    with ThreadPoolExecutor(max_workers=workers) as executor:
        tasks = [executor.submit(probe_duration, f) for f in files]
        try:
            todo = tasks
            if use_bar:
                # One ffprobe per file is quick, but on a large library the
                # wait is long enough to look like a hang, so show it for
                # anything sizeable.
                todo = load_tqdm()(tasks, desc="Measuring", unit="file", leave=False,
                                   disable=len(files) < MEASURE_PROGRESS_THRESHOLD)
            durations = []
            for task in todo:
                durations.append(task.result())
                if emit:
                    emit('probe', done=len(durations), total=len(tasks))
        except KeyboardInterrupt:
            # Same reason as run_conversions: the queue would otherwise drain.
            dropped = sum(1 for task in tasks if task.cancel())
            if emit:
                emit('interrupted', dropped=dropped)
            raise
    if any(d is None for d in durations):
        return None
    return durations

def convert_file(source_file, dest_file, extra_args=None):
    """Convert one file. Returns None on success, ffmpeg's stderr on failure."""
    if _cancel_event.is_set():
        # A worker that picked its file up after a cancel request must not
        # start a fresh encode while the run is unwinding.
        return "cancelled before it started"
    # Ensure destination directory exists
    dest_file.parent.mkdir(parents=True, exist_ok=True)

    # Run ffmpeg
    # -y overwrites without asking
    # -v error suppresses standard output except errors
    # -nostdin keeps parallel ffmpeg processes from competing for the terminal
    cmd = ['ffmpeg', '-y', '-v', 'error', '-nostdin', '-i', str(source_file)]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(dest_file))
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with _live_lock:
        _live_processes.add(process)
        # The cancel sweep may have run between the check above and this
        # registration; decide under the lock the sweep holds, act outside it.
        already_cancelled = _cancel_event.is_set()
    if already_cancelled:
        process.terminate()
    try:
        _, stderr = process.communicate()
    finally:
        with _live_lock:
            _live_processes.discard(process)
    if process.returncode == 0:
        return None
    # A failed or interrupted ffmpeg still leaves an empty or truncated file
    # behind. Remove it, otherwise the next run sees a file that is already
    # in the target format and treats the broken output as finished work.
    try:
        dest_file.unlink()
    except OSError:
        # Nothing was written, or it is not ours to delete.
        pass
    if _cancel_event.is_set():
        return "cancelled"
    return stderr.decode('utf-8', errors='replace').strip()

def move_file(source_file, dest_file):
    """Move `source_file` onto `dest_file` without a moment where neither exists.

    shutil.move degrades to copy-then-delete across filesystems, so an
    interrupt mid-move can lose the file: already gone from the source, not
    yet whole at the destination. Same filesystem gets the atomic rename;
    across filesystems the copy is staged under a temporary name, synced to
    disk, renamed into place, and only then is the source removed. Every
    interruption leaves either the intact source or a complete destination.
    """
    try:
        os.replace(source_file, dest_file)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    # Staged in the destination directory, so the final rename cannot itself
    # cross a filesystem boundary.
    fd, tmp_name = tempfile.mkstemp(dir=dest_file.parent,
                                    prefix=f".{dest_file.name}.", suffix=".partial")
    try:
        with open(source_file, 'rb') as src, open(fd, 'wb') as tmp:
            shutil.copyfileobj(src, tmp)
            tmp.flush()
            # Force the copy to disk before it can take the final name: a
            # rename can survive a crash that the data it points to did not.
            os.fsync(tmp.fileno())
        shutil.copystat(source_file, tmp_name)
        os.replace(tmp_name, dest_file)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    os.unlink(source_file)

def build_ffmpeg_args(args, target_format, stream_copy=False):
    """Translate CLI options into ffmpeg arguments."""
    extra = []
    # Keep embedded cover art where the target container supports it, and drop
    # it everywhere else so that art does not turn into a conversion failure.
    if target_format in ART_CAPABLE_FORMATS:
        extra.extend(['-c:v', 'copy'])
    else:
        extra.append('-vn')
    if stream_copy:
        extra.extend(['-c:a', 'copy'])
    extra.extend(['-map_metadata', '0'])
    if target_format == 'mp3':
        # id3v2.3 is far more widely readable than ffmpeg's id3v2.4 default.
        extra.extend(['-id3v2_version', '3'])
    if args.bitrate:
        extra.extend(['-b:a', args.bitrate])
    if args.quality is not None:
        extra.extend(['-q:a', str(args.quality)])
    if args.sample_rate:
        extra.extend(['-ar', str(args.sample_rate)])
    return extra

def dest_key(path):
    """Key used to compare destinations.

    Casefolded, because the filesystems this tool runs on are usually
    case-insensitive: SONG.wav and song.flac must not both claim song.mp3.
    """
    return str(path).casefold()

def reserve_dest(dest_file, used_dests, hint=''):
    """Claim `dest_file`, adding a suffix while the path is already claimed."""
    candidate = dest_file
    attempt = 0
    while dest_key(candidate) in used_dests:
        attempt += 1
        if hint and attempt == 1:
            marker = hint
        elif hint:
            marker = f"{hint}_{attempt}"
        else:
            marker = str(attempt)
        candidate = dest_file.with_name(f"{dest_file.stem}_{marker}{dest_file.suffix}")
    used_dests.add(dest_key(candidate))
    return candidate

def relative_dest(source_path, audio_file):
    """Path of `audio_file` relative to the source root."""
    if source_path.is_file():
        return Path(audio_file.name)
    return audio_file.relative_to(source_path)

def plan_destinations(source_path, dest_dir, keep_files, convert_files, target_format):
    """Map every source file to a destination, disambiguating collisions.

    `keep_files` are the files already in the target format that will be copied
    or moved verbatim. They reserve their destinations first, so that a
    conversion output can never silently overwrite one of them.

    Returns (keep_plan, convert_plan) as lists of (source, destination) pairs.
    """
    used_dests = set()

    keep_plan = []
    for audio_file in keep_files:
        dest_file = dest_dir / relative_dest(source_path, audio_file)
        keep_plan.append((audio_file, reserve_dest(dest_file, used_dests)))

    convert_plan = []
    for audio_file in convert_files:
        rel_path = relative_dest(source_path, audio_file).with_suffix(f".{target_format}")
        # On a collision, prefer the original extension as the marker so that
        # song.wav and song.flac become song.mp3 and song_flac.mp3.
        hint = audio_file.suffix.lstrip('.').lower()
        convert_plan.append((audio_file, reserve_dest(dest_dir / rel_path, used_dests, hint)))

    return keep_plan, convert_plan

def failure_reason(err):
    """Reduce ffmpeg's captured stderr to a single summary line.

    -v error usually leaves a line or two, worth keeping whole, but a corrupt
    container can produce a complaint per packet; the summary has to stay one
    line per file or a big batch scrolls just like the live errors it exists
    to outlast.
    """
    lines = [line.strip() for line in err.splitlines() if line.strip()]
    if not lines:
        return "ffmpeg reported no error output"
    joined = "; ".join(lines)
    return joined if len(joined) <= 160 else lines[0]

def run_conversions(convert_plan, extra_args, workers, weights=None, bar_args=None,
                    remux_sources=frozenset(), remux_args=None, use_bar=True, emit=None):
    """Convert every (source, destination) pair.

    Sources in `remux_sources` run with `remux_args` instead of `extra_args`,
    so a remux and an encode can share one batch, one bar and one summary.

    Returns (success_count, failures), the failures as (source, stderr) pairs.
    Each failure is also tqdm.write()n the moment it happens: on a long batch
    the returned list is hours away, and trouble should be visible live.

    `emit` receives the per-file protocol events; they fire on the worker
    thread, so a consumer sees each file the moment its ffmpeg starts rather
    than when the main loop collects the result.

    Ctrl-C drops whatever is still queued. Without that, an interrupted run keeps
    going: ThreadPoolExecutor.shutdown(wait=True) drains the queue rather than
    discarding it, so worker threads carry on pulling files and each one starts a
    fresh ffmpeg, after the terminal's signal has already been and gone. The
    progress bar is down by then, so the batch continues out of sight.

    Conversions already running are stopped or left to finish, bounded by the
    worker count: a terminal's Ctrl-C reaches their ffmpeg processes through
    the process group, and a --stdin-control cancel terminates them explicitly,
    since no signal crosses a pipe. Either way convert_file() clears up any
    partial output.
    """
    if bar_args is None:
        bar_args = dict(total=len(convert_plan), unit='file')

    tqdm = load_tqdm() if use_bar else None

    def convert_one(source_file, dest_file):
        if emit:
            emit('file_start', source=str(source_file), dest=str(dest_file),
                 action='remux' if source_file in remux_sources else 'convert')
        error = convert_file(source_file, dest_file,
                             remux_args if source_file in remux_sources else extra_args)
        if emit:
            if error is None:
                emit('file_done', source=str(source_file), dest=str(dest_file),
                     seconds=weights[source_file] if weights else None)
            else:
                emit('file_failed', source=str(source_file), dest=str(dest_file),
                     error=error)
        return error

    success_count = 0
    failures = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        tasks = {executor.submit(convert_one, src, dst): src for src, dst in convert_plan}
        try:
            with (tqdm(desc="Converting", **bar_args) if use_bar
                  else contextlib.nullcontext()) as pbar:
                for future in as_completed(tasks):
                    error = future.result()
                    if error is None:
                        success_count += 1
                    else:
                        source = tasks[future]
                        # Print error without breaking progress bar
                        (tqdm.write if use_bar else print)(
                            f"Failed to convert {source}: {error}")
                        failures.append((source, error))
                    if pbar is not None:
                        pbar.update(weights[tasks[future]] if weights else 1)
        except KeyboardInterrupt:
            dropped = sum(1 for future in tasks if future.cancel())
            if emit:
                emit('interrupted', dropped=dropped)
            print(f"\nInterrupted. Dropped {dropped} queued file(s), "
                  "waiting for the conversion(s) already running to stop.")
            raise
    return success_count, failures

def resolve_options(argv=None):
    """Resolve the command line into a run plan, prompting where needed.

    Everything up to the first filesystem write is decided here: source
    files, destination, ffmpeg arguments, and what happens to files already
    in the target format. That keeps the decision logic testable without a
    terminal. Returns a namespace with what execute() needs; exits on
    validation failures exactly as the CLI reports them.
    """
    parser = argparse.ArgumentParser(description="Offline Audio Format Converter")
    parser.add_argument("--version", action="version", version=f"aconv {__version__}")
    parser.add_argument("source", nargs='?', help="Source directory or file")
    parser.add_argument("format", nargs='?', help="Target audio format (e.g., mp3, wav, flac)")
    parser.add_argument("--dest", help="Destination directory (optional)", default=None)
    parser.add_argument("--ext", help="Specific source extension to filter by (optional)", default=None)
    parser.add_argument("--workers", help="Number of parallel conversions", type=int,
                        default=max(1, (os.cpu_count() or 4) // 2))
    # -b:a and -q:a select CBR and VBR respectively. Passing both leaves ffmpeg
    # to pick one per codec and silently ignore the other, so refuse it up front.
    quality_opts = parser.add_mutually_exclusive_group()
    quality_opts.add_argument("--bitrate", help="Target audio bitrate, e.g. 320k (CBR)", default=None)
    quality_opts.add_argument("--quality", help="VBR quality for the codec (ffmpeg -q:a), e.g. 2 for mp3",
                              type=int, default=None)
    parser.add_argument("--sample-rate", dest="sample_rate", type=int, default=None,
                        help="Target sample rate in Hz, e.g. 44100")
    parser.add_argument("--on-existing", dest="on_existing", choices=['copy', 'move', 'skip'],
                        default=None,
                        help="What to do with files already in the target format, "
                             "without prompting. 'move' removes the originals")
    parser.add_argument("--skip-existing", dest="skip_existing", action="store_true",
                        help="Leave outputs that already exist alone instead of re-encoding them")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="Print what would happen and exit without writing anything")
    parser.add_argument("--no-input", dest="no_input", action="store_true",
                        help="Never prompt; use the defaults and fail if a required value is missing")
    parser.add_argument("--progress", choices=['bar', 'jsonl', 'none'], default='bar',
                        help="How to report progress: a live bar, one JSON object per "
                             "line on stdout for machine consumers, or nothing")
    parser.add_argument("--stdin-control", dest="stdin_control", action="store_true",
                        help="Read control lines from stdin; 'cancel' or EOF stops the run "
                             "(requires --progress jsonl)")

    args = parser.parse_args(argv)

    if args.stdin_control and args.progress != 'jsonl':
        parser.error("--stdin-control requires --progress jsonl")

    emit = make_emitter() if args.progress == 'jsonl' else None
    if emit:
        emit('hello', protocol=1, version=__version__)
    if args.stdin_control:
        start_stdin_control()

    if args.workers < 1:
        if emit:
            emit('error', message="--workers must be a positive integer.")
        print("Error: --workers must be a positive integer.")
        sys.exit(1)

    # Whether the run started fully specified. The optional extension filter is
    # only worth asking about when the user is already being guided through the
    # required values; a complete command line should just run.
    guided = not (args.source and args.format)

    # jsonl is a machine protocol: a prompt would deadlock the consumer, so it
    # always behaves like today's non-TTY path.
    interactive = sys.stdin.isatty() and not args.no_input and args.progress != 'jsonl'

    if not args.source:
        if not interactive:
            if emit:
                emit('error', message="source is required (no input available to prompt for it).")
            print("Error: source is required (no input available to prompt for it).")
            sys.exit(1)
        args.source = prompt_required(
            "Enter the source directory or file path: ",
            "Source path cannot be empty. Enter source directory or file path: ")

    if not args.format:
        if not interactive:
            if emit:
                emit('error', message="target format is required (no input available to prompt for it).")
            print("Error: target format is required (no input available to prompt for it).")
            sys.exit(1)
        args.format = prompt_required(
            "Enter the target audio format (e.g., mp3, wav, flac): ",
            "Target format cannot be empty. Enter the target audio format: ")

    source_path = Path(args.source).resolve()

    # Validate early, before any further prompts, so the user isn't asked
    # questions only to be told the source is missing or ffmpeg is absent.
    if not source_path.exists():
        if emit:
            emit('error', message=f"Source '{args.source}' does not exist.")
        print(f"Error: Source '{args.source}' does not exist.")
        sys.exit(1)

    check_ffmpeg(emit)

    # Only offer the extension filter when the user is already being walked
    # through the required values. A fully specified command line runs as given.
    if not args.ext and source_path.is_dir() and interactive and guided:
        ext_input = prompt("Enter specific source extension to convert (e.g. m4a), or press Enter to convert all audio files: ")
        if ext_input:
            args.ext = ext_input

    target_format = args.format.lower().lstrip('.')
    extra_args = build_ffmpeg_args(args, target_format)

    if args.dest:
        dest_dir = Path(args.dest).resolve()
    else:
        # Default destination: source_path_targetFormat
        if source_path.is_file():
            dest_dir = source_path.parent / f"{source_path.stem}_{target_format}"
        else:
            dest_dir = source_path.parent / f"{source_path.name}_{target_format}"

    # Converting in place, with --dest pointing at the source itself, is a
    # legitimate thing to ask for, so only a destination strictly inside the
    # tree is excluded from the scan.
    exclude_dir = dest_dir if dest_dir != source_path else None
    audio_files = find_audio_files(source_path, args.ext, exclude_dir=exclude_dir)

    if not audio_files:
        if emit:
            emit('scan', found=0, to_convert=0, already_in_format=0)
            emit('done', converted=0, failed=0)
        print(f"No audio files found in '{args.source}'.")
        sys.exit(0)

    target_ext = f".{target_format}"
    to_convert = []
    already_in_format = []

    for f in audio_files:
        if f.suffix.lower() == target_ext:
            already_in_format.append(f)
        else:
            to_convert.append(f)

    if emit:
        emit('scan', found=len(audio_files), to_convert=len(to_convert),
             already_in_format=len(already_in_format))

    choice = 's'
    if already_in_format:
        print(f"\nFound {len(already_in_format)} file(s) already in '{target_format}' format:")
        for f in already_in_format[:5]:
            print(f"  - {f.name}")
        if len(already_in_format) > 5:
            print(f"  ... and {len(already_in_format) - 5} more.")

        if args.on_existing:
            # An explicit flag is the decision, including for move: asking a
            # script to confirm what it just asked for would only deadlock it.
            choice = args.on_existing[0]
            print(f"--on-existing {args.on_existing}: {ON_EXISTING_WORDS[choice][1]} these files.")
        elif not interactive:
            # No TTY: default to the non-destructive choice rather than crash.
            print("Non-interactive run: skipping files already in the target format.")
        else:
            while True:
                answer = prompt(f"\nWould you like to [c]opy, [m]ove, or [s]kip these files to the destination? ", 's').lower()
                if answer in ['c', 'm', 's', 'copy', 'move', 'skip']:
                    choice = answer
                    break

            if choice.startswith('m') and not args.dry_run:
                # Moving deletes the originals; require explicit confirmation.
                # Not under --dry-run, where nothing is going to move.
                confirm = prompt(f"This will MOVE (remove) {len(already_in_format)} original file(s). Type 'yes' to proceed: ", '').lower()
                if confirm != 'yes':
                    print("Move cancelled; skipping these files.")
                    choice = 's'

    # Reserve destinations for the copied or moved files before planning the
    # conversions, so that converting song.wav cannot overwrite a song.mp3 that
    # the user asked to keep.
    keep_files = already_in_format if choice.startswith(('c', 'm')) else []
    keep_plan, convert_plan = plan_destinations(
        source_path, dest_dir, keep_files, to_convert, target_format)

    if args.skip_existing:
        # Applies to the copied and moved files too, so that resuming an
        # interrupted run does not re-copy, or move an original on top of a
        # destination that already holds it.
        planned = len(keep_plan) + len(convert_plan)
        keep_plan = [(src, dst) for src, dst in keep_plan if not dst.exists()]
        convert_plan = [(src, dst) for src, dst in convert_plan if not dst.exists()]
        skipped = planned - len(keep_plan) - len(convert_plan)
        if skipped:
            print(f"--skip-existing: leaving {skipped} existing output file(s) alone.")

    # Any quality flag is a request to transform the audio, which -c:a copy
    # would silently ignore; only an unqualified conversion may stream-copy.
    if args.bitrate or args.quality is not None or args.sample_rate:
        remux_sources = set()
    else:
        remux_sources = plan_remuxes(convert_plan, target_format, args.workers, emit)

    if emit:
        # convert and remux are disjoint counts, matching the per-file action
        # in plan_item and file_start: together they sum to the whole batch.
        emit('plan', keep=len(keep_plan), convert=len(convert_plan) - len(remux_sources),
             remux=len(remux_sources), dest=str(dest_dir),
             on_existing=ON_EXISTING_WORDS[choice[0]][0])

    return argparse.Namespace(
        dest_dir=dest_dir, target_format=target_format, extra_args=extra_args,
        remux_sources=remux_sources,
        remux_args=build_ffmpeg_args(args, target_format, stream_copy=True),
        choice=choice, keep_plan=keep_plan, convert_plan=convert_plan,
        dry_run=args.dry_run, workers=args.workers, bitrate=args.bitrate,
        quality=args.quality, sample_rate=args.sample_rate,
        progress=args.progress, emit=emit)

def execute(options):
    """Carry out a plan from resolve_options(): the dry-run report, the copy
    or move pass, and the conversion run."""
    emit = options.emit
    use_bar = options.progress == 'bar'
    if options.dry_run:
        verb, participle = ON_EXISTING_WORDS[options.choice[0]]
        print(f"\nDry run. Nothing will be written to {options.dest_dir}.")
        for source_file, dest_file in options.keep_plan:
            if emit:
                emit('plan_item', action=verb, source=str(source_file), dest=str(dest_file))
            print(f"  {participle} {source_file} -> {dest_file}")
        for source_file, dest_file in options.convert_plan:
            action = "remux" if source_file in options.remux_sources else "convert"
            if emit:
                emit('plan_item', action=action, source=str(source_file), dest=str(dest_file))
            print(f"  {action}ing {source_file} -> {dest_file}")
        print(f"  ffmpeg options: {' '.join(options.extra_args)}")
        if options.remux_sources:
            print(f"  ffmpeg options (remuxed files): {' '.join(options.remux_args)}")
        print(f"\n{len(options.keep_plan)} file(s) to {verb}, {len(options.convert_plan)} to convert.")
        if emit:
            emit('done', converted=0, failed=0)
        sys.exit(0)

    if options.keep_plan:
        action_name = "Copying" if options.choice.startswith('c') else "Moving"
        print(f"{action_name} {len(options.keep_plan)} files to {options.dest_dir}...")
        for source_file, dest_file in options.keep_plan:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            if options.choice.startswith('c'):
                shutil.copy2(source_file, dest_file)
            else:
                move_file(source_file, dest_file)
        print("Done.")
        if emit:
            emit('keep_done', count=len(options.keep_plan))

    if not options.convert_plan:
        if emit:
            emit('done', converted=0, failed=0)
        print("\nNo files left to convert.")
        sys.exit(0)

    print(f"Found {len(options.convert_plan)} audio files. Converting to .{options.target_format}...")
    print(f"Destination: {options.dest_dir}")
    if options.remux_sources:
        print(f"{len(options.remux_sources)} file(s) already hold audio the "
              f".{options.target_format} container stores natively; "
              "remuxing without re-encoding.")
    if options.bitrate or options.quality is not None or options.sample_rate:
        print(f"ffmpeg options: {' '.join(options.extra_args)}")

    # Weight the bar by audio length where possible, so that one long podcast
    # among short tracks does not make the bar stall near the end.
    durations = measure_durations([src for src, _ in options.convert_plan], options.workers,
                                  use_bar=use_bar, emit=emit)
    if durations:
        weights = {src: seconds for (src, _), seconds in zip(options.convert_plan, durations)}
        bar_args = dict(total=sum(durations), unit='s', unit_scale=True)
    else:
        weights = None
        bar_args = dict(total=len(options.convert_plan), unit='file')

    success_count, failures = run_conversions(options.convert_plan, options.extra_args,
                                              options.workers, weights, bar_args,
                                              options.remux_sources, options.remux_args,
                                              use_bar=use_bar, emit=emit)

    if emit:
        emit('done', converted=success_count, failed=len(failures))
    print(f"\nConversion complete! {success_count}/{len(options.convert_plan)} "
          "files successfully converted.")
    if failures:
        # The live error messages have scrolled off with the batch, on a big
        # library hours ago, so repeat the failures as one block at the end,
        # sorted to read as a checklist rather than replaying completion order.
        print(f"\n{len(failures)} file(s) failed to convert:")
        for source_file, error in sorted(failures):
            print(f"  {source_file}: {failure_reason(error)}")
        # Exit non-zero so scripts and CI notice a partially failed batch.
        sys.exit(1)

def main():
    execute(resolve_options())

if __name__ == "__main__":
    # Ctrl-Break on Windows would otherwise kill the process outright; route
    # it into the same KeyboardInterrupt/exit-130 path as Ctrl-C.
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
