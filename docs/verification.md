# Weryfikacja — 8 października 2026

## Środowisko i testy

- Lokalnie Python 3.12.13: 106 testów przeszło; 8 testów renderowania pominięto, ponieważ lokalny FFmpeg nie ma libass.
- Na `narcyz`, obraz Docker z Pythonem 3.12, FFmpeg/libass i whisper.cpp v1.8.7: **114 testów przeszło**. Oba zadania CI dla poprawki granic rozdziałów również zakończyły się powodzeniem.
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
nie jest oceną jakości na ludzkiej mowie. Użytkownik ocenił również jakość napisów na docelowym
polskim filmie jako niewystarczającą. Rozważane jest zewnętrzne API większego modelu;
obecny kod nadal używa lokalnego modelu base Q5_0.

Raport i filmy na VPS-ie: `/home/nowak/mattermost-rolki/data/benchmark-words/`.
Lokalne podglądy: `artifacts/verification/word-background/` (poza Git).
Pierwotny benchmark bez tła pozostał w `data/benchmark/` (4,54 s ASR, 191,61 MiB RSS).
Model: `/home/nowak/mattermost-rolki/models/ggml-base-q5_0.bin`.

## Próba pełnej integracji

Bot i worker działają na VPS-ie w `/home/nowak/mattermost-rolki` i mają zdrowe healthchecki.
Link w prywatnym kanale Mattermosta uruchomił przetwarzanie rzeczywistego polskiego filmu:
metadane wskazywały 14 min 14 s i 7 rozdziałów. Powstało **14 plików MP4**, po dwa warianty
na rozdział. Wszystkie 14 publicznych URL zwróciło anonimowo `206 video/mp4` dla żądania
zakresowego z nagłówkiem User-Agent przeglądarki; łączna wielkość wynosiła 285 690 272 bajtów.
Odpowiedzi z linkami i potwierdzenie zakończenia zostały wysłane do właściwego wątku.
Katalog roboczy zadania usunięto, a health endpoint potwierdził połączenie WebSocket
i brak nieudanych powiadomień.

W S3 ustawiono lifecycle usuwający obiekty pod prefiksem `clips/` po 30 dniach oraz
porzucający nieukończone multipart uploady po jednym dniu. Istniejąca polityka publicznego
odczytu pozostała bez zmian.

Próba ujawniła i pozwoliła naprawić dwa problemy:

- Sprawdzenie pamięci kontenera traktowało odzyskiwalny cache pobranego filmu jako pamięć
  zajętą przez procesy. Obecnie uwzględnia nieaktywny cache plików, zachowując dirty/writeback
  i pamięć aktywną. Dodano testy dla cgroups v1/v2 oraz brakujących/uszkodzonych statystyk.
- YouTube podawał zaokrąglone 854 s, podczas gdy pobrany plik miał 853,541 s. Koniec ostatniego
  rozdziału jest teraz ograniczany do rzeczywistej długości źródła. Źródło krótsze od metadanych
  o ponad sekundę nadal jest odrzucane. Dodano testy granic i rzeczywisty render krótkiego
  ostatniego rozdziału dla obu wariantów.

Wznowienie zachowało 12 już opublikowanych klipów i dokończyło dwa brakujące, bez ponownego
wysyłania wcześniejszych wyników. W trakcie renderowania obserwowano około 370 MiB pamięci
workera; nie wystąpiły zdarzenia OOM. Swap nie został dodany. Końcowy dostępny RAM hosta
wynosił około 1 GiB. Próba dotyczyła filmu krótszego niż planowane około 20 minut tygodniowo.
