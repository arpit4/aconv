import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from tqdm import tqdm
except ImportError:
    print("tqdm not installed. Please run: pip install -r requirements.txt")
    sys.exit(1)

AUDIO_EXTENSIONS = {'.m4a', '.mp3', '.wav', '.flac', '.ogg', '.aac', '.wma', '.alac', '.aiff', '.opus'}

# Containers that can carry an embedded cover image. For every other target the
# attached picture has to be dropped, because ffmpeg otherwise tries to re-encode
# it with the container's default video codec and fails outright (for example
# .ogg defaults to theora, which is usually not built in).
ART_CAPABLE_FORMATS = {'mp3', 'flac', 'm4a', 'm4b', 'mp4'}

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

def check_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: ffmpeg is not installed or not found in PATH.")
        print("Please install it (e.g., 'brew install ffmpeg' on macOS) and try again.")
        sys.exit(1)

def find_audio_files(source_dir, ext_filter=None):
    audio_files = []
    source_path = Path(source_dir)

    if ext_filter:
        ext_filter = ext_filter.lower()
        if not ext_filter.startswith('.'):
            ext_filter = '.' + ext_filter

    if source_path.is_file() and source_path.suffix.lower() in AUDIO_EXTENSIONS:
        if ext_filter and source_path.suffix.lower() != ext_filter:
            return []
        return [source_path]

    for path in source_path.rglob('*'):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            if ext_filter and path.suffix.lower() != ext_filter:
                continue
            audio_files.append(path)
    return audio_files

def convert_file(source_file, dest_file, extra_args=None):
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
    try:
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True, str(source_file)
    except subprocess.CalledProcessError as e:
        # A failed or interrupted ffmpeg still leaves an empty or truncated file
        # behind. Remove it, otherwise the next run sees a file that is already
        # in the target format and treats the broken output as finished work.
        try:
            dest_file.unlink(missing_ok=True)
        except OSError:
            pass
        err = e.stderr.decode('utf-8', errors='replace').strip()
        return False, f"Failed to convert {source_file}: {err}"

def build_ffmpeg_args(args, target_format):
    """Translate CLI options into ffmpeg arguments."""
    extra = []
    # Keep embedded cover art where the target container supports it, and drop
    # it everywhere else so that art does not turn into a conversion failure.
    if target_format in ART_CAPABLE_FORMATS:
        extra.extend(['-c:v', 'copy'])
    else:
        extra.append('-vn')
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

def main():
    parser = argparse.ArgumentParser(description="Offline Audio Format Converter")
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

    args = parser.parse_args()

    if args.workers < 1:
        print("Error: --workers must be a positive integer.")
        sys.exit(1)

    interactive = sys.stdin.isatty()

    if not args.source:
        if not interactive:
            print("Error: source is required (no input available to prompt for it).")
            sys.exit(1)
        args.source = prompt_required(
            "Enter the source directory or file path: ",
            "Source path cannot be empty. Enter source directory or file path: ")

    if not args.format:
        if not interactive:
            print("Error: target format is required (no input available to prompt for it).")
            sys.exit(1)
        args.format = prompt_required(
            "Enter the target audio format (e.g., mp3, wav, flac): ",
            "Target format cannot be empty. Enter the target audio format: ")

    source_path = Path(args.source).resolve()

    # Validate early, before any further prompts, so the user isn't asked
    # questions only to be told the source is missing or ffmpeg is absent.
    if not source_path.exists():
        print(f"Error: Source '{args.source}' does not exist.")
        sys.exit(1)

    check_ffmpeg()

    # Only prompt for an extension filter interactively; under automation
    # (no TTY) default to converting all audio files rather than crashing.
    if not args.ext and source_path.is_dir() and interactive:
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

    audio_files = find_audio_files(source_path, args.ext)

    if not audio_files:
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

    choice = 's'
    if already_in_format:
        print(f"\nFound {len(already_in_format)} file(s) already in '{target_format}' format:")
        for f in already_in_format[:5]:
            print(f"  - {f.name}")
        if len(already_in_format) > 5:
            print(f"  ... and {len(already_in_format) - 5} more.")

        if not interactive:
            # No TTY: default to the non-destructive choice rather than crash.
            print("Non-interactive run: skipping files already in the target format.")
        else:
            while True:
                answer = prompt(f"\nWould you like to [c]opy, [m]ove, or [s]kip these files to the destination? ", 's')
                if answer in ['c', 'm', 's', 'copy', 'move', 'skip']:
                    choice = answer
                    break

        if choice.startswith('m'):
            # Moving deletes the originals; require explicit confirmation.
            confirm = prompt(f"This will MOVE (remove) {len(already_in_format)} original file(s). Type 'yes' to proceed: ", '')
            if confirm != 'yes':
                print("Move cancelled; skipping these files.")
                choice = 's'

    # Reserve destinations for the copied or moved files before planning the
    # conversions, so that converting song.wav cannot overwrite a song.mp3 that
    # the user asked to keep.
    keep_files = already_in_format if choice.startswith(('c', 'm')) else []
    keep_plan, convert_plan = plan_destinations(
        source_path, dest_dir, keep_files, to_convert, target_format)

    if keep_plan:
        action_name = "Copying" if choice.startswith('c') else "Moving"
        print(f"{action_name} {len(keep_plan)} files to {dest_dir}...")
        for source_file, dest_file in keep_plan:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            if choice.startswith('c'):
                shutil.copy2(source_file, dest_file)
            else:
                shutil.move(str(source_file), str(dest_file))
        print("Done.")

    if not convert_plan:
        print("\nNo files left to convert.")
        sys.exit(0)

    print(f"Found {len(convert_plan)} audio files. Converting to .{target_format}...")
    print(f"Destination: {dest_dir}")
    if args.bitrate or args.quality is not None or args.sample_rate:
        print(f"ffmpeg options: {' '.join(extra_args)}")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        tasks = [executor.submit(convert_file, src, dst, extra_args) for src, dst in convert_plan]

        success_count = 0
        with tqdm(total=len(tasks), desc="Converting", unit="file") as pbar:
            for future in as_completed(tasks):
                success, result = future.result()
                if success:
                    success_count += 1
                else:
                    tqdm.write(result)  # Print error without breaking progress bar
                pbar.update(1)

    failed_count = len(convert_plan) - success_count
    print(f"\nConversion complete! {success_count}/{len(convert_plan)} files successfully converted.")
    if failed_count:
        # Exit non-zero so scripts and CI notice a partially failed batch.
        print(f"{failed_count} file(s) failed to convert.")
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
