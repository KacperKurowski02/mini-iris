import sys
from pathlib import Path
import json
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))
import iris_agent as A
import evaluation as E

st.title("Ewaluacja agenta")
st.markdown(
    "Agent jest sprawdzany na **stałej próbce alertów, dla których znamy prawdę** (wiemy, kto naprawdę jest na liście). "
    f"Próbka jest warstwowa: prawdziwe trafienia, sobowtóry (to samo nazwisko, inna osoba) i przypadkowe podobieństwa, "
    f"zawsze z tym samym ziarnem losowania ({E.SEED}), więc różne wersje agenta porównujemy na tych samych alertach. "
    "Ewaluacja nie zapisuje nic do audit logu i nie korzysta z wcześniejszych decyzji.")

with st.expander("Co oznaczają metryki"):
    st.markdown("""
- **Przepuszczone trafienia** (missed true matches): osoba z listy uznana za fałszywy alarm. **Musi być 0.**
- **Zablokowane trafienia**: prawdziwe trafienia, które agent sam zablokował (reszta poszła do człowieka, co jest bezpieczne).
- **Fałszywe alarmy zamknięte automatycznie**: tyle pracy analityka agent oszczędza (redukcja FP).
- **Błędne blokady**: niewinny klient uznany za osobę z listy.
- **Odsetek eskalacji**: ile spraw i tak trafia do człowieka.
- **Zgodność z polityką**: czy decyzja jest taka, jaką wyznaczają zasady R1–R8 (wyliczone programem, bez AI).
- **Kroki / czas / koszt**: średnio na jeden alert.
""")

# --- Nowy przebieg ---
with st.expander("▶️ Uruchom nową ewaluację (płatne zapytania do API)"):
    c1, c2, c3 = st.columns(3)
    model = c1.selectbox("Model", list(A.PRICES), index=0)
    mode = c2.selectbox("Tryb", list(A.MODES), format_func=lambda m: A.MODES[m])
    n = c3.slider("Liczba alertów", 10, 30, 24)
    pin, pout = A.PRICES[model]
    calls = 1 if mode == "single" else 5
    est = n * calls * (3500 * pin + 400 * pout) / 1e6
    st.caption(f"Szacowany koszt: ok. ${est:.3f} (≈ {calls} wywołań API na alert). Limit: 30 alertów.")
    note = st.text_input("Notatka do przebiegu (np. co zmieniłeś w prompcie)")
    ok = st.checkbox("Rozumiem, że to płatne zapytania")
    if st.button("Uruchom", disabled=not ok):
        bar = st.progress(0.0)
        summary, _ = E.run_eval(n, model, mode, note,
                                progress=lambda i, t, name, d: bar.progress(i / t, text=f"{i}/{t} {name}: {d}"))
        bar.empty()
        st.success(f"Gotowe: {summary['run_id']} · koszt ${summary['total_cost_usd']}")

runs = E.load_runs()
if runs.empty:
    st.info("Brak wyników. Uruchom pierwszą ewaluację powyżej.")
    st.stop()

# --- Porównanie wersji ---
st.header("Porównanie przebiegów")
view = runs[["run_id", "model", "mode", "prompt_version", "n", "missed_true_matches", "true_matches_blocked",
             "false_positives_auto_closed", "wrong_blocks", "escalation_rate", "policy_accuracy",
             "avg_steps", "avg_seconds", "avg_cost_usd", "note"]].iloc[::-1]
st.dataframe(view, hide_index=True, width="stretch")
if (runs["missed_true_matches"] > 0).any():
    st.error("⚠️ Co najmniej jeden przebieg przepuścił osobę z listy sankcyjnej. Ta wersja nie nadaje się do użycia.")

# --- Szczegóły jednego przebiegu ---
st.header("Szczegóły przebiegu")
run_id = st.selectbox("Przebieg", runs["run_id"].iloc[::-1].tolist())
s = runs[runs["run_id"] == run_id].iloc[0]
r = E.load_results(run_id)

c = st.columns(6)
c[0].metric("Przepuszczone trafienia", int(s["missed_true_matches"]), help="Musi być 0")
c[1].metric("Zablokowane trafienia", s["true_matches_blocked"])
c[2].metric("FP zamknięte auto.", s["false_positives_auto_closed"])
c[3].metric("Eskalacje", f"{float(s['escalation_rate']):.0%}")
c[4].metric("Zgodność z polityką", f"{float(s['policy_accuracy']):.0%}")
c[5].metric("Koszt / alert", f"${float(s['avg_cost_usd']):.4f}", help=f"Śr. {s['avg_seconds']} s, {s['avg_steps']} kroków")

left, right = st.columns(2)
with left:
    st.subheader("Prawda × decyzja")
    st.dataframe(pd.crosstab(r["expected"], r["decision"]))
with right:
    st.subheader("Polityka × decyzja")
    st.dataframe(pd.crosstab(r["policy_expected"], r["decision"]))

st.subheader("Alerty")
only_bad = st.checkbox("Pokaż tylko niezgodne z polityką")
rv = r[r["decision"] != r["policy_expected"]] if only_bad else r
st.dataframe(rv[["customer_id", "customer_name", "sdn_name", "label", "expected", "policy_expected", "decision",
                 "rule", "confidence", "steps", "tools_used", "cost_usd", "seconds", "rationale"]],
             hide_index=True, width="stretch")

pick = st.selectbox("Pokaż ślad dochodzenia dla alertu", r["customer_id"] + " · " + r["customer_name"])
row = r[(r["customer_id"] + " · " + r["customer_name"]) == pick].iloc[0]
st.write(f"**Decyzja:** {row['decision']} ({row['rule']}) · **powinno być:** {row['policy_expected']}")
st.write(row["rationale"])
A.render_trace({"trace": json.loads(row["trace"]), "evidence": json.loads(row["evidence"]),
                "usage": {"model": s["model"], "steps": row["steps"], "api_calls": row["api_calls"],
                          "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
                          "cost_usd": row["cost_usd"], "seconds": row["seconds"]}})

st.header("Tabela do README")
st.code(E.markdown_table(runs), language="markdown")
