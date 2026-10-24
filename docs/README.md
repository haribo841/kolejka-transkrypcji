# Documentation

Kolejka transkrypcji is a Windows application for local speech transcription, subtitle export and resumable batch processing. The interface is in Polish. The guides below explain the workflow in English and identify the labels used in the app.

## Start here

| Guide | What you will find |
| --- | --- |
| [Getting started](GETTING_STARTED.md) | Download, prerequisites, first sample test, local and YouTube queues, optional models, results and troubleshooting |
| [Architecture](ARCHITECTURE.md) | Component map, data flow, queue state, recovery semantics, process boundaries and development checks |
| [Example TXT](examples/demo-transcript.txt) / [example SRT](examples/demo-transcript.srt) | Illustrative exports produced with the application's exporter |
| [Detailed user guide (Polish)](../KOLEJKA.md) | Extended operating notes and CLI examples; consult the English guide for the current setup flow |

[Back to the product page](../README.md) · [Report an issue](https://github.com/haribo841/kolejka-transkrypcji/issues) · [MIT license](../LICENSE)

## Implementation and validation history

These are dated engineering notes, not a substitute for the current getting-started guide:

- [Runtime validation](WERYFIKACJA.md) (Polish): model trials and worker recovery exercises.
- [Sorting and session order, 14 October 2026](SORTING-2026-10-14.md).
- [UVR recovery, 13 October 2026](RECOVERY-2026-10-13.md).
- [Folder import, 13 October 2026](IMPORT-2026-10-13.md).
- [Media and output-path fixes, 11 October 2026](NAPRAWA-2026-10-11.md).
- [Repository migration](MIGRACJA.md) (Polish): the former ASP.NET Core exercise and its move to Evaluation-tasks.

## README archive

[README before the product-page rewrite, 24 October 2026](archive/README-kolejka-2026-10-24.md) is an exact copy of the previous file. Its links retain their original paths; the [original revision on GitHub](https://github.com/haribo841/kolejka-transkrypcji/blob/d745e8fe6b2bf76470660f1394c09cd35d2e5151/README.md) keeps their original repository context.

Earlier snapshots are preserved as the [11 October README](archive/README-kolejka-2026-10-11.md), [8 October README](archive/README-kolejka-2026-10-08.md) and [original ASP.NET Core README](archive/README-aspnetcore-xml-crud-2026-10-06.md).

## Screenshots

The product page uses captures of the actual Tkinter application with an isolated demonstration queue and illustrative transcript. No production queue or account is used. See [screenshot provenance](assets/readme/README.md) for the source revision and what each image demonstrates.
