# Publikacja klipów przez Buffer

Pyk dodaje zatwierdzone klipy do kolejki Buffera (`mode: addToQueue`). Buffer wybiera
wolne sloty z harmonogramu każdego konta. Terminy pojawiają się w wiadomości
Mattermosta po zatwierdzeniu i mogą wykraczać poza najbliższe 7 dni.

Na wiadomości z dwoma linkami dodaj:

- `:scissors:` — publikacja wariantu crop;
- `:frame_with_picture:` — publikacja wariantu letterbox.

Każdy członek skonfigurowanego kanału Mattermosta może zatwierdzać; nie ma whitelisty.
Reakcje botów są ignorowane. Pierwsza poprawna reakcja ustala jeden wariant rozdziału
dla wszystkich kont docelowych. Kolejne reakcje nie tworzą kolejnych publikacji.
Reakcja na osobną wiadomość z opisem albo wiadomość źródłową nie uruchamia publikacji.
Usunięcie reakcji nie anuluje wpisu. Anuluj go w Bufferze; Pyk synchronizuje status
przy kolejnej kontroli, domyślnie co godzinę.

## Konfiguracja

1. Podłącz konta społecznościowe w Bufferze i sprawdź, że pozwalają na automatyczną
   publikację, a nie tylko przypomnienia na telefon.
   Ustaw dni, godziny i strefę czasową w **Posting Schedule** każdego konta.
   To tam ustawiasz dzienne godziny i odstępy między publikacjami.
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
  scheduling_mode: addToQueue
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
Domyślny tryb to teraz `addToQueue`, także dla wcześniejszych snapshotów bez pola
`scheduling_mode`. Po restarcie bota stare propozycje terminów zostają usunięte z
nieuruchomionych planów, a wiadomości odświeżone. Już wysłane wpisy i zapisane
żądania `customScheduled` zachowują dotychczasową obsługę, bez ponownej publikacji.
Wyłączenie `buffer.enabled` w aktualnej konfiguracji bota zatrzymuje konsumenta
lokalnej kolejki i obsługę nowych reakcji, ale nie anuluje wpisów już przyjętych przez Buffer.

## Terminy i limity

Buffer wybiera termin osobno dla każdego konta; Instagram, TikTok i YouTube mogą
opublikować klip w różnych godzinach. Pyk nie podaje `dueAt`, nie losuje minut i nie
rezerwuje slotów przed reakcją. `schedule.timezone` służy do wyświetlania terminów
w Mattermoście; harmonogram i jego strefa czasowa pochodzą z Buffera. Ręczne
przesunięcia w Bufferze są widoczne po synchronizacji.

Tryb `scheduling_mode: customScheduled` pozostaje dostępny dla zgodności ze starszą
wersją: używa pseudolosowania w ciągu 168 godzin i pól `schedule.window_start`,
`window_end`, `min_gap_minutes`, `max_posts_per_day`, `min_lead_minutes`.
`buffer-replan ID_ZADANIA` przelicza tylko niezatwierdzone propozycje w tym trybie.

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
Pyk wysyła części w kolejności do kolejnych slotów każdego konta. Nie wymusza
jednoczesnej publikacji ani sąsiadujących godzin na różnych platformach; wpisy
dodane równolegle w Bufferze mogą zająć slot między częściami. Niepowodzenie lub
nieznany wynik wcześniejszej części blokuje późniejsze części tylko na tym koncie.
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

Przed wysyłką Pyk sprawdza harmonogram konta i kolejkę: musi istnieć slot przed
końcem retencji z zapasem, również po ostatnim obecnie zaplanowanym wpisie.
To ostrożna kontrola dostępności, nie samodzielne wyznaczanie daty publikacji.
Brak slotów lub zbyt długa kolejka powodują błąd konkretnej części/konta.
Nie ma transakcji obejmującej odczyt i zapis Buffera: jeśli rzeczywisty zwrócony
termin albo późniejsze ręczne przesunięcie przekracza retencję, wiadomość pokazuje
ostrzeżenie. Przyspiesz wtedy wpis w Bufferze lub zapewnij dłuższą dostępność pliku.
Pyk nie usuwa przyjętego wpisu i nie tworzy go ponownie.

Wiadomość z linkami pokazuje wybrany wariant, datę i wynik per konto:

- oczekuje — czeka na opis lub wykonanie próby;
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
