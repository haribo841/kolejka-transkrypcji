# Kolejka transkrypcji

Aplikacja Windows napisana w Pythonie, z oknem Tkinter/ttk. Przetwarza kolejkę filmów YouTube oraz lokalnych nagrań audio i wideo: pobiera audio, opcjonalnie wydziela wokal, transkrybuje lokalnie przez Whisper/OpenVINO i rozróżnia mówców. Postęp zapisuje w SQLite, a ukończone wyniki w TXT, SRT, VTT i JSON.

[Instrukcja obsługi](KOLEJKA.md) | [Walidacja](docs/WERYFIKACJA.md) | [Migracja repozytorium](docs/MIGRACJA.md) | [Licencja MIT](LICENSE)

## Funkcje

- Import katalogu YouTube z XLSX, plików lokalnych i folderów z podfolderami. Ponowny import pomija duplikaty i zachowuje ukończone pozycje.
- Osobne karty Kolejka YouTube i Kolejka lokalna. Start w karcie obejmuje tylko jej nagrania; opcja zaznaczonych ogranicza sesję bez wyłączania innych pozycji.
- Nawigacja do bieżącego lub pierwszego oczekującego nagrania, czas trwania oraz zachowanie przewijania i zaznaczenia przy odświeżaniu.
- Karta Konfiguracja z kontrolą plików i środowisk w tle oraz datowaną próbą do 60 sekund, uwzględniającą aktualne opcje UVR i mówców.
- Wybór miejsca wyników lokalnych: własny podfolder obok źródła, folder centralny lub wskazany folder. Istniejące zadania zachowują swoje miejsca.
- Jeden materiał naraz, kolejność chronologiczna, filtry, wybór języka i ścieżki audio.
- Start/Wznów oraz Dokończ bieżący i zatrzymaj. Zamknięcie okna pozostawia wykonawcę w tle; aplikację można ponownie otworzyć.
- Punkty wznowienia transkrypcji, diarizacji i UVR. Po awarii ukończone bloki są sprawdzane i wykorzystywane ponownie.
- Whisper large-v3-turbo przez OpenVINO GenAI na CPU, czasy słów, pyannote Community-1 i opcjonalny UVR przez audio-separator.
- Opcjonalne zachowanie źródłowego audio i wydzielonego wokalu. Oryginalne pliki lokalne pozostają na miejscu.
- Nazwy wyników zawierające identyfikator nagrania, zapis atomowy i wersjonowanie przy zajętej nazwie.
- Karta Obróbka transkrypcji tworząca kopie TXT/SRT/VTT bez etykiet mówców, z zachowanymi czasami i wypowiedziami.
- Podpowiedzi przycisków, eksport raportu CSV i konfiguracja opcjonalnej sesji YouTube z testem jednego linku.

Repozytorium zawiera kod i przypięte listy zależności. Modele, środowiska Python, prywatne katalogi filmów, pliki cookies, tokeny i wyniki przetwarzania przygotowuje się lokalnie. Transkrypcja działa bez sterowania oknem Audacity.

## Wymagania

- Windows 10/11, Python 3.13 dla głównego środowiska oraz Python 3.12 dla mówców i opcjonalnego UVR. Instalatory korzystają z launchera `py`; można podać własne ścieżki do interpreterów.
- FFmpeg i FFprobe 9 oraz Node.js 22 dostępne w `PATH` albo wskazane pełnymi ścieżkami w ustawieniach.
- Miejsce na modele oraz audio robocze. Mono PCM 16 kHz zajmuje około 64 kB na sekundę; pozostaw dodatkowe miejsce na UVR i zachowywane nagrania.
- Internet do jednorazowego pobrania wag i do materiałów YouTube. Community-1 wymaga zaakceptowania warunków modelu oraz tokenu Hugging Face z uprawnieniami Read.

Whisper domyślnie działa na CPU. Osobne środowiska mówców/UVR mają PyTorch CUDA 12.6. Dostępność GPU jest sprawdzana podczas obliczeń; nieudane wykonanie przechodzi na CPU z komunikatem. Wykrycie urządzenia nie gwarantuje zgodności wszystkich modeli i sterowników.

## Instalacja i pierwsza próba

W PowerShell otwórz folder sklonowanego repozytorium:

```powershell
.\Instaluj-zaleznosci.ps1
.\.venv\Scripts\python.exe transcribe.py download
# Opcjonalna separacja wokalu:
.\Instaluj-UVR.ps1
.\Uruchom-kolejke.vbs
```

Instalatory nie podnoszą wersji bibliotek ponad pliki `requirements-*.lock.txt`. Wariant z własnymi interpreterami:

```powershell
.\Instaluj-zaleznosci.ps1 -AsrInterpreter 'C:\Python313\python.exe' -DiarizationInterpreter 'C:\Python312\python.exe'
.\Instaluj-UVR.ps1 -Interpreter 'C:\Python312\python.exe'
```

W karcie Konfiguracja wybierz Konfiguracja Community-1, zaakceptuj dostęp do [Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1), sprawdź token Read i pobierz model. Ustaw UVR oraz mówców, a następnie kliknij Sprawdź na fragmencie. Możesz wskazać długie nagranie; program przetworzy do 60 sekund. Sprawdź tekst i etykiety mówców przed dużą sesją. Import nie rozpoczyna pracy; kolejkę uruchamia przycisk Start/Wznów.

Domyślny folder danych to `%USERPROFILE%\Transkrypcje`, poza repozytorium. Inny folder wybiera się przez `KOLEJKA_ROOT` przed uruchomieniem albo parametr `--root`:

```powershell
.\.venv\Scripts\python.exe kolejka.py --root 'D:\Transkrypcje' gui
.\.venv\Scripts\python.exe kolejka.py --root 'D:\Transkrypcje' status
```

Nie ma automatycznego przenoszenia wcześniejszej bazy ani gotowych wyników. Istniejący folder danych można wskazać świadomie po sprawdzeniu ścieżek modeli i narzędzi w jego `ustawienia.json`. Jedna baza dopuszcza jednego wykonawcę. Aktualizowana instalacja może zapamiętać dotychczasową bazę w lokalnym, ignorowanym przez Git pliku `lokalna-instalacja.json` z polem `root`. Parametr `--root` i zmienna `KOLEJKA_ROOT` mają pierwszeństwo.

## Kolejka lokalna

W karcie **Kolejka lokalna** wybierz folder i kliknij **Dodaj folder**, albo dodaj pojedyncze pliki. Skrót **Uruchom-kolejke-lokalna.vbs** otwiera tę kartę z ostatnio wybranym folderem. Możesz uwzględnić podfoldery lub ograniczyć import do M4A. Lista, liczniki, raport CSV i ponawianie błędów obejmują wyłącznie lokalne nagrania. Kopie o tym samym SHA-256 zajmują jedną pozycję w kolejce; oryginały i istniejące transkrypcje pozostają na miejscu.

Przed importem wybierz miejsce wyników. Domyślnie nowy plik dostaje osobny podfolder obok źródła, z krótkim tytułem i stabilnym ID. Dla zaznaczonych, nierozpoczętych zadań służy **Zmień miejsce wyników**. Nie przenosi gotowych ani częściowych wyników. FFmpeg obsługuje audio i wideo, w tym WAV, MP3, M4A/AAC, FLAC, OGG/Opus, MKV, MP4, WebM, MOV i AVI; pliki bez audio lub uszkodzone dostają osobny komunikat.

**Start/Wznów lokalne** nie uruchamia materiałów YouTube. Gdy trwa inna sesja, dokończ jej bieżące nagranie i zatrzymaj ją przed przełączeniem. Wznowienie wykorzystuje te same punkty kontrolne i wyniki, a zamknięcie okna nie przerywa wykonawcy. Opcje UVR i zachowywania audio są wspólne dla obu kolejek. Przykład otwarcia karty z wybranym folderem, bez importu lub automatycznego Start:

```powershell
.\.venv\Scripts\python.exe kolejka.py gui --local-folder 'D:\Nagrania'
```

## YouTube i wyniki

Pobieranie domyślnie działa anonimowo. Gdy YouTube żąda potwierdzenia, Dostęp YouTube pozwala otworzyć link w przeglądarce, wybrać sesję Firefoksa/Chrome/Edge lub lokalny plik Netscape `cookies.txt` i przetestować jeden film. Logowanie wykonuje użytkownik w przeglądarce. Sesja nie gwarantuje usunięcia ograniczenia konta lub adresu IP. [Dokumentacja yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).

Każde nagranie otrzymuje osobny katalog i nazwy z ID YouTube lub skrótem pliku lokalnego. Stan Gotowe jest nadawany po sprawdzeniu kompletu zapisanych wyników. Rozpoznany tekst, język i mówcy wymagają oceny użytkownika; muzyka, nakładanie głosów i przejścia między blokami mogą powodować błędy.

Lokalne etapy ASR, mówców i UVR nie wysyłają audio do chmury. Cookies oraz token Hugging Face nie powinny trafiać do repozytorium. Wagi modeli i biblioteki mają własne licencje i warunki dostępu.

## Testy i pojedyncza transkrypcja

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe transcribe.py transcribe 'D:\Nagrania\wyklad.mp4' --language auto --word-timestamps
.\.venv\Scripts\python.exe transcribe.py transcribe --help
```

Testy używają danych syntetycznych i katalogów tymczasowych. Pobieranie YouTube, model ASR i model mówców są zastępowane kontrolowanymi atrapami; próby FFmpeg używają syntetycznego audio i są pomijane, gdy narzędzia nie są zainstalowane. CI na Windows instaluje główny lockfile i FFmpeg 8.0.1 oraz uruchamia tę samą serię offline. Lokalna instalacja z FFmpeg 9 zachowuje swoją wersję. Dłuższe testy infrastruktury wymagają jawnego wskazania lokalnego modelu oraz własnej próbki i są opisane w [walidacji](docs/WERYFIKACJA.md).

## Historia

Dawne ćwiczenie ASP.NET Core XML CRUD jest zachowane w [historii Git](https://github.com/haribo841/kolejka-transkrypcji/tree/0b078d310db7922928e4ffe67f9c6121dd16570b). Jego migrację opublikowano 07.10.2026 w [Evaluation-tasks/Evaluation-task1](https://github.com/haribo841/Evaluation-tasks/tree/025cbdcd308ebb929f53b4b5244f830eec096038/Evaluation-task1), obok zadań 2 i 3. Archiwum zawiera [README dawnego CRUD](docs/archive/README-aspnetcore-xml-crud-2026-10-06.md) oraz [README Kolejki sprzed dodania karty lokalnej](docs/archive/README-kolejka-2026-10-08.md). Szczegóły przenoszenia znajdują się w [opisie migracji](docs/MIGRACJA.md).
