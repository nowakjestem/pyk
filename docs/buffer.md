# Publikacja klipów przez Buffer

Pyk planuje zatwierdzone klipy ze wspólną datą publikacji na wszystkich kontach
(`scheduling_mode: customScheduled`). Każda część filmu dostaje osobny termin,
wspólny dla Instagrama, TikToka i YouTube. Buffer otrzymuje identyczne `dueAt`
dla danego klipu, zamiast dobierać osobne okienko z kolejki każdego konta.
To wspólna zaplanowana data; rzeczywiste przetwarzanie i publikacja na platformach
mogą powodować opóźnienia.

Na wiadomości z dwoma linkami dodaj:

- `:scissors:` — publikacja wariantu crop;
- `:frame_with_picture:` — publikacja wariantu letterbox.

Bot dodaje obie reakcje do wiadomości z linkami, aby wystarczyło kliknąć gotowe emoji.
Reakcje bota nie zatwierdzają publikacji: dopiero dodatkowa reakcja użytkownika
wybiera wariant. Używane są emoji ze snapshotu konfiguracji danego zadania.
Po restarcie lub błędzie dodawania reakcji bot uzupełnia brakujące reakcje na
niezatwierdzonych wiadomościach z nadal dostępnymi klipami, bez ponownej wysyłki wiadomości.

Każdy członek skonfigurowanego kanału Mattermosta może zatwierdzać; nie ma whitelisty.
Reakcje botów są ignorowane. Pierwsza poprawna reakcja ustala jeden wariant rozdziału
dla wszystkich kont docelowych. Kolejne reakcje nie tworzą kolejnych publikacji.
Reakcja na osobną wiadomość z opisem albo wiadomość źródłową nie uruchamia publikacji.
Usunięcie reakcji nie anuluje wpisu. Anuluj go w Bufferze; Pyk synchronizuje status
przy kolejnej kontroli, domyślnie co godzinę.

## Konfiguracja

1. Podłącz konta społecznościowe w Bufferze i sprawdź, że pozwalają na automatyczną
   publikację, a nie tylko przypomnienia na telefon.
   W trybie wspólnych terminów godziny, odstępy i strefę czasową ustawiasz poniżej
   w `buffer.schedule`; osobne **Posting Schedule** kont nie wyznaczają tych dat.
2. W [Buffer Settings → API](https://publish.buffer.com/settings/api) utwórz klucz.
   Zapisz `BUFFER_API_KEY` w `.env` na VPS-ie albo jako sekret środowiska cloud
   przeznaczony dla `api.buffer.com`. Nie wpisuj klucza do YAML ani Git.
3. Odczytaj ID organizacji i kont:

```sh
docker compose run --rm --no-deps bot buffer-accounts
# Lokalny workflow:
uv run --locked rolki buffer-accounts
```

Klucz jest powiązany z kontem użytkownika i ma dostęp do wszystkich jego organizacji.
Pyk używa wyłącznie organizacji i kont wskazanych poniżej. `buffer-accounts` jest
odczytem: nie tworzy postów.

Zastąp sekcję `buffer` w YAML, używając rzeczywistych ID:

```yaml
buffer:
  enabled: true
  organization_id: 'ID_ORGANIZACJI'
  scheduling_mode: customScheduled
  reactions:
    scissors: crop
    frame_with_picture: letterbox
  channels:
    - id: 'ID_INSTAGRAMA'
      platform: instagram
      max_video_seconds: 180
      max_text_chars: 2000
      should_share_to_feed: true
    - id: 'ID_TIKTOKA'
      platform: tiktok
      max_video_seconds: 180
      max_text_chars: 2000
    - id: 'ID_YOUTUBE'
      platform: youtube
      max_video_seconds: 180
      max_text_chars: 2000
      category_id: '22'
      privacy: public
      made_for_kids: false
  schedule:
    timezone: Europe/Warsaw
    window_start: '10:00'
    window_end: '20:00'
    min_gap_minutes: 180
    max_posts_per_day: 2
    min_lead_minutes: 120
  retention_margin_hours: 72
  poll_seconds: 30
  status_poll_seconds: 3600
```

Następnie przebuduj i odtwórz kontenery, aby wczytały kod i otoczenie:

```sh
docker compose build
docker compose run --rm --no-deps bot check --integrations --tools
docker compose up -d --force-recreate bot worker
```

Kontrola integracji odczytuje stan kont i niczego nie publikuje. Domyślnie integracja
jest wyłączona. Starsze zadania bez konfiguracji Buffera nie dostają harmonogramu
ani nie uruchamiają publikacji na podstawie historycznych reakcji. Nowe zadania
zapisują snapshot konfiguracji; zmiana YAML nie zmienia kont tych zadań.
Konfiguracja w repozytorium wybiera `customScheduled`. Na istniejącym VPS-ie zmień
`buffer.scheduling_mode` z `addToQueue` na `customScheduled` przed przebudowaniem.
Nowy tryb dotyczy nowych zadań: zapisane snapshoty i wpisy przyjęte przez Buffer
zachowują dotychczasowy tryb. Sam restart nie przenosi już zaplanowanych publikacji.
Domyślna wartość modelu konfiguracji pozostaje `addToQueue` dla zgodności ze starymi
snapshotami bez tego pola. Pyk nie publikuje ponownie zaakceptowanych wpisów.
Wyłączenie `buffer.enabled` w aktualnej konfiguracji bota zatrzymuje konsumenta
lokalnej kolejki i obsługę nowych reakcji, ale nie anuluje wpisów już przyjętych przez Buffer.

## Terminy i limity

Limit kolejki jest odczytywany z API organizacji Buffera, a nie wpisany na sztywno.
W planie Free to [10 zaplanowanych materiałów na każde konto](https://buffer.com/pricing),
łącznie z wpisami dodanymi ręcznie. Każda część segmentu zajmuje jedno miejsce.
Pyk liczy wpisy zaplanowane, wysyłane i oczekujące na akceptację oraz rezerwuje
miejsce dla wysyłek o nieznanym wyniku, których API może jeszcze nie pokazywać.
Robocze szkice i opublikowane materiały nie zajmują miejsc w tej kontroli.

Po zapełnieniu kolejki zatwierdzony klip pozostaje w SQLite ze statusem
`waiting_capacity`; Mattermost pokazuje „czeka na wolne miejsce w Bufferze”.
Pyk sprawdza dostępność ponownie co `status_poll_seconds` (domyślnie godzinę), także
po restarcie. Publikacja lub usunięcie wpisu zwalnia miejsce, a Pyk wysyła kolejny
materiał bez ponownej reakcji. W `customScheduled` Pyk sprawdza pojemność wszystkich
kont przed pierwszą wysyłką danej części: pełne konto wstrzymuje cały klip.
Jeśli żadne konto nie przyjęło jeszcze klipu, przeterminowaną datę można wyznaczyć
ponownie dla całej grupy. Po przyjęciu wpisu przez choć jedno konto pozostałe
zachowują tę samą datę; gdy minie, otrzymują błąd wymagający sprawdzenia w Bufferze.
Jednoznaczna odpowiedź API `LimitReachedError`, np. po równoległym dodaniu wpisu
w UI, również powoduje oczekiwanie, bez wyczerpywania trzech prób wysyłki.
Pozostałe błędy i nieznane wyniki zachowują dotychczasową obsługę.
Przed wznowieniem nadal sprawdzamy dostępność filmu do terminu publikacji;
zbyt długie oczekiwanie może wymagać ponownego przygotowania wygasłego pliku.
Wysyłki już zakończone błędem w starszej wersji wymagają `buffer-retry`.

W `customScheduled` Pyk wybiera wspólną minutę w ciągu najbliższych 168 godzin,
w godzinach `schedule.window_start`–`window_end`, z uwzględnieniem strefy czasowej,
minimalnego wyprzedzenia, odstępów i dziennego limitu na każdym koncie. Uwzględnia
też istniejące wpisy Buffera oraz rezerwacje innych klipów i części. Termin zapisuje
w SQLite przed wysyłką. Restart i ponowienie błędu nie zmieniają daty przyjętych wpisów.
`buffer-replan ID_ZADANIA` przelicza tylko niezatwierdzone propozycje.

Opcjonalne `scheduling_mode: addToQueue` zachowuje starsze zachowanie: Buffer wybiera
termin osobno dla każdego konta, bez przekazywania `dueAt`. Daty mogą się różnić
i wykraczać poza 7 dni. Pełne konto w tym trybie nie blokuje pozostałych platform;
`schedule.timezone` służy tylko do wyświetlania terminów w Mattermoście.

## Segmenty dłuższe niż 3 minuty

Nowe segmenty dłuższe niż 180 sekund są dzielone na minimalną liczbę możliwie
równych części: 5 minut daje dwie części po około 2,5 minuty. Granica może zostać
przesunięta do 15 sekund do rozpoznanego końca zdania lub pauzy, jeśli wszystkie
części nadal mieszczą się w limicie. Gdy transkrypcja nie daje odpowiedniej granicy,
stosowane jest równe cięcie; ASR bez czasów słów nie gwarantuje granicy zdania.

Każda część ma przez cały czas napis `part 1`, `part 2` itd. na górze, czarny na
białym tle, w obu wariantach kadru. Krótkie segmenty nie dostają tego napisu.
Wiadomość rozdziału zawiera linki do wszystkich części. Jedna reakcja zatwierdza
wybrany wariant wszystkich części, a opis i tytuł publikacji zawierają numer części.
W `customScheduled` części mają rosnące terminy, oddalone przynajmniej o
`schedule.min_gap_minutes`; każda część ma identyczną datę na wszystkich kontach.
Niepowodzenie lub nieznany wynik wcześniejszej części blokuje późniejsze części
na wszystkich platformach do wyjaśnienia lub ponowienia błędu. W `addToQueue`
części trafiają kolejno do osobnych slotów każdego konta, a blokada jest per konto.
Wcześniej gotowe klipy i częściowo wysłane stare pliki nie są automatycznie
renderowane ponownie.

`max_video_seconds` i `max_text_chars` to konfigurowalne ograniczenia Pyk, nie lista
gwarancji platform. Domyślny ostrożny limit to 180 sekund i 2000 znaków. Dostosuj
wartości do bieżących możliwości kont i formatu. Pyk nie skraca filmu i nie obcina
opisu automatycznie: błąd jednego konta nie usuwa sukcesów innych.
Instagram używa `metadata.instagram.type: reel`. API YouTube wymaga tytułu i kategorii;
nie ma pola `metadata.youtube.type: short`. Klasyfikację pionowego filmu jako Shorts
trzeba zweryfikować na koncie YouTube. Polskie opisy są używane ponownie z checkpointu.
Jeśli opisy są wyłączone lub klip nie zawiera rozpoznanej mowy, podpisem jest tytuł
rozdziału. Jeśli generowanie włączonego opisu nie zakończy się sukcesem, wysyłka
czeka na jego ukończenie po ponowieniu zadania.

## S3 i stan publikacji

Buffer dostaje publiczny bezpośredni URL MP4, nie załącznik ani post z samym linkiem.
Pobiera film **przy publikacji**. Plik S3 musi pozostać dostępny do terminu plus
`retention_margin_hours`. Pyk zachowuje konserwatywny czas rozpoczęcia uploadu
na potrzeby kontroli retencji. Nie zmienia polityki lifecycle bucketa. Wymagana
jest publiczna dostępność HTTPS i rzeczywista retencja zgodna z YAML; wygasające
presigned URL nie nadają się do tej integracji.

W `addToQueue` przed wysyłką Pyk sprawdza harmonogram konta i kolejkę: musi istnieć slot przed
końcem retencji z zapasem, również po ostatnim obecnie zaplanowanym wpisie.
To ostrożna kontrola dostępności, nie samodzielne wyznaczanie daty publikacji.
Brak slotów lub zbyt długa kolejka powodują błąd konkretnej części/konta.
Nie ma transakcji obejmującej odczyt i zapis Buffera: jeśli rzeczywisty zwrócony
termin albo późniejsze ręczne przesunięcie przekracza retencję, wiadomość pokazuje
ostrzeżenie. Przyspiesz wtedy wpis w Bufferze lub zapewnij dłuższą dostępność pliku.
Pyk nie usuwa przyjętego wpisu i nie tworzy go ponownie.
W `customScheduled` kontrola retencji porównuje dostępność pliku bezpośrednio
ze wspólną datą klipu. API nie zapewnia transakcji obejmującej wszystkie konta:
błąd platformy lub zapełnienie kolejki przez równoległy wpis może pozostawić częściowy
sukces. Pyk zachowuje przyjęte wpisy i wspólną datę dla pozostałych; nie obiecuje
publikacji dokładnie w tej samej sekundzie ani automatycznego wycofania wpisów.

Wiadomość z linkami pokazuje wybrany wariant, datę i wynik per konto:

- oczekuje — czeka na opis lub wykonanie próby;
- czeka na wolne miejsce w Bufferze — zatwierdzony klip pozostaje w lokalnej kolejce;
- wysyłanie — trwa tworzenie wpisu;
- zaplanowano — Buffer potwierdził automatyczny wpis z filmem i terminem;
- opublikowano / błąd publikacji — wynik odczytany z Buffera;
- wynik nieznany — operacja mogła się udać; Pyk nie powtarza jej w ciemno;
- błąd — jednoznaczna odmowa, niezgodny format, retencja lub wyczerpane próby;
- usunięto w Bufferze — wcześniej znany wpis nie jest widoczny przy synchronizacji.

HTTP 200 nie jest dowodem sukcesu GraphQL. Klient sprawdza błędy w treści odpowiedzi,
potwierdzony tryb `automatic`, stan, film, opis i termin. Nie przełącza się na
przypomnienia. Przy 429 respektuje `Retry-After`; jednoznacznie nieudane próby są
ograniczone do trzech. Buffer nie dokumentuje idempotencji `createPost`. Timeout,
niejednoznaczny błąd serwera albo restart podczas wysyłki powoduje odczyt kalendarza
i próbę znalezienia unikalnego wpisu po koncie, tekście i URL konkretnej części
(oraz terminie dla starych żądań `customScheduled`). Brak wpisu w jednym
odczycie nie powoduje automatycznej ponownej mutacji.

```sh
docker compose run --rm --no-deps bot buffer-queue
docker compose run --rm --no-deps bot buffer-retry PELNY_ID_ZADANIA NUMER_ROZDZIALU ID_KONTA
```

Numer rozdziału jest liczony od 1. Retry dotyczy tylko wskazanego nieudanego konta;
pozostałe przyjęte wpisy pozostają bez zmian. Dla `unknown` bez znanego ID wpisu
operator musi najpierw sprawdzić w Bufferze, że wpis nie powstał. Dopiero wtedy:

```sh
docker compose run --rm --no-deps bot buffer-retry PELNY_ID_ZADANIA NUMER_ROZDZIALU ID_KONTA --confirmed-not-created
```

Znany zdalny ID blokuje tę operację: nie wolno odtworzyć istniejącej publikacji.
Przy podzielonym segmencie dodaj `--part 2`, aby ponowić drugą część; domyślnie
polecenie dotyczy pierwszej części. Kolejne oczekujące części ruszą po jej przyjęciu.
Anulowanie lub zmianę wariantu po zatwierdzeniu wykonuje się świadomie w Bufferze;
zmiany zdalne mogą spowodować stan `unknown`, wymagający sprawdzenia.

Limity API zależą od planu. Statusy są odczytywane godzinowo, a oczekiwanie na opis
nie powoduje zapytań do Buffera. Duża liczba postów i paginacja mogą zwiększyć liczbę
odczytów; w razie limitów zwiększ `status_poll_seconds`.

## Weryfikacja

Testy lokalne używają serwera HTTP i atrap kont: nie publikują treści. Obejmują
terminy i DST, kolizje, retencję, równoległe reakcje, reconnect, częściowe sukcesy,
GraphQL errors, utratę odpowiedzi i restart po zdalnym sukcesie.
Przed włączeniem produkcyjnym potrzebna jest kontrolowana próba na rzeczywistych
kontach z potwierdzeniem formatów oraz terminu. Samo przejście testów lokalnych
nie potwierdza publikacji na koncie użytkownika.

Dokumentacja API: [autoryzacja](https://developers.buffer.com/guides/authentication.html),
[planowanie](https://developers.buffer.com/guides/posts-and-scheduling.html),
[media](https://developers.buffer.com/guides/hosting-media.html),
[schema](https://developers.buffer.com/reference.html).
