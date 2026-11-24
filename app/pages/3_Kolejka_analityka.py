import sys
from pathlib import Path
import json, hashlib
from datetime import datetime
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))
from iris_agent import read_log, append_audit, analyst_examples, FEEDBACK_K, FEEDBACK_MIN_SCORE

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

all_df = pd.DataFrame(rows)
for col, default in [("alert_type", "customer"), ("auto_escalated", False), ("overridden", False),
                     ("agent_rationale", ""), ("final_decision", ""), ("agent_decision", ""), ("resolves", "")]:
    if col not in all_df:
        all_df[col] = default
    all_df[col] = all_df[col].fillna(default)

# Rozwiązania analityka to osobne wpisy (audit log jest tylko do dopisywania, niczego nie nadpisujemy).
# Do metryk bierzemy pierwotne sprawy z decyzją końcową podmienioną na decyzję analityka.
is_res = all_df["alert_type"] == "analyst_resolution"
resolutions = all_df[is_res]
res_final = dict(zip(resolutions["resolves"], resolutions["final_decision"]))
df = all_df[~is_res].copy()
df["resolved_by_analyst"] = df["hash"].isin(res_final.keys())
df.loc[df["resolved_by_analyst"], "overridden"] = True
df["final_decision"] = df["hash"].map(res_final).fillna(df["final_decision"])
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
queue = df[(df["final_decision"] == "ESCALATE")].iloc[::-1]
cols = [c for c in ["timestamp", "alert_type", "customer_name", "customer_country", "sdn_name",
                    "agent_rule", "agent_confidence", "agent_rationale", "reviewer"] if c in queue]
if queue.empty:
    st.success("Kolejka pusta.")
else:
    st.write(f"{len(queue)} spraw czeka na analityka")
    st.dataframe(queue[cols], width="stretch", hide_index=True)

    st.markdown("**Rozstrzygnij sprawę z kolejki**")
    st.caption("Twoja decyzja i komentarz trafią do audit logu i do bazy przykładów. "
               "Agent dostanie je jako wskazówkę przy podobnych sprawach w przyszłości.")
    labels = {h: f"{r.get('timestamp', '')} · {r.get('customer_name', '?')} ↔ {r.get('sdn_name') or '—'} "
                 f"({r.get('agent_rule') or '?'}, pewność {float(r.get('agent_confidence') or 0):.2f})"
              for h, r in zip(queue["hash"], queue.to_dict("records"))}
    pick = st.selectbox("Sprawa", list(labels), format_func=labels.get)
    case = queue[queue["hash"] == pick].iloc[0].to_dict()
    st.info(f"**Uzasadnienie agenta:** {case.get('agent_rationale') or 'brak'}")
    with st.form("resolve"):
        final = st.radio("Decyzja analityka", ["FALSE_POSITIVE", "TRUE_MATCH"], horizontal=True)
        comment = st.text_area("Dlaczego? (wymagane: to zdanie agent zobaczy przy podobnych sprawach)",
                               placeholder="np. Data urodzenia różni się o 15 lat, inny kraj. To nie ta osoba.")
        reviewer = st.text_input("Analityk", value="analityk_1")
        if st.form_submit_button("Zapisz rozstrzygnięcie"):
            if not comment.strip():
                st.error("Dodaj komentarz. Bez niego przykład niczego agenta nie nauczy.")
            elif not reviewer.strip() or reviewer.strip() == "auto":
                st.error("Podaj nazwę analityka.")
            else:
                def clean(v):
                    return None if pd.isna(v) else v
                append_audit({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "alert_type": "analyst_resolution",
                    "resolves": pick,
                    "resolved_alert_type": case.get("alert_type"),
                    "customer_name": clean(case.get("customer_name")),
                    "customer_dob": clean(case.get("customer_dob")),
                    "customer_country": clean(case.get("customer_country")),
                    "sdn_id": int(case["sdn_id"]) if clean(case.get("sdn_id")) is not None else None,
                    "sdn_name": clean(case.get("sdn_name")),
                    "agent_decision": case.get("agent_decision"),
                    "agent_rule": clean(case.get("agent_rule")),
                    "final_decision": final,
                    "overridden": True,
                    "reviewer": reviewer.strip(),
                    "comment": comment.strip(),
                    "policy_version": clean(case.get("policy_version")),
                })
                st.success(f"Zapisano: {final}. Sprawa zniknęła z kolejki i trafiła do bazy przykładów.")
                st.rerun()

# --- Baza przykładów ---
st.subheader("📚 Baza przykładów od analityków")
examples = analyst_examples(rows)
st.write(f"Agent korzysta z decyzji ludzi: przy każdym nowym alercie szuka do {FEEDBACK_K} najbardziej podobnych "
         f"spraw (ten sam wpis z listy, podobne nazwisko, ten sam kraj, ten sam typ alertu; "
         f"minimalne podobieństwo {FEEDBACK_MIN_SCORE}) i dostaje je w treści alertu jako wskazówkę, nie dowód.")
if examples:
    st.dataframe(pd.DataFrame([{
        "kiedy": e.get("timestamp"), "klient": e.get("customer_name"), "kraj": e.get("customer_country"),
        "lista SDN": e.get("sdn_name"), "agent": e.get("agent_decision"), "analityk": e.get("final_decision"),
        "komentarz": e.get("comment"), "kto": e.get("reviewer"),
        "źródło": "kolejka" if e.get("alert_type") == "analyst_resolution" else "przegląd decyzji",
    } for e in reversed(examples)]), width="stretch", hide_index=True)
else:
    st.info("Brak przykładów. Rozstrzygnij sprawę z kolejki albo zmień decyzję agenta na stronie 1 lub 2 i dodaj komentarz.")

# --- Integralność ---
st.subheader("🔒 Integralność audit logu")
st.write("Każdy wpis zawiera hash poprzedniego wpisu. Zmiana lub usunięcie dowolnego wpisu przerywa łańcuch.")
if st.button("Zweryfikuj łańcuch hashy"):
    ok, bad, why = verify_chain(rows)
    if ok:
        st.success(f"✅ Łańcuch nienaruszony: {len(rows)} wpisów zweryfikowanych")
    else:
        st.error(f"❌ Łańcuch przerwany na wpisie nr {bad + 1}: {why}")
