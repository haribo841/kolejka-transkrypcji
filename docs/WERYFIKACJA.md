# Walidacja samodzielnego checkoutu

11.10.2026: naprawiono siedem porównań oczekiwanych ścieżek przez `resolve()`, zachowując aliasy w danych wejściowych. Ponownie odtworzono pięć błędów poprzedniej publikacji przy `TEMP` i `TMP` wskazujących rzeczywisty alias Windows 8.3. Poprawiona pełna seria: 161 testów bez pominięć z FFmpeg 8.0.1. Przechodzą kompilacja, `pip check` i kontrola białych znaków. Przygotowano też poprawki pozostałych trzech S1192 i jednego S108. Wynik zdalnego CI i SonarCloud wymaga sprawdzenia po publikacji dla nowego SHA. Szczegóły: [raport napraw](SONAR-2026-10-10.md).

10.10.2026: opublikowano `18f94c6960a2064d92fcdf70a5f6edca3fae0ddb` z czterema poprawkami SonarCloud i zaakceptowaną rozbudową UI. SonarCloud potwierdził zielony Quality Gate i niezawodność A. GitHub Actions wykazało pięć błędnych porównań aliasów ścieżek Windows w nowych testach. Poprawkę CI przygotowano lokalnie: odtworzono pięć niepowodzeń na aliasie 8.3, a po korekcie przechodziło 161 testów bez pominięć z FFmpeg 8.0.1. Tego dnia poprawka nie została opublikowana z powodu dziennego limitu.

09.10.2026, usprawnienia UI: lokalnie wdrożono kartę Konfiguracja, nawigację, wybór miejsca wyników i kontrolę formatów. W repozytorium oraz instalacji przechodzą 157 testów bez pominięć. Oba MKV przeszły próby z UVR i bez niego; po wdrożeniu przeszła dodatkowa próba 60 sekund. Zachowano 5059 pozycji i zatrzymaną kolejkę. Zmiany nie zostały ponownie opublikowane. Szczegóły i granice walidacji: [raport UI](UI-2026-10-09.md).

09.10.2026: opublikowano `c0997819b72c903942eeb02904fbdc15d9e1e9e1`. [GitHub Actions](https://github.com/haribo841/kolejka-transkrypcji/actions/runs/37916813578) przeszło: 132 testy, trzy jawne pominięcia prób FFmpeg na runnerze. SonarCloud analizował ten sam commit: liczba zgłoszeń spadła z 72 do 4, bramka bezpieczeństwa przeszła, ale bramka niezawodności pozostaje negatywna. Drobne poprawki czterech pozostałych uwag przygotowano lokalnie, bez kolejnego commitu i pushu. Przechodzi dla nich 132 testy bez pominięć. Szczegóły: [raport napraw](SONAR-2026-10-08.md).

08.10.2026, po publikacji: GitHub Actions zgłosił dwie błędne asercje porównujące alias katalogu Windows ze ścieżką kanoniczną, a SonarCloud odrzucił bramkę bezpieczeństwa. Przygotowano lokalne naprawy i testy regresyjne. Przechodzą 132 testy bez pominięć, także przez alias katalogu tymczasowego, oraz kompilacja, `pip check` i sprawdzenie diff. Szczegóły i granice walidacji: [raport CI i Sonar](SONAR-2026-10-08.md). Poprawki pozostają niezatwierdzone z powodu wykorzystanego dziennego limitu publikacji; status zdalnego CI i Sonar nadal jest negatywny.

08.10.2026: przed publikacją bieżącej wersji ponownie przeszło 111 testów bez pominięć, kompilacja modułów i `pip check`, na Pythonie 3.13.1. Kod zmienionych modułów oraz skrót karty lokalnej są identyczne z obecną instalacją aplikacji. Zachowano bajtową kopię poprzedniego głównego README. Testy używały katalogów tymczasowych i danych syntetycznych; nie uruchamiano produkcyjnej kolejki. Wynik GitHub Actions dla nowego commitu sprawdzany jest osobno po push.

06.10.2026, po dodaniu karty lokalnej: lokalnie przeszło 111 testów tego checkoutu oraz 102 testy zaktualizowanej istniejącej instalacji, bez pominięć. Serie uruchomiono istniejącym interpreterem Python 3.13.1 i jego zainstalowanymi zależnościami. Nie instalowano nowych modeli ani nie uruchamiano pełnej kolejki. Nowe zmiany pozostają lokalne. [GitHub Actions dla wcześniejszego commitu f7cf580](https://github.com/haribo841/kolejka-transkrypcji/actions/runs/37438503418) przeszło, ale nie obejmuje jeszcze rozbudowy karty lokalnej.

Nowe próby obejmują migrację istniejącej bazy SQLite, niezależne zakresy YouTube/lokalne, trwały wybór ID, brak zmiany włączenia innych zadań, pomijanie Node dla plików lokalnych i ochronę przed drugim Start podczas pracy oraz uruchamiania wykonawcy. Testy GUI sprawdzają M4A, podfoldery, SHA-256, import bez Start, ponawianie błędów tylko jednej kolejki oraz zaznaczenia przekazywane do obróbki tekstu. Osobna próba z syntetycznym procesorem potwierdziła zamknięcie i ponowne otwarcie okna podczas pracy oraz zatrzymanie po bieżącym nagraniu. Karta mieści się przy 1360×860 i 1220×760 również z długimi komunikatami.

Zakres obejmuje syntetyczny XLSX z nagłówkami w zmienionej kolejności i hiperłączem, deduplikację, zachowanie źródeł, kolejkę SQLite, zatrzymanie po bieżącym filmie, wznowienie bloków, kontrolę integralności, wersjonowanie eksportu, obróbkę mówców, konfigurację dostępu Hugging Face, cookies wyłącznie YouTube i test jednego linku. Rzeczywisty FFmpeg dekodował syntetyczne audio, wybierał ścieżkę i odrzucał błędny plik. Kontrolki Tk uruchamiano w testowych oknach na katalogach tymczasowych.

Przeszły kompilacja modułów, `pip check`, sprawdzenie składni trzech skryptów PowerShell i parsowanie YAML workflow. Pięć plików zależności jest identycznych bajtowo z wersją istniejącej aplikacji. Sprawdzono wykluczenie środowisk, modeli, plików cookies, konfiguracji, XLSX, baz, audio i wyników przez `.gitignore`. Nie przeprowadzono nowej instalacji zależności ani ponownej próby modeli w samodzielnym checkoutcie. Walidator bez modelu zwrócił jawny SKIP z kodem 2; testy odrzucają ponowne użycie niepustego folderu walidacji.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m compileall -q batch kolejka.py transcribe.py
.\.venv\Scripts\python.exe -m pip check
```

Testy zwykłe nie potrzebują osobistego XLSX, cookies, danych konta, GPU ani pobranych modeli. Trzy próby FFmpeg są jawnie pomijane, gdy brakuje narzędzi. Pominięcie nie jest wynikiem rzeczywistego dekodowania. CI celowo nie loguje się do usług ani nie pobiera wag.

## Próby integracyjne na własnych danych

Po pobraniu Whispera można jawnie uruchomić walidator infrastruktury Windows:

```powershell
.\.venv\Scripts\python.exe validate_batch_runtime.py --root 'D:\TestyKolejki\nowa-proba' --sample 'D:\Nagrania\krotka-mowa.wav' --model '.\models\whisper-large-v3-turbo-fp16-ov'
```

Wybierz nowy, oddzielny folder. Walidator sprawdza zamknięcie i ponowne otwarcie GUI, przerwanie procesu, wznowienie, czas życia procesu podrzędnego oraz trzygodzinną ciszę. Brak modelu lub próbki zwraca SKIP i kod 2 przed rozpoczęciem prób. Wykonanie może zająć czas i miejsce na dysku. Te próby wyłączają diarizację i nie są oceną jakości mówców ani gwarancją transkrypcji wielogodzinnej rozmowy.

Pełny test Whisper + Community-1 + opcjonalny UVR wykonuje przycisk Sprawdź na fragmencie z bieżącymi ustawieniami. Jakość polskiego, angielskiego, muzyki, nakładania głosów i łączenia mówców należy sprawdzić na reprezentatywnych nagraniach. Skuteczność dostępu do YouTube wymaga osobnego rzeczywistego testu linku w aplikacji.
