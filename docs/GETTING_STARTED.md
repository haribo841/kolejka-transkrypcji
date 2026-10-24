# Getting started

[Product page](../README.md) · [Documentation index](README.md) · [Architecture](ARCHITECTURE.md)

## Prerequisites

| Requirement | When it is needed |
| --- | --- |
| Windows 10 or 11 | Supported desktop platform; the UI is in Polish |
| Python 3.13 with Tkinter and the `py` launcher | Main application and Whisper environment |
| FFmpeg and FFprobe | All audio/video decoding; local development uses version 9 and Windows CI uses 8.0.1 |
| Python 3.12 | Optional speaker diarization and UVR environments |
| Node.js 22 | YouTube downloads through yt-dlp/EJS |
| Internet connection | Source/dependency/model downloads and YouTube access; local model inference is offline |
| Free disk space | Model weights, working audio and any recordings you choose to retain |

Make FFmpeg/FFprobe available on `PATH`, or enter their full paths in **Konfiguracja**. Whisper large-v3-turbo is downloaded separately, approximately 1.63 GB. Temporary mono float32 audio uses about 230 MB per hour; UVR and retained source audio need additional space.

Whisper uses CPU by default. The optional speaker and UVR environments can use CUDA, with an execution check and CPU fallback. Hardware detection alone is not a performance or compatibility guarantee.

## Download and install

1. [Download the source ZIP](https://github.com/haribo841/kolejka-transkrypcji/archive/refs/heads/master.zip) and extract it. Alternatively, clone `https://github.com/haribo841/kolejka-transkrypcji.git`.
2. Open PowerShell in the extracted repository folder.
3. For the simplest local transcription setup, run:

   ```powershell
   .\Instaluj-zaleznosci.ps1 -WithoutSpeakers
   .\Uruchom-kolejke-lokalna.vbs
   ```

The script creates `.venv` and installs pinned dependencies. It does not install Python, FFmpeg, Node.js or model weights. `-WithoutSpeakers` skips the separate speaker environment; disable speaker labels in the wizard as described below.

To use an explicit interpreter:

```powershell
.\Instaluj-zaleznosci.ps1 -WithoutSpeakers -AsrInterpreter 'C:\Python313\python.exe'
```

The application is currently distributed as source code with launchers, not a standalone `.exe` installer. After setup, double-click `Uruchom-kolejke.vbs` or `Uruchom-kolejke-lokalna.vbs` to reopen it.

## Configure and test once

Open **Konfiguracja → Kreator krok po kroku**:

1. **Whisper and tools:** download the pinned Whisper model, or select an existing complete model folder. Wait for the operation to finish.
2. **UVR:** leave vocal separation off for the first test.
3. **Speakers:** turn off **Rozróżniaj mówców przez Community-1** for the basic setup.
4. **YouTube:** skip this step if you only want local files.
5. **Sample test:** select a recording with speech. The wizard processes up to 60 seconds with the current options and saves a dated report and exports. Review the text before starting a larger session.

The Next button waits while preparation or a test is running. A component being available means it is ready for a test; a passed sample test confirms the current combination of settings. Importing recordings or finishing the wizard does not start the production queue.

If preferred, Whisper can be downloaded before opening the wizard:

```powershell
.\.venv\Scripts\python.exe transcribe.py download
```

## Process local recordings

1. Open **Kolejka lokalna**.
2. Choose **Wyniki nowych nagrań** before importing: a subfolder beside each source, the central results directory, or a custom folder.
3. Use **Dodaj pliki** for individual recordings. **Dodaj folder...** chooses and imports a folder; **Dodaj z podanej ścieżki** imports the path typed into the adjacent field.
4. **Podfoldery** scans all levels on the next folder import. **Tylko M4A** restricts that scan. Leave **Pliki techniczne** off for normal recordings; generated outputs, linked folders and confirmed technical files are skipped by default.
5. Search, filter or click a column heading to choose the view and order. Heading clicks cycle through ascending, descending and the original chronological order.
6. Click **Start/Wznów widoczne (N)** for eligible, enabled recordings in the current view, or **Start zaznaczonych (N)** for the visible selected subset.

At Start, the app saves the ordered job IDs for that session. Later view changes and new imports affect the next session. A file error does not prevent other valid recordings from being processed; shared resource failures pause the session.

**Dokończ bieżący i zatrzymaj** finishes the entire current recording and then stops. Closing the window leaves the worker running. Reopening the same data directory reconnects to its progress. Start is always manual.

## Optional speaker labels and vocal separation

To add speaker diarization, install its environment by rerunning the main installer without `-WithoutSpeakers`:

```powershell
.\Instaluj-zaleznosci.ps1
```

In the wizard, enable speaker labels and open the Community-1 setup. Accept the access conditions on the [Community-1 model page](https://huggingface.co/pyannote/speaker-diarization-community-1), verify a Hugging Face **Read** token in the local dialog, and download the model. The token is for setup, not something to paste into an issue report.

To add vocal separation:

```powershell
.\Instaluj-UVR.ps1
```

Enable UVR in the wizard and prepare its model. UVR uses a separate Python 3.12 environment and preserves both the source audio and separated vocal track. It can help with background music, but does not guarantee better transcription or remove every unwanted voice. Run another sample test whenever you change these options.

## YouTube queue

Import a catalog with **Importuj katalog YouTube**. The importer reads the **Materiały** worksheet, recognizes its column headers and uses video IDs to identify existing jobs. Import alone does not download or transcribe anything.

Anonymous downloads are the default. If YouTube requests confirmation, open **Dostęp YouTube**, choose the application's dedicated Chrome/Edge window, sign in yourself and test one video link. Keep that browser window open or minimized while the session is used. Existing profiles and a YouTube-only Netscape `cookies.txt` file are available as advanced alternatives.

Account or network restrictions can still block a download. A rate-limit or anti-bot response pauses the queue; it is not a signal to repeatedly click Start. Local recordings do not depend on YouTube access. See [yt-dlp's YouTube access documentation](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).

## Find and edit your results

Each recording has its own output folder and unique filenames. Use **Otwórz wyniki** or **Otwórz TXT** for the selected recording:

| Format | Use |
| --- | --- |
| TXT | Readable transcript with times and speaker labels |
| SRT / VTT | Subtitle cues for players and editors |
| JSON | Words, segments, speakers, model settings and recovery metadata |

See the [illustrative TXT](examples/demo-transcript.txt) and [SRT](examples/demo-transcript.srt). Automatic text and speaker labels need review, especially for music, overlap or technical vocabulary.

Use **Obróbka transkrypcji** to load existing TXT/SRT/VTT files and preview copies without speaker labels. Saving preserves timestamps and originals, adding a suffix and a version number when needed.

The default queue data directory is `%USERPROFILE%\Transkrypcje`, outside the repository. Choose another directory with `KOLEJKA_ROOT` before launching, or with `--root`:

```powershell
.\.venv\Scripts\python.exe kolejka.py --root 'D:\Transcriptions' gui
```

This selects a queue directory; it does not move an existing database or results. New local jobs remember their own output destinations. Source recordings are never deleted by queue cleanup.

## When something needs attention

| Situation | What to do |
| --- | --- |
| Whisper is incomplete, or a tool is missing | Open **Konfiguracja**, correct the model/tool path or complete its download, then repeat the sample test |
| A previous sample test no longer matches the settings | Test a speech fragment again with the current UVR and speaker options |
| A file has moved | Reimport its current location; matching SHA-256 content updates the existing identity without resetting completed work |
| Disk space or a shared model prevents the session from continuing | Correct that cause, then press Start again; existing verified blocks are reused |
| UVR produces a usable but incomplete prefix | The app retains a partial result and checkpoints; a later Start attempts the missing part once |
| A source is permanently unreadable or is technical test data | It is archived as **Pominięte**, with a reason; it is hidden from the default view and not retried automatically |
| Damaged source audio can be recovered | Processing finishes with a warning after available audio is handled; JSON records that source completeness could not be confirmed |

**Raport importu**, **Otwórz log błędu** and the CSV report provide more detail. When [reporting an issue](https://github.com/haribo841/kolejka-transkrypcji/issues), include the step, file format and redacted error text; leave account sessions and tokens out.
