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

AUDIO_EXTENSIONS = {'.m4a', '.mp3', '.wav', '.flac', '.ogg', '.aac', '.wma', '.alac', '.aiff'}

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

def convert_file(source_file, dest_file):
    # Ensure destination directory exists
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Run ffmpeg
    # -y overwrites without asking
    # -v error suppresses standard output except errors
    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', str(source_file), str(dest_file)]
    try:
        subprocess.run(cmd, check=True, stderr=subprocess.PIPE)
        return True, str(source_file)
    except subprocess.CalledProcessError as e:
        return False, f"Failed to convert {source_file}: {e.stderr.decode('utf-8').strip()}"

def main():
    parser = argparse.ArgumentParser(description="Offline Audio Format Converter")
    parser.add_argument("source", nargs='?', help="Source directory or file")
    parser.add_argument("format", nargs='?', help="Target audio format (e.g., mp3, wav, flac)")
    parser.add_argument("--dest", help="Destination directory (optional)", default=None)
    parser.add_argument("--ext", help="Specific source extension to filter by (optional)", default=None)
    parser.add_argument("--workers", help="Number of parallel conversions", type=int, default=os.cpu_count() or 4)

    args = parser.parse_args()

    if not args.source:
        args.source = input("Enter the source directory or file path: ").strip()
        while not args.source:
            args.source = input("Source path cannot be empty. Enter source directory or file path: ").strip()
            
    if not args.format:
        args.format = input("Enter the target audio format (e.g., mp3, wav, flac): ").strip()
        while not args.format:
            args.format = input("Target format cannot be empty. Enter the target audio format: ").strip()

    source_path = Path(args.source).resolve()

    if not args.ext and source_path.is_dir(): 
        ext_input = input("Enter specific source extension to convert (e.g. m4a), or press Enter to convert all audio files: ").strip()
        if ext_input:
            args.ext = ext_input

    check_ffmpeg()

    if not source_path.exists():
        print(f"Error: Source '{args.source}' does not exist.")
        sys.exit(1)

    target_format = args.format.lower().lstrip('.')

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
            
        while True:
            choice = input(f"\nWould you like to [c]opy, [m]ove, or [s]kip these files to the destination? ").strip().lower()
            if choice in ['c', 'm', 's', 'copy', 'move', 'skip']:
                break
                
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
                    shutil.move(f, dest_file)
            print("Done.")

    audio_files = to_convert
    if not audio_files:
        print("\nNo files left to convert.")
        sys.exit(0)
    
    tasks = []
    
    print(f"Found {len(audio_files)} audio files. Converting to .{target_format}...")
    print(f"Destination: {dest_dir}")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for audio_file in audio_files:
            # Determine relative path to maintain folder structure
            if source_path.is_file():
                rel_path = audio_file.name
                dest_file = dest_dir / Path(rel_path).with_suffix(f".{target_format}")
            else:
                rel_path = audio_file.relative_to(source_path)
                dest_file = dest_dir / rel_path.with_suffix(f".{target_format}")
            
            tasks.append(executor.submit(convert_file, audio_file, dest_file))

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
