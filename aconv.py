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
    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', str(source_file)]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(dest_file))
    try:
        subprocess.run(cmd, check=True, stderr=subprocess.PIPE)
        return True, str(source_file)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode('utf-8', errors='replace').strip()
        return False, f"Failed to convert {source_file}: {err}"

def build_ffmpeg_args(args):
    """Translate quality-related CLI options into ffmpeg arguments."""
    extra = []
    if args.bitrate:
        extra.extend(['-b:a', args.bitrate])
    if args.quality is not None:
        extra.extend(['-q:a', str(args.quality)])
    if args.sample_rate:
        extra.extend(['-ar', str(args.sample_rate)])
    return extra

def main():
    parser = argparse.ArgumentParser(description="Offline Audio Format Converter")
    parser.add_argument("source", nargs='?', help="Source directory or file")
    parser.add_argument("format", nargs='?', help="Target audio format (e.g., mp3, wav, flac)")
    parser.add_argument("--dest", help="Destination directory (optional)", default=None)
    parser.add_argument("--ext", help="Specific source extension to filter by (optional)", default=None)
    parser.add_argument("--workers", help="Number of parallel conversions", type=int,
                        default=max(1, (os.cpu_count() or 4) // 2))
    parser.add_argument("--bitrate", help="Target audio bitrate, e.g. 320k (CBR)", default=None)
    parser.add_argument("--quality", help="VBR quality for the codec (ffmpeg -q:a), e.g. 2 for mp3",
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
        args.source = input("Enter the source directory or file path: ").strip()
        while not args.source:
            args.source = input("Source path cannot be empty. Enter source directory or file path: ").strip()

    if not args.format:
        if not interactive:
            print("Error: target format is required (no input available to prompt for it).")
            sys.exit(1)
        args.format = input("Enter the target audio format (e.g., mp3, wav, flac): ").strip()
        while not args.format:
            args.format = input("Target format cannot be empty. Enter the target audio format: ").strip()

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
        ext_input = input("Enter specific source extension to convert (e.g. m4a), or press Enter to convert all audio files: ").strip()
        if ext_input:
            args.ext = ext_input

    target_format = args.format.lower().lstrip('.')
    extra_args = build_ffmpeg_args(args)

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

    if already_in_format:
        print(f"\nFound {len(already_in_format)} file(s) already in '{target_format}' format:")
        for f in already_in_format[:5]:
            print(f"  - {f.name}")
        if len(already_in_format) > 5:
            print(f"  ... and {len(already_in_format) - 5} more.")

        if not interactive:
            # No TTY: default to the non-destructive choice rather than crash.
            print("Non-interactive run: skipping files already in the target format.")
            choice = 's'
        else:
            while True:
                choice = input(f"\nWould you like to [c]opy, [m]ove, or [s]kip these files to the destination? ").strip().lower()
                if choice in ['c', 'm', 's', 'copy', 'move', 'skip']:
                    break

        if choice.startswith('m'):
            # Moving deletes the originals; require explicit confirmation.
            confirm = input(f"This will MOVE (remove) {len(already_in_format)} original file(s). Type 'yes' to proceed: ").strip().lower()
            if confirm != 'yes':
                print("Move cancelled; skipping these files.")
                choice = 's'

        if choice.startswith('c') or choice.startswith('m'):
            action_name = "Copying" if choice.startswith('c') else "Moving"
            print(f"{action_name} {len(already_in_format)} files to {dest_dir}...")
            for f in already_in_format:
                if source_path.is_file():
                    dest_file = dest_dir / f.name
                else:
                    dest_file = dest_dir / f.relative_to(source_path)
                dest_file.parent.mkdir(parents=True, exist_ok=True)

                if choice.startswith('c'):
                    shutil.copy2(f, dest_file)
                else:
                    shutil.move(str(f), str(dest_file))
            print("Done.")

    audio_files = to_convert
    if not audio_files:
        print("\nNo files left to convert.")
        sys.exit(0)

    print(f"Found {len(audio_files)} audio files. Converting to .{target_format}...")
    print(f"Destination: {dest_dir}")
    if extra_args:
        print(f"ffmpeg options: {' '.join(extra_args)}")

    # Map each source file to a destination, disambiguating collisions so that
    # e.g. song.wav and song.flac don't both overwrite song.mp3.
    used_dests = set()
    plan = []
    for audio_file in audio_files:
        if source_path.is_file():
            rel_path = Path(audio_file.name)
        else:
            rel_path = audio_file.relative_to(source_path)
        dest_file = dest_dir / rel_path.with_suffix(f".{target_format}")

        if dest_file in used_dests:
            # Collision: append the original extension to keep both outputs.
            orig_ext = audio_file.suffix.lstrip('.')
            dest_file = dest_file.with_name(f"{dest_file.stem}_{orig_ext}{dest_file.suffix}")
        used_dests.add(dest_file)
        plan.append((audio_file, dest_file))

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        tasks = [executor.submit(convert_file, src, dst, extra_args) for src, dst in plan]

        success_count = 0
        with tqdm(total=len(tasks), desc="Converting", unit="file") as pbar:
            for future in as_completed(tasks):
                success, result = future.result()
                if success:
                    success_count += 1
                else:
                    tqdm.write(result)  # Print error without breaking progress bar
                pbar.update(1)

    print(f"\nConversion complete! {success_count}/{len(audio_files)} files successfully converted.")

if __name__ == "__main__":
    main()
