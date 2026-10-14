# Transcription Queue

A Windows desktop application built with Python and Tkinter/ttk for processing YouTube videos and local audio or video recordings. It downloads audio, optionally separates vocals, transcribes speech locally with Whisper/OpenVINO, and identifies speakers. SQLite stores queue progress; completed recordings are exported to TXT, SRT, VTT, and JSON.

[User guide (Polish)](KOLEJKA.md) | [Validation notes (Polish)](docs/WERYFIKACJA.md) | [Repository migration (Polish)](docs/MIGRACJA.md) | [MIT license](LICENSE)

## Features

- Import a YouTube catalog from XLSX, individual local files, or folders with subfolders. Repeated imports recognize duplicate content and preserve progress.
- Separate YouTube and local queue tabs. Local Start processes every enabled recording; a separate selected-jobs button limits a session without disabling other jobs.
- Jump to the current recording or the first enabled pending job. Lists show duration and retain scrolling and selection during refreshes.
- A configuration tab with background checks and a dated sample test of up to 60 seconds using the current vocal separation and speaker settings.
- Choose where local results are saved: a dedicated subfolder beside the source, the central results directory, or a custom directory. Existing jobs retain their destinations.
- Process one recording at a time, in chronological order, with search, filters, language overrides, and audio track selection.
- Start or resume a session, or finish the current recording and then stop. The worker continues when the window closes; reopening the application reconnects to the queue.
- Start stays visibly pressed during launch and processing, prevents repeated launches, and returns to its idle state when the worker stops. Failed preflight checks show their reason and restore the button.
- Resume transcription, speaker diarization, and vocal separation from verified checkpoints after an interruption.
- Whisper large-v3-turbo through OpenVINO GenAI on CPU, word timestamps, pyannote Community-1 for speaker diarization, and optional Universal Vocal Remover (UVR) through audio-separator.
- Optionally retain source audio and the separated vocal track. Original local files remain untouched.
- Unique output names based on the recording ID, atomic writes, and versioned exports when an output name is already occupied.
- A transcript editing tab that creates TXT/SRT/VTT copies without speaker labels, preserving timestamps and spoken text.
- Delayed tooltips, CSV progress reports, and optional YouTube session settings with a test of one video link.

This repository contains application code and pinned dependency files. Models, Python environments, private video catalogs, cookies, tokens, and processing results are kept locally. Transcription runs directly through OpenVINO and does not require Audacity.

## Requirements

- Windows 10 or 11, Python 3.13 for the main environment, and Python 3.12 for speaker diarization and optional UVR. Installers use the `py` launcher or explicitly supplied interpreter paths.
- FFmpeg and FFprobe 9, plus Node.js 22, available on `PATH` or configured by their full paths. The Windows CI workflow uses FFmpeg 8.0.1.
- Disk space for models, temporary audio, and any retained recordings. Mono float32 PCM at 16 kHz uses approximately 64 kB per second; vocal separation needs additional space.
- Internet access for the initial model downloads and for YouTube recordings. Community-1 requires accepting the model's access conditions and a Hugging Face token with Read permissions.

Whisper uses CPU by default. The separate diarization and UVR environments use PyTorch with CUDA 12.6. These stages check GPU execution and fall back to CPU with a message if GPU processing fails. Detecting a device does not guarantee that every model and driver combination can use it.

## Installation and first test

Open PowerShell in the cloned repository:

```powershell
.\Instaluj-zaleznosci.ps1
.\.venv\Scripts\python.exe transcribe.py download
# Optional vocal separation:
.\Instaluj-UVR.ps1
.\Uruchom-kolejke.vbs
```

The installers use the versions in `requirements-*.lock.txt`. To supply your own Python interpreters:

```powershell
.\Instaluj-zaleznosci.ps1 -AsrInterpreter 'C:\Python313\python.exe' -DiarizationInterpreter 'C:\Python312\python.exe'
.\Instaluj-UVR.ps1 -Interpreter 'C:\Python312\python.exe'
```

In the configuration tab, open the Community-1 setup, accept the access conditions on the [model page](https://huggingface.co/pyannote/speaker-diarization-community-1), verify your Read token, and download the model. Enable or disable vocal separation and speaker diarization, then run a sample test. You can select a long recording; the test processes up to 60 seconds. Review the transcript and speaker labels before starting a large session. Importing recordings never starts processing automatically.

The interface is currently in Polish. These are the main controls:

| Action | Label in the application |
| --- | --- |
| Open configuration | Konfiguracja |
| Set up speaker diarization | Konfiguracja Community-1 |
| Test a recording sample | Sprawdź na fragmencie |
| Select a folder and import its recordings | Dodaj folder... |
| Import from the entered folder path | Dodaj z podanej ścieżki |
| Start or resume the YouTube queue | Start/Wznów |
| Start or resume the local queue | Start/Wznów lokalne |
| Start selected local jobs | Start zaznaczonych (N) |
| Resume a missing tail | Wznów brakującą część |
| Open verified partial text | Otwórz częściowy TXT |
| Open a recording's error log | Otwórz log błędu |
| Finish the current recording, then stop | Dokończ bieżący i zatrzymaj |
| Retry failed jobs | Ponów nieudane |
| Process selected YouTube jobs only | Start tylko zaznaczonych |
| Configure YouTube access | Dostęp YouTube |

## Data directory

The default data directory is `%USERPROFILE%\Transkrypcje`, outside the repository. Set `KOLEJKA_ROOT` before launching the application, or pass `--root` to use another directory:

```powershell
.\.venv\Scripts\python.exe kolejka.py --root 'D:\Transcriptions' gui
.\.venv\Scripts\python.exe kolejka.py --root 'D:\Transcriptions' status
```

Existing databases and results are not moved automatically. To reuse a data directory, check the model and tool paths in its `ustawienia.json` first. Each database allows one worker at a time. An existing installation can remember its data directory in the local, Git-ignored `lokalna-instalacja.json` file using the `root` field. The `--root` argument and `KOLEJKA_ROOT` environment variable take precedence.

## Local recordings

The folder picker imports recordings as soon as you confirm the selected folder. To scan a pasted or saved path, use the adjacent import button or press Enter in the path field. You can also add individual files. `Uruchom-kolejke-lokalna.vbs` opens the local queue with the last selected folder.

The subfolder option scans every level when you next add a folder. Changing it alone does not import files. M4A-only limits that scan to M4A; otherwise it includes all supported audio and video formats. Scanning skips the application's result folders, symbolic links, and junctions, and reports inaccessible directories. Confirmed Python environments, installed package directories and textual TypeScript, CMake and Qt `.ts` files are excluded by default. **Pliki techniczne** includes them on the next scan; explicitly adding an individual file also allows an intentional test recording. MPEG transport streams remain supported.

After import, the list immediately shows and selects the matching jobs, including existing ones. The summary distinguishes file paths, new unique recordings, existing jobs and duplicate copies. It reports separate causes for media validation, output paths and access failures; **Raport importu** opens the complete JSON report. An empty scan shows an explicit message. Files with the same SHA-256 share one queue entry, even if their names differ. Reimporting does not reset completed work or failed jobs; retry repairable failures and start the queue manually. Files confirmed to contain no audio remain **Bez ścieżki audio** and require a different source, rather than repeated processing.

See the [folder import and UVR timing fixes (Polish)](docs/NAPRAWA-2026-10-11.md) for the regression cases and local validation.

Choose an output destination before importing. By default, each new recording receives a separate subfolder beside its source, with a short title and stable ID. You can change the destination of selected jobs that have not started. Completed or partially processed results are not moved. FFmpeg handles audio and video, including WAV, MP3, M4A/AAC, FLAC, OGG/Opus, MKV, MP4, WebM, MOV, and AVI. Files without audio, damaged files, and decoding failures receive a specific error.

New folder and export names share a Windows path budget. Longer source directories shorten the title portion while keeping the recording ID, partial marker and export version; existing filenames remain unchanged. If a destination is still too long, the recording stays visible as **Wybierz miejsce wyników**. Select a shorter central or custom destination and apply **Zmień miejsce wyników**. Processing does not start until the destination is valid. A deep duplicate of an existing recording keeps the original job's output location without requiring a new destination.

Starting the local queue does not start YouTube jobs. If another session is active, finish its current recording and stop it before switching queues. Resuming reuses the existing checkpoints and results; closing the window leaves the worker running. Vocal separation and audio retention settings are shared by both queues.

**Start/Wznów lokalne** includes all enabled local jobs, regardless of the table selection. **Start zaznaczonych (N)** includes only the selection. The session banner shows its scope and remaining recordings. A completed job, file error, or partial result advances to the next recording. Missing shared models, exhausted memory, insufficient disk space, and a user stop request pause the session.

UVR uses the selected audio track's sample positions on the video clock. A successfully decoded source track that ends before the video is padded with silence; missing model output is never stretched or silently replaced with silence. If UVR truncates vocals, valid blocks and a verified prefix are retained, followed by one recovery pass over the missing tail in 60-second blocks with the normal context overlap.

If recovery still fails, the valid vocal prefix is transcribed and exported as **Częściowo gotowe**. Partial TXT, SRT, VTT, and JSON filenames include `_czesciowa`; their JSON and `czesciowe.json` marker identify the full duration, processed boundary, and error. `gotowe.json` is reserved for verified complete exports. Audio and checkpoints stay available. Every later Start attempts each enabled partial recording once, then continues to the next job if it fails again. Unchanged ASR and diarization blocks are reused by audio content; extending a tail only recomputes new blocks and affected context boundaries. Recordings with no usable vocal prefix remain errors.

See the [recovery implementation and local validation notes](docs/RECOVERY-2026-10-13.md).

To open the local tab with a chosen folder, without importing or starting processing:

```powershell
.\.venv\Scripts\python.exe kolejka.py gui --local-folder 'D:\Recordings'
```

## YouTube access and outputs

Downloads are anonymous by default. The access dialog detects Chrome and Edge and offers a dedicated browser window for the application. The login button selects this option automatically when no session has been chosen. Sign in once in that window, open the video, and leave the window open or minimized. The test and downloader read the current YouTube session from that local window; no manual `cookies.txt` export is needed. The browser keeps its own profile beside the queue settings, separate from your regular browser profiles. Closing the window requires reopening it before resuming YouTube downloads.

The same screen also lists existing Firefox/Chrome/Edge profiles. Advanced settings retain manual profile paths and Netscape-format `cookies.txt` imports. Profile discovery does not read cookies; session access happens when you explicitly test a link or start processing. Only YouTube cookies are passed to the downloader, held in memory and excluded from application logs and reports. A successful test can be confirmed with one button without starting the queue. YouTube access settings do not block the local queue.

Sign in and complete any YouTube confirmation in your browser. A session does not guarantee that account or IP restrictions will be lifted. If browser access fails and Firefox is available, the dialog offers a button to select its profile and open the login page. See the [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).

Each recording has its own directory, with output filenames containing its YouTube ID or local content hash. A job is marked complete only after the full set of exports has been written and verified. Review the recognized text, language, and speaker labels: music, overlapping voices, and block boundaries can cause recognition errors.

The transcription, diarization, and vocal separation stages process audio locally. Keep cookies and Hugging Face tokens out of the repository. Model weights and third-party libraries have their own licenses and access conditions.

## Tests and single-file transcription

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe transcribe.py transcribe 'D:\Recordings\lecture.mp4' --language auto --word-timestamps
.\.venv\Scripts\python.exe transcribe.py transcribe --help
```

The test suite uses synthetic data and temporary directories. YouTube downloads and neural model calls are replaced with controlled test doubles. FFmpeg integration tests use generated audio and video; these tests are skipped if the tools are unavailable. Windows CI installs the pinned application and test dependencies plus FFmpeg 8.0.1, then runs the same offline suite. Local installations using FFmpeg 9 keep their version. Tests with real model weights and recordings are separate from the offline suite and are documented in the [validation notes](docs/WERYFIKACJA.md).

SonarQube Cloud uses automatic analysis configured in [.sonarcloud.properties](.sonarcloud.properties). The Python versions are 3.12 and 3.13, matching the installed environments. Test files are explicitly listed under `sonar.tests` and excluded from the production scope. When adding a test file, update both lists in that configuration. Production files remain in scope; no rules are suppressed.

## Repository history

The former ASP.NET Core XML CRUD exercise remains available in [Git history](https://github.com/haribo841/kolejka-transkrypcji/tree/0b078d310db7922928e4ffe67f9c6121dd16570b). Its migration was published on 7 October 2026 in [Evaluation-tasks/Evaluation-task1](https://github.com/haribo841/Evaluation-tasks/tree/025cbdcd308ebb929f53b4b5244f830eec096038/Evaluation-task1), alongside tasks 2 and 3.

The archive includes the [original CRUD README](docs/archive/README-aspnetcore-xml-crud-2026-10-06.md), the [queue README before the local tab](docs/archive/README-kolejka-2026-10-08.md), and the [README before this English rewrite](docs/archive/README-kolejka-2026-10-11.md). See the [migration notes](docs/MIGRACJA.md) for details.
