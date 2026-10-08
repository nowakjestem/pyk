# Weryfikacja — 8–9 października 2026

## Środowisko i testy

- Lokalnie Python 3.12.13, po poprawkach API OpenAI: 144 testy przeszły; 8 testów renderowania pominięto, ponieważ lokalny FFmpeg nie ma libass.
- Na `narcyz`, nowy obraz Docker z Pythonem 3.12, FFmpeg/libass i whisper.cpp v1.8.7: **152 testy przeszły**. Wcześniejsza poprawka granic rozdziałów miała 114 zaliczonych testów i oba zadania CI zakończone powodzeniem.
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
polskim filmie jako niewystarczającą. Na tej podstawie przygotowano backend OpenAI;
pełna próba opisana poniżej używała lokalnego modelu base Q5_0.

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

## Integracja bezpośredniego API OpenAI

Dodano backend `openai`, domyślnie wybrany w YAML, z modelem `whisper-1` zwracającym tekst,
czasy słów i segmentów. Schemat konfiguracji nadal domyślnie przyjmuje `local`, aby zachować
ustawienia zadań zapisanych przed tą zmianą. Lato, podświetlanie słów i oba warianty korzystają
z tego samego formatu Cue/Word. Klucz pochodzi wyłącznie z `OPENAI_API_KEY` w otoczeniu procesu.

Testy adaptera sprawdzają prawdziwe żądania multipart do lokalnego serwera testowego:
oba poziomy timestampów, język, prompt, ponowne otwarcie audio w retry, błędy autoryzacji,
budżetu, limitów, timeout, rozmiar uploadu i niepoprawny JSON. Sprawdzono też polskie znaki,
interpunkcję, granice klipu, czasy względne, ASS obu wariantów oraz pomijanie ciszy.
Wznowienie po błędzie drugiego fragmentu nie powtarza pierwszego rozpoznanego fragmentu.
Klucz nie trafia do konfiguracji zapisanej w SQLite. Kontrola zależności backendu OpenAI
nie wymaga lokalnego modelu ani binarek Whispera.

Obraz początkowo przygotowano na VPS-ie z kodu pobranego przez Git, a po dodaniu klucza
odtworzono oba kontenery, żeby wczytały nowy `env_file`. Zachowano zmiany stylu użytkownika
przez autostash podczas aktualizacji checkoutu. Backend `openai` jest aktywny dla nowych zadań.
Harmonogram wyłączenia `whisper-1` i instrukcje konfiguracji opisano w README.

Pierwsza rzeczywista próba została odrzucona przez OpenAI z kodem `credit_balance_exhausted`.
Po doładowaniu salda przez użytkownika transkrypcja zakończyła się powodzeniem. Dodano
rozróżnianie braku salda, limitów wydatków organizacji/projektu oraz limitu użycia od
chwilowych limitów żądań. Błędy rozliczeń nie są ponawiane. Odpowiedź błędu jest odczytywana
w całości do limitu 4096 bajtów przed klasyfikacją, również przy fragmentowanym HTTP.

Próba wykorzystała 30 sekund polskiej mowy z wcześniejszego klipu. Z wariantu letterbox
usunięto pasy z wcześniejszymi napisami, aby uzyskać czysty obraz do ponownego renderu.

| Etap | Wynik |
|---|---:|
| Długość próbki | 30 s |
| Transkrypcja OpenAI wraz z wyodrębnieniem audio | 3,81 s |
| Pierwszy render crop | 8,84 s |
| Pierwszy render letterbox | 4,78 s |

Oba pliki przeszły kontrolę parametrów i pełne dekodowanie. Wizualna kontrola klatek
potwierdziła Lato, tło słowa i czytelne napisy w obu wariantach. Rzeczywista odpowiedź API
ujawniła podział nazwiska z łącznikiem na dwa wpisy słów; parser teraz łączy takie wpisy,
zachowując ich rzeczywiste granice czasu. Dodano regresję dla nazwiska, osobnej interpunkcji
oraz niezgodności tekstu bez wymyślania czasów słów. Ponowny render wykorzystuje zapisaną
transkrypcję i nie wymaga kolejnej opłaty API. Próba sprawdza integrację i renderowanie;
nie stanowi pełnej oceny jakości rozpoznawania ani ponownego przetworzenia całego filmu.

Raport, transkrypcja i oba filmy są na VPS-ie w `data/openai-verification/`.
