from pathlib import Path
import pandas as pd
import streamlit as st

DATA = Path(__file__).parent.parent / "data" / "processed"

customers = pd.read_csv(DATA / "customers.csv")
alerts = pd.read_csv(DATA / "alerts.csv")
decisions = pd.read_csv(DATA / "decisions.csv")

st.set_page_config(page_title="Mini-Iris", layout="wide")
st.title("Mini-Iris: adjudykacja alertów sankcyjnych")

# --- Lejek: klienci -> alerty -> decyzje ---
st.header("1. Screening")
c1, c2, c3 = st.columns(3)
c1.metric("Klienci", f"{len(customers):,}")
c2.metric("Alerty (fuzzy match ≥ 85)", len(alerts))
c3.metric("Klienci z alertem", alerts["customer_id"].nunique())

# --- Wyniki agenta ---
st.header("2. Agent LLM (próbka)")
n = len(decisions)
auto_closed = (decisions["decision"] == "FALSE_POSITIVE").sum()
escalated = (decisions["decision"] == "ESCALATE").sum()
accuracy = (decisions["decision"] == decisions["expected"]).mean()
missed = ((decisions["expected"] == "TRUE_MATCH") & (decisions["decision"] == "FALSE_POSITIVE")).sum()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Skuteczność", f"{accuracy:.0%}")
c2.metric("Zamknięte automatycznie", f"{auto_closed}/{n}")
c3.metric("Eskalowane do człowieka", escalated)
c4.metric("Przepuszczone trafienia", missed)
c5.metric("Śr. czas decyzji", f"{decisions['seconds'].mean():.1f} s")

st.subheader("Macierz pomyłek")
st.dataframe(pd.crosstab(decisions["expected"], decisions["decision"]))

st.subheader("Rozkład decyzji")
st.bar_chart(decisions["decision"].value_counts())

# --- Szczegóły ---
st.header("3. Decyzje")
choice = st.selectbox("Filtruj decyzję", ["wszystkie"] + sorted(decisions["decision"].unique()))
view = decisions if choice == "wszystkie" else decisions[decisions["decision"] == choice]
st.dataframe(view[["customer_name", "sdn_name", "expected", "decision", "rule", "confidence", "rationale"]],
             width="stretch")