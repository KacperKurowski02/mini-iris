# Polityka rozstrzygania alertów sankcyjnych (Mini-Iris Bank)

## 1. Cel
Każdy alert z systemu screeningu musi zostać rozstrzygnięty jako jeden z trzech wyników:
- **FALSE_POSITIVE**: klient NIE jest osobą z listy sankcji. Alert zamknięty.
- **TRUE_MATCH**: klient JEST osobą z listy sankcji. Konto blokowane, sprawa trafia do MLRO.
- **ESCALATE**: brak wystarczających dowodów. Decyzję podejmuje człowiek.

## 2. Dowody (od najsilniejszego)
1. Numer paszportu
2. Data urodzenia (DOB)
3. Obywatelstwo
4. Podobieństwo nazwiska

## 3. Zasady
- **R1.** Zgodny numer paszportu → TRUE_MATCH.
- **R2.** Różny numer paszportu (oba znane) → FALSE_POSITIVE.
- **R3.** Rok urodzenia różni się o więcej niż 2 lata → FALSE_POSITIVE.
- **R4.** DOB zgodna (co najmniej rok) ORAZ zgodne obywatelstwo → TRUE_MATCH.
- **R5.** DOB zgodna, ale obywatelstwo różne lub nieznane → ESCALATE.
- **R6.** Brak DOB po stronie listy sankcji → ESCALATE.
- **R7.** Samo podobieństwo nazwiska NIGDY nie wystarcza do TRUE_MATCH.
- **R8.** W razie wątpliwości → ESCALATE. Błędne zwolnienie przestępcy jest dużo gorsze niż dodatkowa praca analityka.

## 4. Wymagane uzasadnienie
Każda decyzja musi zawierać:
- decyzję (FALSE_POSITIVE / TRUE_MATCH / ESCALATE),
- numer zastosowanej zasady (np. R3),
- porównanie dowodów (co się zgadza, co nie),
- pewność od 0.0 do 1.0.