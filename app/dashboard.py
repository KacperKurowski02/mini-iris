import sys
from pathlib import Path
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from evaluation import load_runs, load_results

DATA = Path(__file__).parent.parent / "data" / "processed"

customers = pd.read_csv(DATA / "customers.csv")
alerts = pd.read_csv(DATA / "alerts.csv")

st.set_page_config(page_title="Mini-Iris", layout="wide")
st.title("Mini-Iris: adjudykacja alertów sankcyjnych")

# --- Lejek: klienci -> alerty ---
st.header("1. Screening")
c1, c2, c3 = st.columns(3)
c1.metric("Klienci", f"{len(customers):,}")
c2.metric("Alerty (fuzzy match ≥ 85)", len(alerts))
c3.metric("Klienci z alertem", alerts["customer_id"].nunique())

# --- Wyniki agenta: najnowsza ewaluacja ---
st.header("2. Agent AI: wyniki ewaluacji")
runs = load_runs()
tools_runs = runs[runs["mode"] == "tools"] if not runs.empty else runs
if tools_runs.empty:
    st.info("Brak wyników ewaluacji. Uruchom ewaluację na stronie **5 · Ewaluacja** "
            "albo w terminalu: `python app/evaluation.py --n 24 --mode tools`.")
    st.stop()

latest = tools_runs.sort_values("timestamp").iloc[-1]
st.caption(f"Najnowszy przebieg agenta z narzędziami: {latest['n']} alertów ze znaną prawdą, "
           f"model {latest['model']}, {str(latest['timestamp'])[:16].replace('T', ' ')}. "
           "Szczegóły i wszystkie przebiegi na stronie 5 · Ewaluacja.")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Przepuszczone osoby z listy", int(latest["missed_true_matches"]),
          help="Musi być 0: przepuszczenie osoby objętej sankcjami to najgorszy błąd.")
c2.metric("Zablokowane osoby z listy", latest["true_matches_blocked"])
c3.metric("Fałszywe alarmy zamknięte automatycznie", latest["false_positives_auto_closed"])
c4.metric("Zgodność z polityką banku", f"{latest['policy_accuracy']:.0%}")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Eskalowane do człowieka", f"{latest['escalation_rate']:.0%}")
c2.metric("Śr. czas na alert", f"{latest['avg_seconds']:.1f} s")
c3.metric("Śr. koszt na alert", f"${latest['avg_cost_usd']:.4f}")
c4.metric("Śr. liczba kroków agenta", f"{latest['avg_steps']:.1f}")

st.subheader("Porównanie architektur (najnowszy przebieg każdego trybu)")
MODE_NAMES = {"tools": "agent z narzędziami", "multi": "zespół agentów", "single": "jedno zapytanie"}
last_per_mode = runs.sort_values("timestamp").groupby("mode").tail(1)
table = pd.DataFrame({
    "tryb": last_per_mode["mode"].map(MODE_NAMES).fillna(last_per_mode["mode"]),
    "alerty": last_per_mode["n"],
    "przepuszczone": last_per_mode["missed_true_matches"],
    "zablokowane": last_per_mode["true_matches_blocked"],
    "FP zamknięte": last_per_mode["false_positives_auto_closed"],
    "eskalacje": last_per_mode["escalation_rate"].map("{:.0%}".format),
    "zgodność z polityką": last_per_mode["policy_accuracy"].map("{:.0%}".format),
    "s / alert": last_per_mode["avg_seconds"],
    "$ / alert": last_per_mode["avg_cost_usd"].map("${:.4f}".format),
})
st.dataframe(table, hide_index=True, width="stretch")

results = load_results(latest["run_id"])
st.subheader("Prawda × decyzja agenta")
st.dataframe(pd.crosstab(results["expected"], results["decision"]))

# --- Bez agenta vs. z agentem ---
st.header("3. Bez agenta vs. z agentem")
min_per_alert = st.slider("Minuty analityka na 1 alert (ręcznie)", 2, 15, 5)

total = len(alerts)
true_alerts = int((alerts["label"] == "true_match").sum())
fp_alerts = total - true_alerts
fp_closed = round(fp_alerts * float(latest["fp_reduction"]))
human_alerts = total - fp_closed
before_h = total * min_per_alert / 60
after_h = human_alerts * min_per_alert / 60

c1, c2, c3 = st.columns(3)
c1.metric("Do człowieka: tylko porównanie nazwisk", total)
c2.metric("Do człowieka: z agentem", human_alerts,
          delta=f"-{total - human_alerts} alertów", delta_color="inverse")
c3.metric("Godziny pracy analityka", f"{after_h:.1f} h",
          delta=f"-{before_h - after_h:.1f} h (było {before_h:.1f} h)", delta_color="inverse")

st.bar_chart(pd.DataFrame({"Alerty do człowieka": [total, human_alerts]},
                          index=["Tylko nazwiska", "Z agentem AI"]))
st.caption(f"Szacunek: odsetek fałszywych alarmów zamkniętych automatycznie w ewaluacji "
           f"({float(latest['fp_reduction']):.0%}) zastosowany do {fp_alerts} fałszywych alarmów spośród {total}. "
           f"Wszystkie {true_alerts} prawdziwe trafienia i tak trafiają do człowieka (blokada, MLRO).")

# --- Decyzje ---
st.header("4. Decyzje agenta w ewaluacji")
choice = st.selectbox("Filtruj decyzję", ["wszystkie"] + sorted(results["decision"].unique()))
view = results if choice == "wszystkie" else results[results["decision"] == choice]
st.dataframe(view[["customer_name", "sdn_name", "label", "expected", "decision", "rule", "confidence", "rationale"]],
             width="stretch", hide_index=True)
