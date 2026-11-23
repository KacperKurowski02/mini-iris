import sys
from pathlib import Path
from datetime import datetime
import json
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))  # żeby zaimportować app/iris_agent.py
from iris_agent import (MODES, CONFIDENCE_THRESHOLD, SANCTIONS_POLICY, policy_version, LOG, ICONS,
                        screen, append_audit, investigate_customer, audit_fields, render_trace, render_factors)

POLICY_VERSION = policy_version(SANCTIONS_POLICY)


AGENT_MODES = ["tools", "multi"]


def ask_agent(name, dob, country, hit, mode="tools"):
    """mode="tools": jeden agent z narzędziami. mode="multi": Śledczy -> Decydent -> Recenzent."""
    return investigate_customer(name, dob, country, hit, mode=mode)


st.title("Sprawdź osobę na liście sankcyjnej OFAC")
tab1, tab2, tab3 = st.tabs(["Jedna osoba", "Lista (CSV)", "Audit log"])

with tab1:
    name = st.text_input("Imię i nazwisko")
    c1, c2 = st.columns(2)
    dob = c1.text_input("Data urodzenia (opcjonalnie)", placeholder="np. 07 Oct 1952")
    country = c2.text_input("Obywatelstwo (opcjonalnie)", placeholder="np. Russia")
    use_agent = st.checkbox("Zapytaj agenta AI o decyzję (max 3 najlepsze trafienia)")
    mode = st.radio("Tryb agenta", AGENT_MODES, format_func=MODES.get, horizontal=True,
                    help="Zespół agentów jest dokładniejszy (recenzent sprawdza decyzję), ale robi ok. 2× więcej zapytań.")

    if st.button("Sprawdź") and name:
        res = screen(name)
        decisions = []
        if use_agent and not res.empty:
            for _, hit in res.head(3).iterrows():
                with st.spinner(f"Agent analizuje: {hit['matched_name']}..."):
                    decisions.append((hit.to_dict(), ask_agent(name, dob, country, hit, mode)))
        st.session_state["last"] = {"name": name, "dob": dob, "country": country,
                                    "res": res, "decisions": decisions}

    last = st.session_state.get("last")
    if last:
        res = last["res"]
        if res.empty:
            st.success("✅ Brak trafień na liście OFAC SDN")
        else:
            st.warning(f"⚠️ {len(res)} potencjalnych trafień")
            st.dataframe(res, width="stretch")

        for i, (hit, out) in enumerate(last["decisions"]):
            st.divider()
            left, right = st.columns(2)
            with left:
                st.markdown("#### Stary system: tylko nazwiska")
                st.error(f"🔔 ALERT: {hit['matched_name']} (podobieństwo {hit['score']})")
                st.write("Alert trafia do kolejki. Analityk musi ręcznie porównać datę urodzenia, kraj i dokumenty, co zajmuje ok. 5–15 minut.")
            with right:
                st.markdown("#### Mini-Iris: z agentem AI")
                st.subheader(f"{ICONS.get(out['decision'], '❔')} {out['decision']}")
                st.write(f"**Reguła:** {out.get('rule')} · **Pewność:** {out.get('confidence')}")
                render_factors(out)
                st.write(out.get("rationale"))
                render_trace(out)

                with st.form(f"review_{i}"):
                    st.markdown("**Przegląd analityka**")
                    verdict = st.radio("Decyzja", ["Zatwierdzam decyzję agenta",
                                                   "Zmieniam na: TRUE_MATCH",
                                                   "Zmieniam na: FALSE_POSITIVE"])
                    comment = st.text_input("Komentarz")
                    reviewer = st.text_input("Analityk", value="analityk_1")
                    if st.form_submit_button("Zapisz do audit logu"):
                        final = out["decision"] if verdict.startswith("Zatwierdzam") else verdict.split(": ")[1]
                        append_audit({
                            "timestamp": datetime.now().isoformat(timespec="seconds"),
                            "alert_type": "customer",
                            "customer_name": last["name"],
                            "customer_dob": last["dob"],
                            "customer_country": last["country"],
                            "sdn_id": int(hit["sdn_id"]),
                            "sdn_name": hit["matched_name"],
                            **audit_fields(out),
                            "final_decision": final,
                            "overridden": final != out["decision"],
                            "reviewer": reviewer,
                            "comment": comment,
                            "policy_version": POLICY_VERSION,
                        })
                        st.success(f"Zapisano w audit logu: {final}")

with tab2:
    file = st.file_uploader("Wgraj CSV z kolumną 'name' (opcjonalnie: 'dob', 'country')", type="csv")
    batch_agent = st.checkbox("Tryb agenta: agent rozstrzyga najlepsze trafienie każdej osoby (max 20 wierszy)")
    if batch_agent:
        st.warning("Każdy wiersz z trafieniem to jedno dochodzenie agenta (kilka zapytań do API; Haiku: ułamek centa). Limit: 20 wierszy.")
    if file:
        df = pd.read_csv(file)
        if batch_agent:
            df = df.head(20)
        out = []
        bar = st.progress(0.0, text="Screening...")
        for i, row in enumerate(df.to_dict("records")):
            n = str(row.get("name", ""))
            d = row.get("dob") if pd.notna(row.get("dob", None)) else ""
            c = row.get("country") if pd.notna(row.get("country", None)) else ""
            r = screen(n)
            rec = {"name": n, "trafienia": len(r),
                   "najlepsze_trafienie": r["matched_name"].iloc[0] if len(r) else "",
                   "score": r["score"].iloc[0] if len(r) else None}
            if batch_agent:
                if len(r):
                    hit = r.iloc[0]
                    try:
                        a = ask_agent(n, d, c, hit)
                    except Exception as e:
                        a = {"decision": "ESCALATE", "rule": "BŁĄD", "confidence": 0, "auto_escalated": True,
                             "rationale": f"Błąd agenta: {e}", "factors": [], "trace": [], "usage": {}}
                    rec.update({"decyzja": a["decision"], "reguła": a.get("rule"),
                                "pewność": a.get("confidence"),
                                "czynniki": "; ".join(f"{f.get('factor')}: {f.get('result')}"
                                                      for f in a.get("factors", []) if isinstance(f, dict)),
                                "kroki": a.get("usage", {}).get("steps"),
                                "koszt_usd": a.get("usage", {}).get("cost_usd"),
                                "uzasadnienie": a.get("rationale")})
                    append_audit({
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "alert_type": "customer_batch",
                        "customer_name": n, "customer_dob": d, "customer_country": c,
                        "sdn_id": int(hit["sdn_id"]), "sdn_name": hit["matched_name"],
                        **audit_fields(a),
                        "final_decision": a["decision"], "overridden": False,
                        "reviewer": "auto", "comment": "",
                        "policy_version": POLICY_VERSION,
                    })
                else:
                    rec.update({"decyzja": "NO_HIT", "reguła": "", "pewność": None, "czynniki": "",
                                "kroki": 0, "koszt_usd": 0.0, "uzasadnienie": ""})
            out.append(rec)
            bar.progress((i + 1) / len(df), text=f"{i + 1}/{len(df)}: {n}")
        bar.empty()
        result = pd.DataFrame(out)
        if batch_agent and "decyzja" in result:
            st.write(result["decyzja"].value_counts().to_dict())
            st.write(f"Łączny koszt agenta: ${result['koszt_usd'].fillna(0).sum():.4f}")
            st.success("Decyzje agenta zapisano w audit logu (reviewer: auto).")
        st.dataframe(result, width="stretch")
        st.download_button("Pobierz wyniki", result.to_csv(index=False), "wyniki.csv")

with tab3:
    if LOG.exists():
        rows = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
        st.write(f"{len(rows)} wpisów")
        log_df = pd.DataFrame(rows)
        if "factors" in log_df:
            log_df["factors"] = log_df["factors"].apply(
                lambda v: "; ".join(f"{f.get('factor')}: {f.get('result')}" for f in v) if isinstance(v, list) else "")
        if "agent_trace" in log_df:
            log_df["agent_trace"] = log_df["agent_trace"].apply(
                lambda v: " → ".join(t.get("tool", "") for t in v) if isinstance(v, list) else "")
        for col in ("agent_usage", "agent_evidence"):
            if col in log_df:
                log_df[col] = log_df[col].apply(lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else "")
        st.dataframe(log_df.iloc[::-1], width="stretch")
    else:
        st.info("Dziennik jest pusty.")