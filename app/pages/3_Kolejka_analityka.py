from pathlib import Path
import json, hashlib
import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent.parent.parent
LOG = ROOT / "data" / "processed" / "audit_log.jsonl"

def read_log():
    if not LOG.exists():
        return []
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]

def verify_chain(rows):
    """Sprawdza łańcuch hashy. Zwraca (ok, numer_złego_wpisu, powód)."""
    prev = "0" * 64
    for i, row in enumerate(rows):
        row = dict(row)
        h = row.pop("hash", None)
        if row.get("prev_hash") != prev:
            return False, i, "prev_hash nie zgadza się z hashem poprzedniego wpisu (wpis usunięty lub przestawiony)"
        calc = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if calc != h:
            return False, i, "treść wpisu nie pasuje do jego hasha (wpis zmieniony po zapisaniu)"
        prev = h
    return True, None, ""

st.title("Kolejka analityka i metryki operacyjne")
rows = read_log()
if not rows:
    st.info("Audit log jest pusty. Zapisz kilka decyzji na stronach 1 lub 2.")
    st.stop()

df = pd.DataFrame(rows)
for col, default in [("alert_type", "customer"), ("auto_escalated", False), ("overridden", False),
                     ("agent_rationale", ""), ("final_decision", ""), ("agent_decision", "")]:
    if col not in df:
        df[col] = default
    df[col] = df[col].fillna(default)
auto_esc = df["auto_escalated"].astype(bool) | df["agent_rationale"].astype(str).str.startswith("[Auto-eskalacja")

# --- Metryki ---
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Wszystkie decyzje", len(df))
c2.metric("Zamknięte jako FALSE_POSITIVE", int((df["final_decision"] == "FALSE_POSITIVE").sum()))
c3.metric("TRUE_MATCH (blokada)", int((df["final_decision"] == "TRUE_MATCH").sum()))
human = df[df["reviewer"].astype(str) != "auto"] if "reviewer" in df else df
rate = human["overridden"].astype(bool).mean() if len(human) else 0
c4.metric("Analityk zmienił agenta", f"{rate:.0%}", help="Odsetek decyzji przejrzanych przez człowieka, w których analityk zmienił decyzję agenta")
c5.metric("Auto-eskalacje", int(auto_esc.sum()), help="Agent był niepewny (pewność < próg) albo kraj wysokiego ryzyka")

left, right = st.columns(2)
with left:
    st.subheader("Decyzje końcowe")
    st.bar_chart(df["final_decision"].value_counts())
with right:
    st.subheader("Typ alertu")
    st.bar_chart(df["alert_type"].value_counts())

st.subheader("Decyzja końcowa × typ alertu")
st.dataframe(pd.crosstab(df["final_decision"], df["alert_type"]))

# --- Kolejka ---
st.subheader("🟡 Otwarta kolejka: ESCALATE do decyzji człowieka")
queue = df[df["final_decision"] == "ESCALATE"]
cols = [c for c in ["timestamp", "alert_type", "customer_name", "customer_country", "sdn_name",
                    "agent_rule", "agent_confidence", "agent_rationale", "reviewer"] if c in queue]
if queue.empty:
    st.success("Kolejka pusta.")
else:
    st.write(f"{len(queue)} spraw czeka na analityka")
    st.dataframe(queue[cols].iloc[::-1], width="stretch", hide_index=True)

# --- Integralność ---
st.subheader("🔒 Integralność audit logu")
st.write("Każdy wpis zawiera hash poprzedniego wpisu. Zmiana lub usunięcie dowolnego wpisu przerywa łańcuch.")
if st.button("Zweryfikuj łańcuch hashy"):
    ok, bad, why = verify_chain(rows)
    if ok:
        st.success(f"✅ Łańcuch nienaruszony: {len(rows)} wpisów zweryfikowanych")
    else:
        st.error(f"❌ Łańcuch przerwany na wpisie nr {bad + 1}: {why}")
