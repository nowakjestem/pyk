# Weryfikacja — 8 października 2026

## Środowisko i testy

- Lokalnie Python 3.12.13: 89 testów przeszło; 7 testów renderowania pominięto, ponieważ lokalny FFmpeg nie ma libass.
- Na `narcyz`, obraz Docker z Pythonem 3.12, FFmpeg/libass i whisper.cpp v1.8.7: **96 testów przeszło**.
- Kontener testowy miał limit **768 MiB RAM, bez swapu, 2 vCPU**.
- Lint Ruff, formatowanie i sprawdzenie lockfile zależności zakończyły się powodzeniem.
- Render sprawdzono przez ffprobe, pełne dekodowanie FFmpeg oraz kontrolę pikseli: oba warianty są pionowe, zachowują audio i granice klipu, a wariant letterbox ma czarne pasy i napisy w dolnym pasie.
- Wizualnie sprawdzono rzeczywiste klatki crop i letterbox.
- Po dodaniu Lato i pełnego JSON Whispera sprawdzono tło całych linijek oraz ruch tła pojedynczych słów: jedna i dwie linijki, oba warianty, brak podświetlenia w pauzie, nieruchomy tekst. Testy obejmują łączenie polskich subtokenów, interpunkcję, niekompletne i zerowe czasy, przesunięcia chunków oraz wznowienie z checkpointu zawierającego słowa.
- Produkcyjny obraz `mattermost-rolki:local` przebudowano; `docker compose config --quiet` i `docker compose run --rm --no-deps bot check --tools` przeszły, w tym weryfikacja dostępności Lato.
- Z VPS-a pobrano metadane i cały krótki publiczny film YouTube (`jNQXAC9IVRw`) przez implementowany downloader, z Deno/EJS. Ten film nie ma rozdziałów; obsługę rozdziałów sprawdzają testy pipeline.

## Pomiar modelu base Q5_0 na CPU

Próbka: 10,434 s polskiej mowy syntetycznej z espeak-ng. Dwa filmy 720×1280,
font Lato, tryb tła `word`. Whisper zwrócił 16 słów z czasami.

| Etap | Wynik |
|---|---:|
| Transkrypcja | 4,29 s |
| Render crop | 2,56 s |
| Render letterbox | 2,19 s |
| Największy RSS procesu potomnego | 191,76 MiB |

RSS to pomiar pojedynczego procesu, nie całego kontenera. Cały benchmark ukończył się
w kontenerze z limitem 768 MiB. Nie wystąpiło OOM.

Rozpoznanie próbki syntetycznej zawierało błędy. Ten pomiar potwierdza działanie i zasoby,
nie jest oceną jakości na ludzkiej mowie. Jakość trzeba sprawdzić na docelowym polskim filmie;
większy model można wskazać w YAML i ponownie zmierzyć jego pamięć przed zmianą domyślnego modelu.

Raport i filmy na VPS-ie: `/home/nowak/mattermost-rolki/data/benchmark-words/`.
Lokalne podglądy: `artifacts/verification/word-background/` (poza Git).
Pierwotny benchmark bez tła pozostał w `data/benchmark/` (4,54 s ASR, 191,61 MiB RSS).
Model: `/home/nowak/mattermost-rolki/models/ggml-base-q5_0.bin`.

## Gotowość integracji

Kod, konfiguracja, model i obrazy są przygotowane na VPS-ie w `/home/nowak/mattermost-rolki`.
Bot i worker usługowy nie zostały uruchomione: brakuje adresu/tokena Mattermosta, ID kanału
oraz konfiguracji i dostępu do S3. Lokalny szablon tych danych znajduje się w `.env.example`.

Testy Mattermosta używają prawdziwego lokalnego serwera HTTP/WebSocket; testy S3 używają klienta
testowego. Nie wykonano publikacji do rzeczywistego Mattermosta ani rzeczywistego S3.
Polityki publicznego odczytu oraz retencja muszą zostać zastosowane w docelowym storage.
Pełna próba około 20-minutowego filmu z rozdziałami wymaga jego URL i konfiguracji integracji.
