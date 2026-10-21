# Zasady pracy

## Kończenie funkcjonalności i wydawanie wersji

- Po ukończeniu każdej funkcjonalności (feature’a) i sprawdzeniu zmian zapytaj
  użytkownika, czy utworzyć nowy tag wersji.
- Zgoda na utworzenie taga oznacza zgodę na pełne wydanie, w tym push do
  `origin`, aby uruchomić budowanie i publikację obrazu w GHCR. Tak samo traktuj
  polecenia „wydaj to” i „publikuj”. Bez dodatkowych pytań przygotuj commit
  ukończonych zmian, jeśli jest potrzebny, utwórz nowy tag i wypchnij commit
  oraz tag do `origin`. Nie kończ na lokalnym tagu. Nie dołączaj do wydania
  niezwiązanych zmian.
- Stosuj tagi `vMAJOR.MINOR.PATCH`. Jeśli użytkownik nie poda wersji, zwiększ
  PATCH względem najwyższego istniejącego stabilnego taga; przy braku tagów
  zacznij od `v0.1.0`. Nie nadpisuj ani nie przenoś istniejących tagów.
- Tag `v*` uruchamia workflow `.github/workflows/docker.yaml`, który publikuje
  obraz w GHCR. Po wydaniu podaj użytkownikowi tag i stan publikacji; nie
  deklaruj powodzenia publikacji bez sprawdzenia wyniku workflow.
