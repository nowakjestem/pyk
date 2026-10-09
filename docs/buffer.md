# Publikacja klipów przez Buffer

Pyk planuje terminy po odczytaniu listy rozdziałów i zapisuje je w SQLite przed
renderowaniem. Terminy są propozycjami: do Buffera trafiają tylko zatwierdzone klipy.

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
zapisują snapshot konfiguracji; zmiana YAML nie zmienia kont ani terminów tych zadań.
Wyłączenie `buffer.enabled` w aktualnej konfiguracji bota zatrzymuje konsumenta
lokalnej kolejki i obsługę nowych reakcji, ale nie anuluje wpisów już przyjętych przez Buffer.

## Terminy i limity

Okno publikacji trwa od chwili odczytu metadanych do +7 dni (168 godzin), również
w poprzek zmiany czasu. Publikacja może nastąpić już dziś, jeśli pozwalają na to
okno godzinowe i `min_lead_minutes`. Nie czekamy do poniedziałku. Pyk równomiernie rozdziela rozdziały między dniami
i dobiera pseudolosową minutę w oknie dziennym. SQLite utrwala plan; restart go
nie losuje ponownie. Ten sam rozdział może ukazać się jednocześnie na różnych kontach.

Odstępy i limity dzienne obejmują wszystkie zadania Pyk kierowane na dane konto.
Pyk odczytuje również kalendarz Buffera podczas tworzenia planu i przed wysyłką,
więc uwzględnia wpisy dodane ręcznie. Zmiana kalendarza po kontroli nadal może
spowodować kolizję; nie ma wspólnej transakcji SQLite i Buffera.

Przy braku pojemności wiadomość pokazuje brak terminu. Po późnej reakcji albo
wykryciu kolizji Pyk szuka terminu w ciągu kolejnych 7 dni względem bieżącego czasu
i aktualizuje wiadomość. Terminów już przyjętych na choćby jednym koncie nie
przesuwa automatycznie; brakujące konta mogą wymagać ręcznego rozstrzygnięcia.

Po aktualizacji wcześniejsze propozycje terminów można przeliczyć dla wybranego
zadania. Polecenie dotyczy tylko rozdziałów, których nikt jeszcze nie zatwierdził,
i aktualizuje wiadomości z linkami przez outbox bota:

```sh
docker compose run --rm --no-deps bot buffer-replan PELNY_ID_ZADANIA
```

Wpisy już zatwierdzone lub zaplanowane w Bufferze zachowują swój termin.

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
i próbę znalezienia wpisu po koncie, terminie, tekście i URL. Brak wpisu w jednym
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
