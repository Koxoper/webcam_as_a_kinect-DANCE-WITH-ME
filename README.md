# Webcam Dance Scorer

Ten projekt idzie w bardziej realistyczna strone niz emulacja Kinecta. Aplikacja pozwala:

- przeskanowac lokalny film z choreografia tylko raz
- zapisac wykryte ruchy ciala do pliku JSON
- potem grac z kamerka przeciw gotowemu wzorcowi bez ponownego analizowania calego filmu
- obslugiwac to wszystko przez proste okno zamiast wpisywania komend

To jest podejscie typu `scan once, play many times`, zeby nie obciazac komputera podczas kazdej sesji.

## Jak to dziala

### Tryb okienkowy `GUI`

Najprostsza opcja:

```bash
python main.py
```

albo:

```bash
python main.py gui
```

W oknie mozesz:

- wybrac film do skanowania
- wskazac gdzie zapisac plik `JSON`
- ustawic `FPS` skanowania
- wybrac gotowa choreografie
- wybrac poziom trudnosci oceniania
- uruchomic gre z kamerka jednym przyciskiem

## Jak teraz wyglada tryb gry

Po kliknieciu `Graj z kamerka` aplikacja:

1. otwiera film referencyjny zapisany w choreografii
2. sprawdza, czy Twoja sylwetka ma zblizona wielkosc do wzorca
3. kiedy ustawienie jest dobre, uruchamia odliczanie od `5`
4. po starcie pokazuje samo wideo choreografii
5. naklada na to wideo Twoj szkielet, zebys widzial jak trafiasz w ruch

Na ekranie nie ma osobnego podgladu z kamerki. Zamiast tego widzisz film i swoj szkielet nalozony na wzorzec.
Tryb gry startuje tez w fullscreenie, a klawiszem `F` mozna przelaczac fullscreen wlaczony / wylaczony.

### Tryb `scan`

Aplikacja otwiera lokalny plik wideo, wykrywa punkty ciala i zapisuje uproszczona choreografie do pliku `JSON`.

Przykladowe zastosowanie:

```bash
python main.py scan "C:\sciezka\do\tanca.mp4" --output choreography.json
```

Opcjonalnie mozna zmniejszyc obciazenie podczas skanu:

```bash
python main.py scan "C:\sciezka\do\tanca.mp4" --output choreography.json --scan-fps 8
```

### Tryb `play`

Aplikacja otwiera kamerke, laduje wczesniej zeskanowana choreografie i porownuje Twoja poze do wzorca w czasie.

```bash
python main.py play choreography.json
```

Na ekranie zobaczysz:

- referencyjne wideo choreografii
- Twoj szkielet nalozony na film
- szkielet wzorca dla latwiejszego trafiania w ruch
- dzwiek z filmu choreografii
- aktywny poziom trudnosci
- aktualny wynik
- sredni wynik
- prosta ocene `Perfect / Good / Ok / Miss`

## Poziomy trudnosci

W GUI sa dostepne 3 poziomy:

- `Latwy` - bardziej wyrozumialy scoring, dobry do pierwszych testow
- `Normalny` - wywazone ustawienie domyslne
- `Trudny` - ostrzejsza ocena i trudniej o `Perfect`

Poziomy trudnosci zmieniaja tez tolerancje czasowa, wiec system moze lepiej znosic male opoznienie kamerki i reakcji.

## Instalacja

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Audio

Dzwiek w trybie gry jest odtwarzany z tego samego pliku wideo, ktory byl skanowany. Do tego projekt uzywa `python-vlc`.

Jesli obraz dziala, ale nie slychac muzyki:

- upewnij sie, ze zainstalowales `python-vlc`
- na Windows najlepiej miec tez zainstalowany desktopowy `VLC media player`
- sprawdz, czy sam plik wideo faktycznie zawiera sciezke audio

## Co projekt robi dobrze

- przenosi ciezsze skanowanie filmu do osobnego etapu
- nie musi analizowac calego referencyjnego wideo podczas gry
- dziala na zwyklej kamerce internetowej
- daje baze pod bardziej zaawansowany system oceniania tanca

## Ograniczenia obecnej wersji

- film referencyjny musi byc lokalnym plikiem wideo
- aplikacja nie pobiera filmow z YouTube sama
- scoring jest na razie prosty i bazuje na podobienstwie pozy
- nie ma jeszcze automatycznej synchronizacji do muzyki
- dopasowanie przed startem jest oparte glownie o rozmiar sylwetki

## Dlaczego lokalny plik zamiast bezposrednio YouTube

To rozwiazuje dwa problemy:

- skan robimy tylko raz, a potem gramy na gotowych danych
- architektura jest prostsza i stabilniejsza niz laczenie odtwarzania YouTube, trackingu i gry w jednym kroku

Najwygodniejszy workflow jest taki:

1. Przygotowujesz lokalny plik wideo z choreografia.
2. Uruchamiasz `scan`.
3. Dostajesz plik `choreography.json`.
4. Potem odpalasz `play` tyle razy, ile chcesz.

## Dalszy rozwoj

Kolejne sensowne kroki:

1. dodanie okna z podgladem referencyjnego filmu podczas gry
2. lepsze dopasowanie ruchu w czasie
3. liczenie osobno rak, nog i tulowia
4. obsluga restartu, pauzy i wyboru momentu startu
