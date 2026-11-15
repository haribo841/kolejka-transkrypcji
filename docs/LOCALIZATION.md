# Polish and English YouTube localizations

The **Lokalizacje YouTube** tab tracks metadata, subtitles and retained audio independently of transcription. A completed Whisper job is a source asset, not proof that a translation has been reviewed or published.

## First setup

Open **Kreator lokalizacji** in the new tab or Configuration. Complete one step at a time; **Dalej** becomes available when the current operation finishes.

1. In Google Cloud, create or select a project, enable **YouTube Data API v3**, configure the OAuth consent screen and add your Google account as a test user when using Testing mode. Create an OAuth client of type **Desktop app** and download its JSON. Import that file in the wizard.
2. Click **Połącz konto**, sign in through the system browser and grant access. The application uses PKCE with a loopback callback and stores the refresh token in Windows Credential Manager. Browser cookies configured for media downloads serve a separate purpose.
   If Google rejects the attempt without returning to the application, use **Ponów logowanie** in the same wizard. This cancels the previous wait before opening a new attempt. **Anuluj oczekiwanie na logowanie** releases the waiting state without closing the window. Other step actions are disabled while their work is in progress.
3. Load and select the channel to edit. Every publication checks channel ownership.
4. Install the isolated Python 3.12 translation environment. The equivalent command is `./Instaluj-tlumaczenia.ps1` in PowerShell.
5. Download and convert the pinned `google/madlad400-3b-mt` revision to CTranslate2 INT8. Conversion needs about 12 GB for the source weights plus working space. The final model runs on CPU. Downloads reuse a managed workspace in the system temporary directory. Conversion uses a private folder under the data root's `modele` directory, then publishes `modele/madlad400-3b-ct2` without overwriting an existing model. Setup selects its own workspace and rejects model destinations outside this folder before downloading.
6. Run the local translation trial and inspect its results. This does not start either production queue or publish to YouTube.
7. Check the actual title and description of one selected video. This is a separate check from the model trial. The wizard displays the retrieved text and distinguishes authenticated API access from a public preview.

You may choose **Pomiń Google (bez publikacji)** to configure local processing first. Without an OAuth client, a saved account credential and a selected channel, the final button explicitly ends setup in local mode. A passed translation trial does not imply that Google is connected. Google can expire or revoke access; use **Połącz ponownie** when prompted. OAuth projects in Testing mode may require renewed consent.

## Prepare and review a batch

Select videos using Ctrl/Shift. **Odśwież z YouTube** refreshes selected videos and caption-track information through the API when Google is configured. With no selection, it refreshes the channel upload catalog and metadata, including available private uploads for the authorized owner. Without Google, select videos to read their public titles and descriptions anonymously through yt-dlp. This does not download media or establish a publication baseline, and cannot confirm other localizations or account ownership. A full catalog refresh and publication require Google setup. The original XLSX remains unchanged.

The wizard's metadata trial covers one video. Refresh the main tab afterward for the rest of your selection or channel catalog. A refresh clicked during an automatic table read is queued and runs once that read finishes. The button changes to **Odświeżanie…** and remains busy while the detached session runs. The persistent action message and session progress are displayed separately, so table updates do not erase an accepted command or error.

Before a remote read, the source preview shows the catalog title and explicitly says that the description has not been retrieved. A retrieved empty description is labelled separately. Polish and English draft fields remain empty until you prepare translations or enter text yourself. The selected video's source and prepared fields refresh when a background operation finishes, preserving unsaved corrections. If the same field changes in the background while you edit it, **Zapisz teksty** asks before replacing that newer version and retains its history.

Choose the preparation tasks, then click **Przygotuj zaznaczone**. The session stores its IDs and order. New imports, filtering or sorting do not change an active batch. **Dokończ bieżący i zatrzymaj** finishes the current video. Closing the application window leaves the detached worker running. **Wznów zapisaną sesję** resumes its durable cursor after interruption; a new preparation can also reuse completed translation and separation checkpoints.

The application prepares:

- A Polish primary title and description, plus an English localization. Original fields and existing translations are retained for comparison. Source-language overrides are separate for title and description; a shared glossary protects names. URLs, chapter times, hashtags and numbers are kept unchanged.
- Polish and English SRT subtitles using existing transcription JSON/SRT. Speaker labels are removed from publication copies. Corrections retain cue IDs and times. The editor rejects missing cues, invalid times and cues outside the recording. It flags excessive subtitle length or reading speed.
- An optional native **Instrumental** stem from the retained original audio. This removes all voices, including game dialogue or singing. It is not a narrator-specific separator. Listen before approving: voice leakage and damaged effects are possible. The original mix and the existing vocal file are preserved.

Empty descriptions require deliberate approval. Speech-free recordings can be marked as not requiring subtitles. Polish and English dubbing remain **Odłożone**; this version does not synthesize voices.

If the underlying speech recognition is incorrect, click **Transkrybuj ponownie…** in the subtitle editor or transcription queue. This reruns Whisper for one recording with fresh checkpoints, using the retained original mix or vocal and a selectable speech language. It does not translate text in the editor. Previous transcripts and caption revisions are retained. After success, PL/EN captions show **Brak** and appear under **Do przygotowania**: select the film, enable captions in the preparation tasks, prepare them again, then review and approve. Titles, descriptions and content already published on YouTube are unchanged. An editor opened before the new transcription cannot overwrite its replacement subtitles.

New files live in `lokalizacje/<video-id>` under the data root. Preparation, approval and publication are separate states. Editing a field revokes its approval; older field revisions remain in the database history. Synchronization preserves local text while flagging relevant remote changes. The CSV report lists each asset and its current problem or remaining step.

## Publish reviewed versions

Approve each title, description and subtitle after reviewing it. A metadata language pair needs both its title and description reviewed. Open **Podgląd zmian** to compare existing and proposed values. **Opublikuj zatwierdzone** opens a preview with a separate confirmation button and freezes the approved content hashes for that session.

The writer preserves other localizations, category, tags and audio-language settings. The primary metadata language becomes Polish; the audio language is independent. It re-reads remote state and uses conditional updates. A conflict requires a fresh preview. Each operation is journaled before sending, and retry checks remote state before another upload. Caption track IDs and application-specific names prevent duplicate uploads after timeouts. A track still processing in YouTube is not shown as confirmed publication.

The default application budget is **9,000 API units per Pacific day**, leaving a reserve against Google's default allocation. Caption insertion costs 400 units, updates 450, caption listing 50 and content verification downloads 200. Metadata updates cost 50; reads and failed requests also use quota. A fully verified two-language video can therefore cost more than 850 units. The local counter covers this application; other clients using the same Google project can consume additional quota. Exhaustion pauses publication without repeating local inference. Resume manually when quota is available.

Audio is managed through files and YouTube Studio. A voiceless stem must not be labelled Polish or English. If Studio has no suitable option, keep it as a local asset. Only use **Potwierdź publikację audio** after actually uploading the approved track; its status explicitly says it was confirmed manually.

## Privacy and recovery

Whisper, UVR and translation run locally after their models are downloaded. Publication uses the official Google API. Refresh credentials are stored in Windows Credential Manager, not in SQLite, request files, logs or the repository. The imported Desktop client JSON remains local. Preparation shares the compute lock with transcription and model trials to avoid competing heavy workers.

On a problem, inspect the per-video details and `lokalizacje/worker.log`. Translation and UVR logs live in the video's separate workspace. Completed transcription files and original media are never overwritten by localization.

References: [Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app), [video metadata updates](https://developers.google.com/youtube/data/reference/rest/v3/videos/update), [caption uploads](https://developers.google.com/youtube/data/reference/rest/v3/captions/insert), [quota overview](https://developers.google.com/youtube/data/getting-started#quota-usage), [MADLAD model](https://huggingface.co/google/madlad400-3b-mt).
