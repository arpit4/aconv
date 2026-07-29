"""Tkinter front end for aconv.py.

Stdlib only, run straight from a source checkout:

    python3 aconv_gui.py

The CLI stays the primary tool; this module is a pure consumer of its
--progress jsonl protocol. Everything below App is Tk-free, so the protocol
and process plumbing can be imported and tested without a display.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

# Tk is only needed by App and main(). The import failure is kept for main()
# to report, so that build_argv/parse_event/Runner stay importable on a
# machine without a tkinter build or a display.
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None

ACONV = Path(__file__).resolve().parent / 'aconv.py'

# The jsonl protocol version this GUI speaks. A child announcing anything
# else gets cancelled rather than misrendered.
PROTOCOL = 1

# Offered in the format combobox, which stays editable: aconv accepts any
# container ffmpeg can write.
FORMATS = ('mp3', 'flac', 'm4a', 'm4b', 'wav', 'ogg', 'opus', 'aac', 'mka')

# Checked when ffmpeg is not on PATH: apps launched from Finder or a desktop
# environment get a minimal PATH that misses the usual install locations.
FFMPEG_FALLBACK_DIRS = ('/opt/homebrew/bin', '/usr/local/bin')

# Substrings of ffmpeg's stderr mapped to one plain-English sentence each,
# matched case-insensitively, most specific first.
ERROR_PATTERNS = (
    ('invalid data found', "The file is corrupt or is not valid audio data."),
    ('corrupt', "The file is corrupt or is not valid audio data."),
    ('permission denied', "Permission was denied reading the source or writing the destination."),
    ('no space left', "The destination disk is out of space."),
    ('unknown encoder', "This ffmpeg build cannot encode the chosen format."),
    ('encoder not found', "This ffmpeg build cannot encode the chosen format."),
    ('unsupported codec', "This ffmpeg build does not support the file's codec."),
    ('no such file or directory', "The file could not be found; it may have been moved or deleted."),
)


def build_argv(settings):
    """Full child argv for one run described by `settings`.

    --on-existing is always passed (defaulting to skip), so the child never
    falls back to TTY detection for files already in the target format.
    Flags whose value is None or False are omitted.
    """
    # -b:a and -q:a are mutually exclusive in the CLI; refuse the pair here
    # rather than surface an argparse error from the child.
    if settings.get('bitrate') and settings.get('quality') is not None:
        raise ValueError("bitrate and quality are mutually exclusive")
    argv = [sys.executable, str(ACONV), settings['source'], settings['format'],
            '--progress', 'jsonl', '--stdin-control']
    if settings.get('dest'):
        argv.extend(['--dest', settings['dest']])
    if settings.get('workers') is not None:
        argv.extend(['--workers', str(settings['workers'])])
    if settings.get('bitrate'):
        argv.extend(['--bitrate', settings['bitrate']])
    if settings.get('quality') is not None:
        argv.extend(['--quality', str(settings['quality'])])
    if settings.get('sample_rate') is not None:
        argv.extend(['--sample-rate', str(settings['sample_rate'])])
    argv.extend(['--on-existing', settings.get('on_existing') or 'skip'])
    if settings.get('skip_existing'):
        argv.append('--skip-existing')
    if settings.get('dry_run'):
        argv.append('--dry-run')
    return argv


def parse_event(line):
    """Decode one protocol line into an event dict, or None.

    Deliberately defensive: a stray print in the child or a partial line at
    shutdown must not take the reader thread down, so anything that is not a
    JSON object with an "event" key is dropped.
    """
    try:
        data = json.loads(line)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict) and 'event' in data:
        return data
    return None


def translate_error(stderr_text):
    """One plain-English sentence for ffmpeg's captured stderr.

    Unrecognised output falls back to its first non-blank line, so an
    unexpected failure is still reported in ffmpeg's own words.
    """
    lowered = (stderr_text or '').lower()
    for pattern, sentence in ERROR_PATTERNS:
        if pattern in lowered:
            return sentence
    for line in (stderr_text or '').splitlines():
        if line.strip():
            return line.strip()
    return "ffmpeg reported no error output."


def find_ffmpeg():
    """Directory containing ffmpeg, or None if it cannot be found."""
    found = shutil.which('ffmpeg')
    if found:
        return str(Path(found).parent)
    for directory in FFMPEG_FALLBACK_DIRS:
        candidate = Path(directory) / 'ffmpeg'
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return directory
    return None


class Runner:
    """One child aconv run: the process, its reader thread, and .events.

    Runner never touches Tk. The reader thread parses stdout lines onto the
    .events queue and always finishes with an {'event': '_exit'} record, so
    the UI has a single, thread-safe stream to drain from its own side.
    """

    STDERR_TAIL = 2048

    def __init__(self, argv, env=None):
        self.argv = list(argv)
        self.env = env
        self.events = queue.Queue()
        self.process = None
        self._reader = None

    def start(self):
        self.process = subprocess.Popen(
            self.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE, text=True, env=self.env)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        # stderr is drained concurrently: a chatty child would otherwise fill
        # the pipe and deadlock against a reader waiting on stdout.
        chunks = []
        drain = threading.Thread(target=self._drain_stderr, args=(chunks,), daemon=True)
        drain.start()
        try:
            for line in self.process.stdout:
                event = parse_event(line)
                if event is not None:
                    self.events.put(event)
        finally:
            returncode = self.process.wait()
            drain.join()
            tail = ''.join(chunks)[-self.STDERR_TAIL:]
            # A GUI session can run many children; the pipes are closed here
            # rather than left to the GC so fds do not pile up. cancel() may
            # race this close, which is why it swallows ValueError too.
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
            self.events.put({'event': '_exit', 'returncode': returncode,
                             'stderr_tail': tail})

    def _drain_stderr(self, chunks):
        # Only the tail is kept: it is diagnostic context for the _exit
        # record, not a transcript, and must stay bounded on a huge batch.
        kept = 0
        for line in self.process.stderr:
            chunks.append(line)
            kept += len(line)
            while len(chunks) > 1 and kept - len(chunks[0]) >= self.STDERR_TAIL:
                kept -= len(chunks.pop(0))

    def cancel(self):
        """Ask the child to stop via its --stdin-control channel."""
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self.process.stdin.write('cancel\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            # BrokenPipeError included: a child that already exited has
            # nothing left to cancel.
            pass

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None


def install_command():
    """The ffmpeg install command for the current platform."""
    if sys.platform == 'darwin':
        return 'brew install ffmpeg'
    if sys.platform.startswith('win'):
        return 'winget install ffmpeg'
    return 'sudo apt install ffmpeg'


def open_folder(path):
    """Open `path` in the platform's file manager."""
    if sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    elif sys.platform.startswith('win'):
        os.startfile(str(path))  # noqa: os.startfile exists on Windows only
    else:
        subprocess.Popen(['xdg-open', str(path)])


class App:
    """The Tk application.

    The only reader-to-UI handoff is Runner.events, polled from the Tk main
    thread with root.after: no Tk call ever happens off the main thread.
    """

    POLL_MS = 100
    # A child that ignores 'cancel' cannot be allowed to block quitting the
    # window forever; after this long it is terminated outright.
    FORCE_QUIT_MS = 10000

    def __init__(self, root=None):
        self.root = root if root is not None else tk.Tk()
        self.root.title('aconv')
        self.root.minsize(680, 520)

        self.runner = None
        self.child_env = None
        self.failure_details = {}
        self._reset_run_state(dry_run=False)
        self.resume_pending = False

        self._build_ui()
        self._setup_ffmpeg()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    # ------------------------------------------------------------- state

    def _reset_run_state(self, dry_run):
        self.dry_run = dry_run
        self.protocol_mismatch = False
        self.cancel_requested = False
        self.quit_after_exit = False
        self.plan_info = None
        self.error_message = None
        self.file_actions = {}
        self.counts = {'converted': 0, 'remuxed': 0, 'failed': 0}
        self.total_files = 0
        self.done_files = 0
        self.done_seconds = 0.0
        self.seconds_reliable = True
        self.dest_shown = None

    # ---------------------------------------------------------------- UI

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.grid(row=0, column=0, sticky='nsew')
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        self._build_ffmpeg_panel(outer)
        self._build_form(outer)
        self._build_buttons(outer)
        self._build_progress(outer)
        self._build_results(outer)

    def _build_ffmpeg_panel(self, outer):
        panel = ttk.Frame(outer, padding=8, relief='groove', borderwidth=1)
        panel.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        panel.columnconfigure(1, weight=1)
        ttk.Label(panel, text='ffmpeg was not found on this machine. '
                             'Install it, then press Re-check:').grid(
            row=0, column=0, columnspan=3, sticky='w')
        ttk.Label(panel, text='Install:').grid(row=1, column=0, sticky='w', pady=(4, 0))
        # A read-only Entry rather than a Label, so the command is copyable.
        command_var = tk.StringVar(value=install_command())
        entry = ttk.Entry(panel, textvariable=command_var, state='readonly')
        entry.grid(row=1, column=1, sticky='ew', padx=6, pady=(4, 0))
        ttk.Button(panel, text='Re-check', command=self._setup_ffmpeg).grid(
            row=1, column=2, pady=(4, 0))
        self.ffmpeg_panel = panel
        panel.grid_remove()

    def _build_form(self, outer):
        form = ttk.Frame(outer)
        form.grid(row=1, column=0, sticky='ew')
        form.columnconfigure(1, weight=1)

        self.source_var = tk.StringVar()
        self.format_var = tk.StringVar(value='mp3')
        self.dest_var = tk.StringVar()
        self.on_existing_var = tk.StringVar(value='skip')
        self.bitrate_var = tk.StringVar()
        self.quality_var = tk.StringVar()
        self.sample_rate_var = tk.StringVar()
        self.workers_var = tk.StringVar()
        self.skip_existing_var = tk.BooleanVar(value=False)

        ttk.Label(form, text='Source:').grid(row=0, column=0, sticky='w')
        ttk.Entry(form, textvariable=self.source_var, state='readonly').grid(
            row=0, column=1, sticky='ew', padx=6, pady=2)
        pickers = ttk.Frame(form)
        pickers.grid(row=0, column=2, pady=2)
        ttk.Button(pickers, text='Choose Folder', command=self._choose_folder).pack(
            side='left', padx=(0, 4))
        ttk.Button(pickers, text='Choose File', command=self._choose_file).pack(side='left')

        ttk.Label(form, text='Format:').grid(row=1, column=0, sticky='w')
        combo = ttk.Combobox(form, textvariable=self.format_var, values=FORMATS, width=10)
        combo.grid(row=1, column=1, sticky='w', padx=6, pady=2)

        ttk.Label(form, text='Destination:').grid(row=2, column=0, sticky='w')
        ttk.Entry(form, textvariable=self.dest_var).grid(
            row=2, column=1, sticky='ew', padx=6, pady=2)
        ttk.Button(form, text='Choose...', command=self._choose_dest).grid(row=2, column=2, pady=2)
        # Blank destination means the CLI default; show what that computes to.
        self.dest_hint = ttk.Label(form, text='', foreground='gray50')
        self.dest_hint.grid(row=3, column=1, columnspan=2, sticky='w', padx=6)
        self.source_var.trace_add('write', self._update_dest_hint)
        self.format_var.trace_add('write', self._update_dest_hint)

        ttk.Label(form, text='Files already in format:').grid(row=4, column=0, sticky='w')
        radios = ttk.Frame(form)
        radios.grid(row=4, column=1, columnspan=2, sticky='w', padx=6, pady=2)
        for value, label in (('skip', 'Skip'), ('copy', 'Copy'), ('move', 'Move (removes originals)')):
            ttk.Radiobutton(radios, text=label, value=value,
                            variable=self.on_existing_var).pack(side='left', padx=(0, 10))

        self.advanced_shown = False
        self.advanced_button = ttk.Button(form, text='Advanced +', command=self._toggle_advanced)
        self.advanced_button.grid(row=5, column=0, sticky='w', pady=(8, 2))

        advanced = ttk.Frame(form, padding=(16, 2, 0, 2))
        advanced.grid(row=6, column=0, columnspan=3, sticky='ew')
        advanced.columnconfigure(1, weight=1)
        ttk.Label(advanced, text='Bitrate (e.g. 320k):').grid(row=0, column=0, sticky='w')
        self.bitrate_entry = ttk.Entry(advanced, textvariable=self.bitrate_var, width=10)
        self.bitrate_entry.grid(row=0, column=1, sticky='w', padx=6, pady=2)
        ttk.Label(advanced, text='VBR quality (e.g. 2):').grid(row=1, column=0, sticky='w')
        self.quality_entry = ttk.Entry(advanced, textvariable=self.quality_var, width=10)
        self.quality_entry.grid(row=1, column=1, sticky='w', padx=6, pady=2)
        ttk.Label(advanced, text='Sample rate (Hz):').grid(row=2, column=0, sticky='w')
        ttk.Entry(advanced, textvariable=self.sample_rate_var, width=10).grid(
            row=2, column=1, sticky='w', padx=6, pady=2)
        ttk.Label(advanced, text='Workers:').grid(row=3, column=0, sticky='w')
        ttk.Spinbox(advanced, textvariable=self.workers_var, from_=1, to=64, width=8).grid(
            row=3, column=1, sticky='w', padx=6, pady=2)
        ttk.Checkbutton(advanced, text='Skip existing outputs (resume)',
                        variable=self.skip_existing_var).grid(
            row=4, column=0, columnspan=2, sticky='w', pady=(4, 0))
        self.advanced_frame = advanced
        advanced.grid_remove()

        # -b:a and -q:a are mutually exclusive; filling one locks the other.
        self.bitrate_var.trace_add('write', self._sync_quality_lock)
        self.quality_var.trace_add('write', self._sync_quality_lock)
        # Unticking the checkbox withdraws the resume labelling too.
        self.skip_existing_var.trace_add('write', self._sync_resume_label)

    def _build_buttons(self, outer):
        row = ttk.Frame(outer)
        row.grid(row=2, column=0, sticky='ew', pady=8)
        self.preview_button = ttk.Button(row, text='Preview', command=self._on_preview)
        self.preview_button.pack(side='left')
        self.convert_button = ttk.Button(row, text='Convert', command=self._on_convert)
        self.convert_button.pack(side='left', padx=6)
        self.cancel_button = ttk.Button(row, text='Cancel', command=self._on_cancel,
                                        state='disabled')
        self.cancel_button.pack(side='left')
        self.open_button = ttk.Button(row, text='Open destination folder',
                                      command=self._on_open_dest, state='disabled')
        self.open_button.pack(side='right')

    def _build_progress(self, outer):
        frame = ttk.Frame(outer)
        frame.grid(row=3, column=0, sticky='ew')
        frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(frame, maximum=1.0, value=0.0)
        self.progress.grid(row=0, column=0, sticky='ew')
        self.measure_label = ttk.Label(frame, text='')
        self.measure_label.grid(row=0, column=1, sticky='e', padx=(6, 0))
        self.current_label = ttk.Label(frame, text='')
        self.current_label.grid(row=1, column=0, sticky='w', pady=(2, 0))
        self.fail_label = ttk.Label(frame, text='')
        self.fail_label.grid(row=1, column=1, sticky='e', padx=(6, 0), pady=(2, 0))
        self.status_label = ttk.Label(frame, text='Ready.')
        self.status_label.grid(row=2, column=0, columnspan=2, sticky='w', pady=(2, 4))

    def _build_results(self, outer):
        paned = ttk.PanedWindow(outer, orient='vertical')
        paned.grid(row=4, column=0, sticky='nsew')

        tree_frame = ttk.Frame(paned)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tree_frame, columns=('action', 'source', 'detail'),
                                 show='headings', selectmode='browse')
        self.tree.heading('action', text='Action')
        self.tree.heading('source', text='Source')
        self.tree.heading('detail', text='Destination')
        self.tree.column('action', width=90, stretch=False)
        self.tree.column('source', width=260)
        self.tree.column('detail', width=260)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        paned.add(tree_frame, weight=3)

        detail_frame = ttk.Frame(paned)
        detail_frame.rowconfigure(1, weight=1)
        detail_frame.columnconfigure(0, weight=1)
        header = ttk.Frame(detail_frame)
        header.grid(row=0, column=0, columnspan=2, sticky='ew')
        ttk.Label(header, text='Details').pack(side='left')
        ttk.Button(header, text='Copy details', command=self._on_copy_details).pack(side='right')
        self.detail_text = tk.Text(detail_frame, height=6, wrap='word', state='disabled')
        self.detail_text.grid(row=1, column=0, sticky='nsew')
        detail_scroll = ttk.Scrollbar(detail_frame, orient='vertical',
                                      command=self.detail_text.yview)
        detail_scroll.grid(row=1, column=1, sticky='ns')
        self.detail_text.configure(yscrollcommand=detail_scroll.set)
        paned.add(detail_frame, weight=1)

    # ------------------------------------------------------- form actions

    def _choose_folder(self):
        chosen = filedialog.askdirectory(title='Choose the source folder')
        if chosen:
            self.source_var.set(chosen)

    def _choose_file(self):
        chosen = filedialog.askopenfilename(title='Choose the source file')
        if chosen:
            self.source_var.set(chosen)

    def _choose_dest(self):
        chosen = filedialog.askdirectory(title='Choose the destination folder')
        if chosen:
            self.dest_var.set(chosen)

    def _default_dest(self):
        """The destination the CLI would pick for the current source/format."""
        source = self.source_var.get().strip()
        target = self.format_var.get().strip().lower().lstrip('.')
        if not source or not target:
            return None
        source_path = Path(source).expanduser().resolve()
        if source_path.is_file():
            return source_path.parent / f"{source_path.stem}_{target}"
        return source_path.parent / f"{source_path.name}_{target}"

    def _update_dest_hint(self, *_):
        default = self._default_dest()
        self.dest_hint.configure(text=f'Default: {default}' if default else '')

    def _sync_quality_lock(self, *_):
        if self.bitrate_var.get().strip():
            self.quality_entry.state(['disabled'])
        else:
            self.quality_entry.state(['!disabled'])
        if self.quality_var.get().strip():
            self.bitrate_entry.state(['disabled'])
        else:
            self.bitrate_entry.state(['!disabled'])

    def _sync_resume_label(self, *_):
        if not self.skip_existing_var.get():
            self.resume_pending = False
        self.convert_button.configure(
            text='Convert (resume)' if self.resume_pending else 'Convert')

    def _toggle_advanced(self):
        self.advanced_shown = not self.advanced_shown
        if self.advanced_shown:
            self.advanced_frame.grid()
            self.advanced_button.configure(text='Advanced -')
        else:
            self.advanced_frame.grid_remove()
            self.advanced_button.configure(text='Advanced +')

    # ------------------------------------------------------------- ffmpeg

    def _setup_ffmpeg(self):
        """Find ffmpeg, show the install panel if absent, and build the
        child environment when it lives outside PATH."""
        directory = find_ffmpeg()
        if directory is None:
            self.ffmpeg_panel.grid()
            self.child_env = None
            return
        self.ffmpeg_panel.grid_remove()
        if shutil.which('ffmpeg') is None:
            env = dict(os.environ)
            env['PATH'] = directory + os.pathsep + env.get('PATH', '')
            self.child_env = env
        else:
            self.child_env = None

    # ---------------------------------------------------------- run flow

    def _collect_settings(self, dry_run):
        """The settings dict for build_argv, or None after reporting a
        validation problem to the user."""
        source = self.source_var.get().strip()
        target = self.format_var.get().strip()
        if not source:
            messagebox.showerror('aconv', 'Choose a source folder or file first.')
            return None
        if not target:
            messagebox.showerror('aconv', 'Choose a target format first.')
            return None
        try:
            workers = self._int_field(self.workers_var, 'Workers')
            quality = self._int_field(self.quality_var, 'VBR quality')
            sample_rate = self._int_field(self.sample_rate_var, 'Sample rate')
        except ValueError as error:
            messagebox.showerror('aconv', str(error))
            return None
        return {
            'source': source,
            'format': target,
            'dest': self.dest_var.get().strip() or None,
            'workers': workers,
            'bitrate': self.bitrate_var.get().strip() or None,
            'quality': quality,
            'sample_rate': sample_rate,
            'on_existing': self.on_existing_var.get() or 'skip',
            'skip_existing': bool(self.skip_existing_var.get()),
            'dry_run': dry_run,
        }

    @staticmethod
    def _int_field(var, name):
        text = var.get().strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            raise ValueError(f'{name} must be a whole number.')

    def _on_preview(self):
        self._start_run(dry_run=True)

    def _on_convert(self):
        self._start_run(dry_run=False)

    def _start_run(self, dry_run):
        if self.runner is not None and self.runner.running:
            return
        settings = self._collect_settings(dry_run)
        if settings is None:
            return
        # Mirrors the CLI's typed confirmation: moving deletes originals, so
        # a real run never starts on 'move' without an explicit yes.
        if not dry_run and settings['on_existing'] == 'move':
            confirmed = messagebox.askyesno(
                'aconv',
                'Files already in the target format will be MOVED: the '
                'originals are removed from the source.\n\nProceed?')
            if not confirmed:
                return
        try:
            argv = build_argv(settings)
        except ValueError as error:
            messagebox.showerror('aconv', str(error))
            return

        resuming = self.resume_pending and settings['skip_existing'] and not dry_run
        self._reset_run_state(dry_run)
        self._clear_results()
        self.tree.heading('detail', text='Destination' if dry_run else 'Problem')
        self.preview_button.state(['disabled'])
        self.convert_button.state(['disabled'])
        self.cancel_button.state(['!disabled'])
        self.open_button.state(['disabled'])
        self.progress.configure(maximum=1.0, value=0.0)
        if dry_run:
            self.status_label.configure(text='Previewing...')
        elif resuming:
            self.status_label.configure(text='Resuming (existing outputs are skipped)...')
        else:
            self.status_label.configure(text='Starting...')

        self.runner = Runner(argv, env=self.child_env)
        try:
            self.runner.start()
        except OSError as error:
            self.runner = None
            self._set_idle_buttons()
            messagebox.showerror('aconv', f'Could not start aconv: {error}')
            return
        self.root.after(self.POLL_MS, self._poll)

    def _clear_results(self):
        self.tree.delete(*self.tree.get_children())
        self.failure_details = {}
        self.current_label.configure(text='')
        self.measure_label.configure(text='')
        self.fail_label.configure(text='')
        self._set_detail('')

    def _set_idle_buttons(self):
        self.preview_button.state(['!disabled'])
        self.convert_button.state(['!disabled'])
        self.cancel_button.state(['disabled'])

    def _poll(self):
        exited = None
        while True:
            try:
                event = self.runner.events.get_nowait()
            except queue.Empty:
                break
            if event.get('event') == '_exit':
                exited = event
                break
            self._handle_event(event)
        if exited is not None:
            self._finish_run(exited)
        else:
            self.root.after(self.POLL_MS, self._poll)

    # ------------------------------------------------------ event handling

    def _handle_event(self, event):
        name = event.get('event')
        if name == 'hello':
            if event.get('protocol') != PROTOCOL:
                # Nothing after a mismatched hello can be trusted to mean
                # what this GUI thinks it means; stop the child instead.
                self.protocol_mismatch = True
                self.runner.cancel()
                self.status_label.configure(text='GUI and aconv.py versions do not match.')
            return
        if self.protocol_mismatch:
            return
        handler = getattr(self, f'_on_event_{name}', None)
        if handler is not None:
            handler(event)

    def _on_event_scan(self, event):
        self.status_label.configure(
            text=f"Found {event.get('found', 0)} audio file(s): "
                 f"{event.get('to_convert', 0)} to convert, "
                 f"{event.get('already_in_format', 0)} already in the target format.")

    def _on_event_plan(self, event):
        self.plan_info = event
        self.dest_shown = event.get('dest')
        self.total_files = (event.get('convert') or 0) + (event.get('remux') or 0)

    def _on_event_plan_item(self, event):
        self.tree.insert('', 'end', values=(event.get('action', ''),
                                            event.get('source', ''),
                                            event.get('dest', '')))

    def _on_event_probe(self, event):
        self.measure_label.configure(
            text=f"Measuring {event.get('done', 0)}/{event.get('total', 0)}")

    def _on_event_keep_done(self, event):
        word = (self.plan_info or {}).get('on_existing', 'copy')
        self.status_label.configure(
            text=f"{event.get('count', 0)} file(s) already in the target format "
                 f"handled ({word}).")

    def _on_event_file_start(self, event):
        self.measure_label.configure(text='')
        source = event.get('source', '')
        action = event.get('action', 'convert')
        self.file_actions[source] = action
        self.current_label.configure(text=f"{action}: {Path(source).name}")
        self.status_label.configure(text='Converting...')

    def _on_event_file_done(self, event):
        self.done_files += 1
        action = self.file_actions.get(event.get('source', ''), 'convert')
        self.counts['remuxed' if action == 'remux' else 'converted'] += 1
        seconds = event.get('seconds')
        if seconds is None:
            self.seconds_reliable = False
        else:
            self.done_seconds += seconds
        self._update_progress()

    def _on_event_file_failed(self, event):
        self.counts['failed'] += 1
        self.done_files += 1
        # A failed file has no trustworthy duration; keep the weights honest.
        self.seconds_reliable = False
        error = event.get('error') or ''
        item = self.tree.insert('', 'end', values=('failed',
                                                   event.get('source', ''),
                                                   translate_error(error)))
        self.failure_details[item] = error
        self.fail_label.configure(text=f"Failures: {self.counts['failed']}")
        self._update_progress()

    def _on_event_interrupted(self, event):
        self.status_label.configure(
            text=f"Interrupted; dropped {event.get('dropped', 0)} queued file(s).")

    def _on_event_error(self, event):
        self.error_message = event.get('message', '')

    def _on_event_done(self, event):
        pass  # the _exit returncode is the ground truth; done is display only

    def _update_progress(self):
        """Advance the bar by audio seconds when every finished file reported
        one, else by file counts.

        The protocol carries no total-seconds figure, so the weighted total
        is estimated from the running average across the files done so far.
        """
        total = max(self.total_files, self.done_files)
        if not total:
            return
        if self.seconds_reliable and self.done_files:
            estimated = self.done_seconds / self.done_files * total
            if estimated > 0:
                self.progress.configure(maximum=estimated, value=self.done_seconds)
                return
        self.progress.configure(maximum=total, value=self.done_files)

    # ------------------------------------------------------------- finish

    def _finish_run(self, exit_event):
        returncode = exit_event.get('returncode')
        stderr_tail = exit_event.get('stderr_tail') or ''
        self.runner = None
        if self.quit_after_exit:
            self.root.destroy()
            return
        self._set_idle_buttons()
        self.current_label.configure(text='')
        self.measure_label.configure(text='')

        if self.protocol_mismatch:
            self.status_label.configure(text='GUI and aconv.py versions do not match.')
            messagebox.showerror('aconv', 'GUI and aconv.py versions do not match.')
            return

        if self.dry_run:
            self._finish_preview(returncode, stderr_tail)
            return

        if returncode == 0:
            self.progress.configure(maximum=1.0, value=1.0)
            self.status_label.configure(
                text=f"Done: {self.counts['converted']} converted, "
                     f"{self.counts['remuxed']} remuxed.")
        elif self.cancel_requested or returncode == 130:
            self.status_label.configure(text='Cancelled.')
            self._prime_resume()
        else:
            message = self.error_message or translate_error(stderr_tail)
            if self.counts['failed']:
                message = (f"Finished with {self.counts['failed']} failure(s): "
                           f"{self.counts['converted']} converted, "
                           f"{self.counts['remuxed']} remuxed.")
            self.status_label.configure(text=message)
            if not self.counts['failed'] and stderr_tail:
                self._set_detail(stderr_tail)
            self._prime_resume()
        if self.done_files:
            self.open_button.state(['!disabled'])

    def _finish_preview(self, returncode, stderr_tail):
        if returncode == 0 and self.plan_info is not None:
            plan = self.plan_info
            self.status_label.configure(
                text=f"Preview: {plan.get('keep', 0)} to {plan.get('on_existing', 'skip')}, "
                     f"{plan.get('convert', 0)} to convert, {plan.get('remux', 0)} to remux "
                     f"-> {plan.get('dest', '')}")
        elif returncode == 0:
            self.status_label.configure(text='Preview finished.')
        elif self.cancel_requested or returncode == 130:
            # Exit 130 is a cancel, not a failure; translating its stderr
            # would dress the run's last human line up as an error.
            self.status_label.configure(text='Preview cancelled.')
        else:
            self.status_label.configure(
                text=self.error_message or translate_error(stderr_tail))
            if stderr_tail:
                self._set_detail(stderr_tail)

    def _prime_resume(self):
        # After a cancelled or partly failed run the finished outputs are
        # already on disk; the next run should pick up where this one stopped.
        self.skip_existing_var.set(True)
        self.resume_pending = True
        self._sync_resume_label()

    # ------------------------------------------------------ other actions

    def _on_cancel(self):
        if self.runner is None or not self.runner.running:
            return
        self.cancel_requested = True
        self.cancel_button.state(['disabled'])
        self.status_label.configure(text='Cancelling...')
        self.runner.cancel()
        # A child that ignores the channel must not leave Cancel a dead button
        # with ffmpeg still burning CPU; SIGTERM reaches the same cleanup path
        # through the CLI's handler.
        self.root.after(self.FORCE_QUIT_MS, self._force_cancel, self.runner)

    def _force_cancel(self, runner):
        # Bound to the run it was armed for: by the time the timer fires that
        # run may be long gone, and a later one must not be shot down.
        if self.runner is runner and runner.running:
            runner.process.terminate()

    def _on_close(self):
        if self.runner is not None and self.runner.running:
            confirmed = messagebox.askyesno(
                'aconv', 'A run is still in progress. Cancel it and quit?')
            if not confirmed:
                return
            # The modal dialog pumps the event loop, so _poll kept draining
            # and the child may have finished while the question was open.
            if self.runner is None or not self.runner.running:
                self.root.destroy()
                return
            self.quit_after_exit = True
            self.cancel_requested = True
            self.cancel_button.state(['disabled'])
            self.status_label.configure(text='Cancelling...')
            self.runner.cancel()
            self.root.after(self.FORCE_QUIT_MS, self._force_quit)
            return
        self.root.destroy()

    def _force_quit(self):
        if self.runner is not None and self.runner.running:
            self.runner.process.terminate()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _on_open_dest(self):
        dest = self.dest_shown or self.dest_var.get().strip() or self._default_dest()
        if dest:
            open_folder(dest)

    def _on_select(self, _event):
        selection = self.tree.selection()
        if not selection:
            return
        item = selection[0]
        raw = self.failure_details.get(item)
        values = self.tree.item(item, 'values')
        if raw is not None:
            self._set_detail(f"{values[1]}\n{translate_error(raw)}\n\n{raw}")
        else:
            self._set_detail(f"{values[0]}: {values[1]}\n-> {values[2]}")

    def _on_copy_details(self):
        text = self.detail_text.get('1.0', 'end').strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _set_detail(self, text):
        self.detail_text.configure(state='normal')
        self.detail_text.delete('1.0', 'end')
        self.detail_text.insert('1.0', text)
        self.detail_text.configure(state='disabled')


def main():
    if tk is None:
        print("Error: tkinter is not available in this Python.\n"
              "Install your platform's Tk package (e.g. 'sudo apt install "
              "python3-tk') and try again.", file=sys.stderr)
        sys.exit(1)
    app = App()
    app.root.mainloop()


if __name__ == '__main__':
    main()
