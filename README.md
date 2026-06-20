# Offline Audio File Format Converter

A fast, interactive, and offline command-line tool for converting audio files between formats (e.g., `.m4a` to `.mp3`) using `ffmpeg`. It maintains your folder structure, processes files in parallel for maximum speed, and handles pre-existing target formats elegantly.

## Features

- **Interactive Mode**: Run the script with no arguments to be prompted for inputs.
- **Fast & Parallel**: Uses all available CPU cores to convert files simultaneously.
- **Smart Directory Handling**: Replicates the original folder structure in the destination.
- **Existing File Detection**: Automatically detects files that are already in the target format and offers to copy, move, or skip them without re-encoding.
- **Format Filtering**: Optionally filter conversions to a specific source extension.
- **Clean Progress Bar**: Shows conversion progress via `tqdm`.

## Requirements

1. **Python 3.x**
2. **ffmpeg**: Must be installed and accessible in your system's PATH.
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`
   - Windows: Install via `winget install ffmpeg` or download from [ffmpeg.org](https://ffmpeg.org).
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

### Interactive Mode
The easiest way to use the tool is to run it without arguments:
```bash
python3 aconv.py
```
You will be prompted to enter the source directory, target format, and any specific extensions to filter.

### Command-Line Arguments
For automation, you can provide arguments directly:

**Convert a single folder to mp3:**
```bash
python3 aconv.py /path/to/my_music mp3
```
*(This creates a new folder named `my_music_mp3` next to `my_music` and converts everything inside).*

**Convert to wav and specify a custom destination:**
```bash
python3 aconv.py /path/to/my_music wav --dest /path/to/destination_folder
```

**Convert a single file:**
```bash
python3 aconv.py /path/to/song.m4a flac
```

## How It Works

1. Scans the source path for audio files (e.g., `.m4a`, `.wav`, `.flac`).
2. Checks if any files are already in the target format. If so, you'll be prompted to copy, move, or skip them.
3. Spawns an `ffmpeg` subprocess for each file needing conversion, executing them in parallel.
4. Outputs the converted files into the destination folder, mimicking the original directory tree.

## License
MIT License
