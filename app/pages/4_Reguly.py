from pathlib import Path
import re
import streamlit as st

ROOT = Path(__file__).parent.parent.parent
POLICY_DIR = ROOT / "policy"

# Te same wartości, których używa agent (jedno źródło: app/iris_agent.py)
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from iris_agent import SCREENING_THRESHOLD, CONFIDENCE_THRESHOLD, HIGH_RISK_COUNTRIES, MAX_STEPS

def read_policy(name):
    path = POLICY_DIR / name
    return path.read_text() if path.exists() else ""

def parse_rules(text, prefix):
    """Wyciąga z pliku polityki linie typu '- **R1.** treść' -> [(R1, treść)]."""
    rules = []
    for line in text.splitlines():
        m = re.match(rf"-\s*\*\*({prefix}\d+)\.\*\*\s*(.+)", line.strip())
        if m:
            rules.append((m.group(1), m.group(2).strip()))
    return rules

def show_rules(rules, explain):
    if not rules:
        st.warning("Nie udało się wczytać zasad z pliku polityki.")
        return
    for code, text in rules:
        st.markdown(f"**{code}.** {text}")
        if code in explain:
            st.caption(f"Po ludzku: {explain[code]}")

sanctions = read_policy("sanctions_policy.md")
payments = read_policy("payment_policy.md")

st.title("Reguły: jak działa Mini-Iris")
st.markdown(
    "Mini-Iris sprawdza, czy klient banku albo strona przelewu **nie jest osobą z listy sankcyjnej** "
    "(amerykańska lista OFAC SDN). Komputer szybko wyłapuje podobne nazwiska, agent AI ocenia każde "
    "trafienie według spisanej polityki banku, a człowiek dostaje tylko trudne przypadki."
)

# --- Przepływ ---
st.header("1. Jak przebiega sprawdzenie")
st.markdown(f"""
1. **Screening (porównanie nazwisk).** Imię i nazwisko porównujemy z ok. 16 tys. nazwisk i aliasów z listy OFAC SDN.
   Używamy *fuzzy matchingu*, czyli porównania „na podobieństwo”, a nie 1:1, żeby złapać literówki i inne zapisy
   (np. *Mohammed* / *Muhammad*). Wynik to liczba od 0 do 100.
2. **Brak trafienia (wynik < {SCREENING_THRESHOLD}).** Nikt z listy nie jest podobny, więc klient lub przelew przechodzi
   automatycznie. Tak kończy się zdecydowana większość sprawdzeń.
3. **Trafienie (wynik ≥ {SCREENING_THRESHOLD}) = alert.** Samo podobne nazwisko jeszcze nic nie znaczy, bo na świecie jest wielu
   ludzi o tym samym nazwisku. Dlatego alert trafia do **agenta AI**.
4. **Agent AI prowadzi dochodzenie.** Agent nie dostaje wszystkiego naraz. Dostaje sam alert i **narzędzia**,
   z których sam wybiera, czego potrzebuje, krok po kroku (jak analityk):
   pełny wpis z listy SDN (aliasy, data urodzenia, paszport), dane klienta z systemu banku, dokładne porównanie dat
   urodzenia (liczone przez program, nie „w głowie” modelu), ryzyko kraju, kraj banku z kodu BIC i wcześniejsze decyzje
   analityków w podobnych sprawach. Gdy ma dowody, wywołuje narzędzie **submit_decision**: jedna z trzech decyzji,
   uzasadnienie z numerem zasady, lista dowodów, ocena czynników i pewność (0.0–1.0). Każdy krok widać w UI
   („Ślad dochodzenia agenta”) i jest zapisany w audit logu. Limit: {MAX_STEPS} rund, żeby agent nie krążył w nieskończoność.
5. **Bezpiecznik pewności.** Jeśli agent mówi „to nie on” albo „to on”, ale jego pewność jest niższa niż
   **{CONFIDENCE_THRESHOLD}**, system sam zmienia decyzję na **ESCALATE**. Niepewna AI nie może sama zamknąć sprawy.
6. **Kolejka analityka.** Wszystkie ESCALATE trafiają do człowieka (strona *Kolejka analityka*). Analityk może
   też zmienić decyzję agenta; to jest zapisywane i liczone w metrykach.
7. **Audit log.** Każda decyzja jest zapisywana w dzienniku, w którym każdy wpis zawiera „odcisk palca” (hash)
   poprzedniego. Jeśli ktoś po cichu zmieni lub usunie wpis, łańcuch się zerwie i strona *Kolejka analityka* to pokaże.
""")

st.subheader("Trzy możliwe decyzje")
c1, c2, c3 = st.columns(3)
c1.success("**FALSE_POSITIVE**\n\nTo NIE jest osoba z listy. Fałszywy alarm, sprawa zamknięta, klient/przelew przechodzi.")
c2.error("**TRUE_MATCH**\n\nTo JEST osoba z listy. Konto lub przelew zablokowane, sprawa idzie do MLRO (osoby odpowiedzialnej za przeciwdziałanie praniu pieniędzy).")
c3.warning("**ESCALATE**\n\nZa mało dowodów albo podwyższone ryzyko. Decyzję podejmuje człowiek.")

st.info("**Główna zasada:** w razie wątpliwości zawsze ESCALATE. Przepuszczenie osoby objętej sankcjami "
        "jest dużo gorsze niż dodatkowa praca analityka.")

# --- Klienci ---
st.header("2. Zasady dla klientów (strona *Sprawdź osobę*)")
st.markdown("""
Na czym agent opiera ocenę (od najmocniejszego dowodu do najsłabszego):
**numer paszportu → data urodzenia → obywatelstwo → podobieństwo nazwiska.**
Nazwisko jest najsłabszym dowodem, bo jest tylko powodem, dla którego powstał alert.
""")
show_rules(parse_rules(sanctions, "R"), {
    "R1": "ten sam paszport to praktycznie pewność, że to ta sama osoba.",
    "R2": "jeśli znamy oba paszporty i są różne, to dwie różne osoby.",
    "R3": "ktoś urodzony w 1985 nie jest osobą z listy urodzoną w 1960, nawet przy tym samym nazwisku.",
    "R4": "zgadza się nazwisko, data urodzenia i kraj, więc mamy kilka niezależnych dowodów.",
    "R5": "data się zgadza, ale kraj nie (albo go nie znamy), więc to za mało, by kogoś zablokować lub puścić.",
    "R6": "bez daty urodzenia osoby z listy nie da się porządnie porównać, więc decyduje człowiek.",
    "R7": "nikogo nie blokujemy tylko za to, że ma podobne nazwisko.",
    "R8": "lepiej dać analitykowi dodatkową sprawę niż przepuścić przestępcę.",
})

# --- Płatności ---
st.header("3. Zasady dla przelewów (strona *Płatności*)")
st.markdown("""
Przelew zwykle **nie zawiera daty urodzenia ani paszportu**, więc agent ma mniej dowodów. Patrzy na:
imię i nazwisko (lub nazwę firmy) zleceniodawcy i beneficjenta, kraj beneficjenta, bank beneficjenta i jego kraj
oraz tytuł przelewu.
""")
show_rules(parse_rules(payments, "P"), {
    "P1": "nazwisko z listy plus pasujący kraj lub bank to mocny sygnał, przelew blokujemy.",
    "P2": "podobne nazwisko, ale przelew idzie w zupełnie inny region i wygląda zwyczajnie, więc to fałszywy alarm.",
    "P3": "trafienie tylko na popularne słowo, jak „Ali” albo „Trading”, nic nie znaczy.",
    "P4": "przelew do kraju objętego sankcjami zawsze ogląda człowiek, nawet gdy nazwisko nie pasuje do nikogo.",
    "P5": "samo podobne nazwisko nie wystarczy do blokady.",
    "P6": "dziwny tytuł, brak kraju lub sprzeczne dane oznaczają, że decyduje człowiek.",
})
st.markdown("**Kraje wysokiego ryzyka** (przelew do nich jest automatycznie eskalowany, niezależnie od zdania agenta): "
            + ", ".join(HIGH_RISK_COUNTRIES) + ".")

# --- Ograniczenia ---
st.header("4. Czego Mini-Iris NIE robi")
st.markdown("""
- To jest **screening sankcyjny**, a nie pełny system AML (przeciwdziałania praniu pieniędzy).
  System sprawdza **kim jest** klient lub odbiorca. **Nie ocenia kwoty ani tytułu przelewu** pod kątem podejrzanych
  transakcji (np. nietypowo duże kwoty, rozbijanie wpłat). Tym zajmuje się osobny monitoring transakcji.
- Dane klientów są **syntetyczne** (wygenerowane), a lista krajów wysokiego ryzyka jest uproszczona.
- Agent AI to pomocnik, nie sędzia: ostateczna odpowiedzialność za ESCALATE i nadpisania należy do analityka.
""")

# --- Oryginalne polityki ---
st.header("5. Pełna treść polityk")
st.caption("Agent dostaje dokładnie ten tekst jako instrukcję. Zasady wyżej są wczytywane z tych samych plików, "
           "więc po zmianie polityki ta strona zaktualizuje się sama.")
with st.expander("policy/sanctions_policy.md (klienci)"):
    st.markdown(sanctions or "_Brak pliku._")
with st.expander("policy/payment_policy.md (przelewy)"):
    st.markdown(payments or "_Brak pliku._")
