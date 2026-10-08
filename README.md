# Pyk — klipy z rozdziałów YouTube

Bot w prywatnym kanale Mattermosta przyjmuje zwykłe wiadomości z linkiem do YouTube.
Z każdego rozdziału tworzy dwa MP4 z polskimi napisami: wycięty pionowy kadr oraz pełny
obraz na pionowym czarnym tle. Publikuje linki S3 w wątku wiadomości źródłowej.

Python 3.12, SQLite, yt-dlp + Deno/EJS, bezpośrednie API OpenAI, FFmpeg/libass i Docker Compose.
Lokalny whisper.cpp pozostaje opcjonalnym backendem i obsługuje starsze zadania.
Bez Redis, Celery, publicznego webhooka i panelu administracyjnego.

## Uruchomienie na VPS

Repozytorium i wszystkie obrazy buduj w osobnym katalogu, np. `/home/nowak/mattermost-rolki`.
`scripts/setup.sh` tworzy katalogi danych oraz `.env`, jeśli plik jeszcze nie istnieje,
i ustawia UID/GID kontenerów na właściciela katalogu. Nie nadpisuje istniejącego `.env`.

```sh
git clone git@github.com:nowakjestem/pyk.git /home/nowak/mattermost-rolki
cd /home/nowak/mattermost-rolki
sh scripts/setup.sh
# Uzupełnij .env na serwerze.
docker compose build
docker compose run --rm bot check --integrations --tools
docker compose up -d
curl --fail http://127.0.0.1:8089/healthz
```

### Aktualizacja przez Git

Na `narcyzie` katalog `/home/nowak/mattermost-rolki` jest checkoutem gałęzi `main`.
Po wypchnięciu zmian do GitHuba wdrażaj je w tym katalogu:

```sh
git pull --ff-only origin main
docker build --target test -t pyk-tests .
docker run --rm --memory=768m --memory-swap=768m --cpus=2 pyk-tests -m pytest -q
docker compose build
docker compose run --rm --no-deps bot check --integrations --tools
docker compose up -d
```

Wykonuj następny krok dopiero po powodzeniu poprzedniego. Przy lokalnych zmianach Git
zatrzyma aktualizację, zamiast nadpisywać konfigurację. `.env`, `data/`, `models/`,
`fonts/`, `artifacts/` oraz `compose.override.yaml` są poza Gitem i pozostają na serwerze.
Ustawienia środowiska trzymaj w `.env`, a własne mounty w `compose.override.yaml`.
GitHub Actions uruchamia lint i testy na push oraz pull request; wdrożenie na VPS pozostaje ręczne.

Bot ma limit 128 MiB; worker 768 MiB oraz 2 vCPU. Swap nie jest wymagany ani automatycznie
tworzony. Jednocześnie działa jeden proces kosztownego etapu. Obie wersje klipu powstają
kolejno. W trybie OpenAI model działa u dostawcy; VPS wyodrębnia audio i renderuje napisy.
W opcjonalnym trybie lokalnym model jest zwalniany przed renderowaniem.

Kontrola zapasu pamięci kontenera uwzględnia czysty, nieaktywny cache plików,
który Linux może odzyskać. Pobrany film w cache nie blokuje kolejnego etapu.
Pamięć procesów, tmpfs i strony oczekujące na zapis pozostają w zużyciu;
przy braku statystyk cgroup kontrola zachowuje ostrożniejszy, surowy pomiar.

Przy pierwszym uruchomieniu bot ustala punkt początkowy i pomija stare wiadomości.
Później synchronizuje przerwy przez REST API, również po restarcie. Edycje wiadomości
nie są osobnym wyzwalaczem. Ponowne przesłanie linku w **nowej wiadomości** tworzy nowe
zadanie; powtórzenie tego samego zdarzenia nie tworzy duplikatu.

### Konfiguracja Mattermosta

1. Utwórz konto bota i jego token dostępu. Używamy tokena konta bota, nie tokena człowieka.
2. Dodaj bota do zespołu i wybranego prywatnego kanału. Musi mieć prawo odczytu oraz tworzenia postów.
3. Wpisz do `.env`: `MATTERMOST_URL`, `MATTERMOST_BOT_TOKEN`, `MATTERMOST_CHANNEL_IDS`.
   Wiele ID kanałów oddzielaj przecinkami. Adres może zawierać bazową ścieżkę, ale nie `/api/v4`.
4. Dostęp HTTPS i WebSocket do Mattermosta musi działać z VPS-a.

Nie konfiguruj outgoing webhooka: Mattermost nie obsługuje go w kanałach prywatnych.
Bot nasłuchuje `/api/v4/websocket`, a odpowiedzi wysyła przez `/api/v4/posts` z `root_id`.

### Konfiguracja S3 i retencji

W `.env` ustaw `S3_BUCKET`, `S3_REGION`, `S3_PUBLIC_BASE_URL` i dane AWS.
`S3_PUBLIC_BASE_URL` oznacza **bazę adresów obiektów w buckecie**, bez prefiksu `clips`;
np. `https://bucket.s3.eu-central-1.amazonaws.com` albo publiczną domenę CDN.
Dla usługi zgodnej z S3 ustaw także `S3_ENDPOINT_URL`; jeśli wymaga adresowania po ścieżce,
ustaw `s3.addressing_style: path` w YAML. Zamiast statycznych kluczy AWS można użyć roli IAM.

```sh
docker compose run --rm bot s3-policies > data/s3-policies.json
```

Komenda generuje trzy dokumenty: politykę publicznego odczytu dla prefiksu `clips/*`,
uprawnienia konta aplikacji oraz regułę lifecycle z usuwaniem po 30 dniach.
**Nie zmienia bucketa.** Administrator storage musi zastosować te ustawienia.
Jeżeli bucket ma istniejące polityki/reguły, należy scalić je z wygenerowanymi dokumentami.
AWS Block Public Access musi dopuszczać publiczną politykę bucketa; nie potrzebujemy publicznych ACL.
Linki są publiczne również wtedy, gdy wiadomość Mattermosta pochodzi z prywatnego kanału.

Preferowany jest osobny bucket bez wersjonowania. W wersjonowanym buckecie należy też
skonfigurować usuwanie nieaktualnych wersji i delete markerów. Lifecycle działa asynchronicznie;
nie jest gwarancją usunięcia co do sekundy. Usługi zgodne z S3 muszą obsługiwać lifecycle
albo mieć równoważną regułę retencji u dostawcy.

Upload jest jednoznaczny dzięki kluczowi `clips/{job_id}/{chapter_index}/{variant}.mp4`.
Aplikacja sprawdza rozmiar i metadane SHA-256 przez HEAD przed potwierdzeniem wyniku.
Duże pliki używają multipart bez równoległych wątków.

### Konfiguracja OpenAI

Do `.env` dodaj `OPENAI_API_KEY` z uprawnieniem do tworzenia transkrypcji audio.
Klucz jest odczytywany z otoczenia procesu; nie trafia do YAML, checkpointów ani Git.
Po dodaniu lub zmianie klucza **odtwórz kontenery** — sam restart nie przeładowuje `env_file`:

```sh
docker compose run --rm --no-deps bot check --integrations --tools
docker compose up -d --force-recreate bot worker
```

Domyślnie wysyłamy do `https://api.openai.com/v1/audio/transcriptions` wyłącznie audio
mono 16 kHz, raz dla obu wariantów klipu. Nie ma pośrednika. Model `gpt-transcribe`
zwraca JSON z samym tekstem; nie żądamy czasów słów ani segmentów. Audio dzielimy na
krótkie fragmenty, domyślnie około 8 sekund, wybierając pobliskie pauzy. Granice tych
fragmentów wyznaczają przybliżony czas wyświetlania napisów. Dłuższy tekst jest dzielony
na krótkie frazy proporcjonalnie do jego długości. To nie jest dokładne dopasowanie
do wypowiadanych słów. Własne nazwy i pisownię można podać w `asr.prompt`.

Przy cenie 0,0045 USD/min 20 minut tygodniowo oznacza około **0,39 USD miesięcznie**,
bez ponawianych zapytań. Źródła: [OpenAI — transkrypcja](https://developers.openai.com/api/docs/guides/speech-to-text),
[model i cena gpt-transcribe](https://developers.openai.com/api/docs/models/gpt-transcribe).

Brak klucza jest wykrywany przed uruchomieniem workera i przy kontroli integracji.
Błędy autoryzacji i brak środków kończą zadanie czytelnym komunikatem. Błędy połączenia,
limit chwilowy i błędy serwera mają maksymalnie trzy próby. Retry uwzględnia `Retry-After`
(sekundy lub datę HTTP); bez poprawnej wskazówki stosuje rosnące opóźnienie.
Surowe odpowiedzi błędów API
nie są publikowane. Rozpoznane fragmenty są zapisywane atomowo w katalogu rozdziału;
wznowienie wykorzystuje je zamiast powtarzać płatne zapytania. Utrata połączenia po
przyjęciu żądania przez OpenAI może mimo to prowadzić do ponownego naliczenia opłaty.

## YAML: obraz, napisy i ASR

`config.yaml` jest walidowany przy starcie. Nieznane klucze, niepoprawny format obrazu,
kolory lub parametry kończą start czytelnym błędem. Zmiany konfiguracji wymagają restartu:

```sh
docker compose restart bot worker
```

Każde zadanie zapisuje pełną konfigurację i jej hash. Zmiana YAML dotyczy nowych zadań;
już rozpoczęte zadania wznawiają się z zapisanymi ustawieniami. Sekrety nie trafiają do tego zapisu.

Przykład zmiany stylu:

```yaml
subtitles:
  font: Lato
  font_size: 42
  primary_color: '#FFFFFF'
  outline_color: '#000000'
  outline: 3
  shadow: 0
  bold: true
  max_lines: 2
  max_chars_per_line: 26
  max_phrase_seconds: 4
  margin_x: 40
  background:
    mode: line # none | line | word
    color: '#2563EB'
    opacity: 0.85
    padding: 6
  crop:
    alignment: 2
    margin_v: 130
  letterbox:
    alignment: 2
    margin_v: 230
```

Alignment używa numeracji ASS: 1–3 dół, 4–6 środek, 7–9 góra; 2 oznacza dół/środek.
Marginesy i font_size są liczone w pikselach docelowego obrazu.
Przy większym foncie zmniejsz `max_chars_per_line`, aby tekst mieścił się w kadrze:
np. przy Lato 80 px i obrazie 720×1280 punktem wyjścia jest 16 znaków zamiast 26.
Dla innego formatu źródła lub rozdzielczości wyjściowej dostosuj położenie napisów w wariancie
z pasami. Napisy są wypalane **po** kadrowaniu.

Obraz Docker zawiera **Lato** (także bold) i **DejaVu Sans**, z obsługą polskich znaków.
W `font` wpisz nazwę rodziny fontu; `bold` wybiera pogrubienie. `rolki check --tools`
sprawdza, czy font jest zainstalowany, żeby wykryć niezamierzoną podmianę fontu.
Własne TTF/OTF można umieścić w katalogu `fonts/` i zamontować w obu usługach
przez `compose.override.yaml`, bez zmian kodu:

```yaml
services:
  bot:
    volumes:
      - ./fonts:/usr/local/share/fonts/custom:ro
  worker:
    volumes:
      - ./fonts:/usr/local/share/fonts/custom:ro
```

Po zmianie mountów użyj `docker compose up -d --force-recreate bot worker`.
Nazwy dostępnych rodzin sprawdzisz przez
`docker compose run --rm --no-deps --entrypoint fc-list bot -f '%{family}\n'`.
Samą zmianę stylu w YAML wystarczy zastosować restartem.

`background.mode` wybiera: `none` — bez tła, `line` — prostokąt pod każdą linijką,
`word` — prostokąt pod aktualnie wypowiadanym pełnym słowem. Tekst całej frazy pozostaje
nieruchomy, tło znika w przerwach między słowami. `color` używa `#RRGGBB`, `opacity`
ma zakres 0–1 (1 oznacza nieprzezroczyste), a `padding` to odstęp w pikselach (0–30).
Obrys i kolor liter są niezależne od tła. Domyślny YAML wybiera Lato i niebieskie tło linijki;
na czarne tło całej linijki zmień `mode: line` i `color: '#000000'`.

Domyślny `gpt-transcribe` nie zwraca czasów słów; używaj `mode: line`. Tryb `word`
również zadziała, ale użyje tła całej linijki. Opcjonalny `asr.model: whisper-1`
zwraca pełne słowa z czasami; aplikacja zachowuje interpunkcję segmentów i dobiera
krótkie frazy według granic słów. W opcjonalnym trybie lokalnym Whisper jest uruchamiany
z `-ojf`: pełny JSON zawiera czasy tokenów. Aplikacja łączy
tokeny w słowa z interpunkcją i dobiera frazy według granic tych słów. Są to
[eksperymentalne czasy whisper.cpp](https://github.com/ggml-org/whisper.cpp/tree/v1.8.7#word-level-timestamp-experimental),
bez dodatkowego modelu forced alignment; mogą być niedokładne. Jeśli dane słów są
niekompletne, mają zerową długość lub słowo wymaga podziału między linijkami,
fraza zachowuje tradycyjne czasy segmentu, a tryb `word` używa tła całej linijki.
Starsze checkpointy bez czasów słów również działają z tym fallbackiem.
SRT pozostaje zwykłym tekstem; dynamiczne tło znajduje się w ASS i wypalonym MP4.

`video.width` / `height` muszą tworzyć parzyste 9:16, np. 720×1280 albo 1080×1920.
`crop_x` / `crop_y` mają zakres 0–1: 0 to lewy/górny brzeg, 0.5 to środek, 1 prawy/dolny brzeg.
Domyślnie źródło jest ograniczone do 1080p; crop z takiego materiału bywa powiększany.
Zwiększenie rozdzielczości oznacza większy koszt i zużycie pamięci — wymaga ponownego benchmarku.

ASR domyślnie używa OpenAI `gpt-transcribe` i języka `pl`:

```yaml
asr:
  provider: openai
  model: gpt-transcribe
  language: pl
  prompt: ''
  text_chunk_seconds: 8
  concurrency: 3
  max_chunk_seconds: 300
  request_timeout_seconds: 300
```

`max_chunk_seconds` ogranicza długość audio wyodrębnianego jednorazowo na VPS-ie;
`text_chunk_seconds` ustawia docelową długość zapytań GPT (3–15 sekund). Jeśli pobliska
pauza występuje do 1,5 sekundy wcześniej, dzielimy w jej środku. Końcówka do 1,5 sekundy
jest dołączana do poprzedniego fragmentu. Próbki nie są pomijane ani wysyłane dwukrotnie.
Całkowicie ciche fragmenty nie wymagają zapytania API. Jakość przy muzyce, szumie i nazwach własnych wymaga
sprawdzenia na własnych nagraniach. Dla angielskiego ustaw `language: en`, dla autodetekcji `auto`.

`asr.concurrency` ogranicza liczbę równoległych zapytań GPT w rozdziale (1–8, domyślny
YAML: 3). Odpowiedzi są zapisywane osobno i składane w kolejności audio. Błąd lub
przerwanie zadania anuluje pozostałe zapytania i sprząta ich pliki; ukończone odpowiedzi
pozostają dostępne przy wznowieniu. W tym samym czasie istnieje najwyżej tyle plików
audio, ile wynosi limit. Renderowanie i zadania kolejki nadal są wykonywane kolejno.
Starsze snapshoty bez tego ustawienia zachowują jedno zapytanie naraz. Ograniczenie
współbieżności nie zastępuje limitu żądań na minutę; w razie błędów 429 zmniejsz tę wartość.
[OpenAI Docs — limity i ponawianie](https://developers.openai.com/api/docs/guides/rate-limits).

Dla lokalnego trybu ustaw `asr.provider: local`. Wtedy domyślnie używany jest wielojęzyczny
`base` Q5_0 i dwa wątki. Pobierz model przez
`docker compose run --rm -v ./models:/app/models bot model-download`.
Lokalny model można zmienić przez `model_url` i `model_path`; nie używaj modeli `.en` dla polskiego.
Zadania zapisane przed dodaniem API zachowują tryb lokalny i nie wymagają klucza OpenAI.
Zadania przyjęte z modelem `whisper-1` również zachowują swój model. OpenAI zapowiada jego
wyłączenie na **26 lutego 2027**: [harmonogram wyłączeń](https://developers.openai.com/api/docs/deprecations).

## CLI, kolejka i odzyskiwanie

```sh
docker compose run --rm bot queue
docker compose run --rm bot job PELNY_ID_ZADANIA
docker compose run --rm bot retry PELNY_ID_ZADANIA
docker compose run --rm bot retry-notifications
docker compose logs --tail=100 bot worker
```

Można też przetworzyć URL bez Mattermosta. Przed uruchomieniem pipeline z CLI zatrzymaj worker:

```sh
docker compose stop worker
docker compose run --rm worker run 'https://www.youtube.com/watch?v=VIDEO_ID'
# Wersja bez S3 — wyniki trafiają do data/output/:
docker compose run --rm worker run 'https://www.youtube.com/watch?v=VIDEO_ID' --local-output
# Wznowienie także zadania z lokalnym outputem:
docker compose run --rm worker resume PELNY_ID_ZADANIA
docker compose start worker
```

Lokalne wyniki nie podlegają lifecycle S3; usuwaj je samodzielnie. Worker usługowy nie przejmuje
zadań z lokalnym outputem. Komenda `retry` wznawia zadania usługowe, `resume` wykonuje je w CLI.

SQLite używa WAL, transakcji i ograniczenia `(post_id, video_id)`. Blokada pliku dopuszcza
tylko jeden worker. Po restarcie transkrypcja oraz wysłane warianty są odczytywane z checkpointów.
Ukończony render oczekujący na upload jest używany ponownie. Wyniki wcześniejszych rozdziałów
pozostają dostępne po błędzie późniejszego etapu.

Powiadomienia mają osobny outbox i maksymalnie trzy próby. Po niejednoznacznym błędzie POST
bot sprawdza własny identyfikator zdarzenia w wątku, zanim wyśle wiadomość ponownie.
Failed notifications powodują stan degraded w `/healthz`; `retry-notifications` ponawia wysyłkę.

`waiting` oznacza niedobór RAM/dysku. Worker ponawia sprawdzenie co 60 sekund i wysyła tylko
jedno powiadomienie o oczekiwaniu. Domyślne progi: 768 MiB dostępnego RAM hosta, 512 MiB
headroom kontenera, 5 GiB wolnego dysku i 8 GiB katalogu roboczego. Guard kontroluje dysk
również podczas przetwarzania. Nieudane pliki robocze są usuwane po 24 godzinach; ukończone
wyniki lokalne rozdziału po potwierdzonym uploadzie, źródło po zakończeniu zadania.

`/healthz` jest publikowane wyłącznie na `127.0.0.1:8089`. Sprawdza WebSocket, synchronizację,
heartbeat workera i niedostarczone powiadomienia. Docker sam restartuje zakończone procesy;
healthcheck oznacza stan unhealthy, ale sam nie restartuje działającego kontenera.
Logi Dockera mają rotację 3×10 MB; zawierają job_id, etap, czas i typ błędu, bez tokenów.
Zabezpiecz backup `data/state.sqlite3`, `-wal` i `-shm`: zatrzymaj oba procesy przed kopiowaniem
albo użyj API backup SQLite. Baza zawiera metadane wiadomości i publiczne linki.

## Testy i pomiar na narcyz

```sh
uv sync --locked --extra dev --python 3.12
uv run ruff check .
uv run pytest -q
docker build --target test -t mattermost-rolki:test .
docker run --rm --memory=768m --memory-swap=768m --cpus=2 mattermost-rolki:test -m pytest -q
```

Testy obejmują reconnect/backfill, wątki prywatne, deduplikację, częściowy upload, restart,
limity zasobów, błędy HTTP oraz prawdziwy render FFmpeg, z kontrolą geometrii, czasu, napisów,
audio i odtwarzalności. Test libass jest pomijany, jeśli lokalny FFmpeg nie ma tego filtra;
w obrazie testowym i CI uruchamia się w całości.

Pomiar polskiej próbki syntetycznej pod limitem zasobów:

```sh
docker run --rm --memory=768m --memory-swap=768m --cpus=2 \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/models:/app/models:ro" -v "$PWD/data:/app/data" \
  mattermost-rolki:test scripts/benchmark.py
```

Raport i oba filmy pojawią się w `data/benchmark/`. `max_child_rss_mib` oznacza największy
RSS pojedynczego procesu potomnego, nie sumę pamięci całego kontenera; limit Dockera obejmuje
cały kontener. Dla oceny jakości na ludzkiej mowie zamontuj własny plik i użyj `--source`.
Próbka syntetyczna sprawdza działanie oraz zasoby, nie dowodzi jakości na docelowych filmach.
Nie gwarantujemy czasu wykonania przed pomiarem prawdziwego materiału około 20 minut.
Benchmark domyślnie używa `--provider local`, niezależnie od backendu w YAML.
Próbę OpenAI uruchom przez `--provider openai`, przekazując klucz w `env_file` i używając
osobnego, pustego katalogu `--output`. Taka próba wykonuje płatne zapytania API.

## Granice v1

- Każdy **cały rozdział** daje dwa klipy. Brak rozdziałów kończy zadanie przed downloadem.
- Obsługiwane są publicznie dostępne filmy do 60 minut, także linki Shorts; playlisty nie są pobierane.
- Filmy wymagające logowania, aktywne transmisje i automatyczny wybór najlepszych fragmentów nie są obsługiwane.
- Stały kadr, bez śledzenia osoby. Publiczne linki, bez logowania do pobierania.
- Zmiany YouTube mogą wymagać aktualizacji yt-dlp/EJS; aktualizuj razem i odśwież `uv.lock` oraz `requirements.lock`.
