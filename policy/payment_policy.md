# Polityka rozstrzygania alertów płatniczych (Mini-Iris Bank)

## 1. Cel
Każdy przelew zatrzymany przez screening płatności musi zostać rozstrzygnięty jako:
- **FALSE_POSITIVE**: strony przelewu NIE są osobami z listy sankcji i przelew nie dotyczy kraju objętego sankcjami. Przelew zostaje zwolniony.
- **TRUE_MATCH**: beneficjent lub zleceniodawca JEST osobą z listy sankcji. Przelew zostaje zablokowany, sprawa trafia do MLRO.
- **ESCALATE**: brak wystarczających dowodów albo podwyższone ryzyko. Przelew czeka, decyzję podejmuje człowiek.

## 2. Dowody w płatności
Przelew zwykle NIE zawiera daty urodzenia ani paszportu. Dostępne dowody to:
1. Imię i nazwisko (nazwa) beneficjenta oraz zleceniodawcy
2. Kraj beneficjenta
3. Bank beneficjenta (nazwa / BIC) i jego jurysdykcja
4. Tytuł przelewu, kwota, waluta

## 3. Zasady
- **P1.** Nazwisko zgodne z osobą z listy SDN ORAZ kraj beneficjenta lub banku zgodny z obywatelstwem/krajem tej osoby → TRUE_MATCH.
- **P2.** Nazwisko podobne, ale kraj beneficjenta i bank są w jurysdykcji niezwiązanej z osobą z listy (inne obywatelstwo, inny region) i tytuł przelewu jest zwyczajny → FALSE_POSITIVE.
- **P3.** Zgodność dotyczy tylko pojedynczego, popularnego słowa (np. "Mohammed", "Ali", "Trading", "Bank", "Group", nazwa miasta) → FALSE_POSITIVE.
- **P4.** Przelew do kraju objętego sankcjami lub przez taki kraj (np. Iran, Korea Północna, Syria, Kuba, Rosja, Białoruś, Krym) albo do banku z takiego kraju → co najmniej ESCALATE, nawet bez trafienia nazwiska.
- **P5.** Samo podobieństwo nazwiska NIGDY nie wystarcza do TRUE_MATCH.
- **P6.** W razie wątpliwości (niejasny tytuł przelewu, brak kraju, sprzeczne dane) → ESCALATE. Przepuszczenie przelewu do osoby objętej sankcjami jest dużo gorsze niż dodatkowa praca analityka.

## 4. Wymagane uzasadnienie
Każda decyzja musi zawierać:
- decyzję (FALSE_POSITIVE / TRUE_MATCH / ESCALATE),
- numer zastosowanej zasady (np. P2),
- porównanie dowodów (co się zgadza, co nie),
- pewność od 0.0 do 1.0.
