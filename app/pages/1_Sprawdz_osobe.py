from pathlib import Path
from datetime import datetime
import json, hashlib
import pandas as pd
import streamlit as st
import anthropic
from dotenv import load_dotenv
from rapidfuzz import process, fuzz, utils

ROOT = Path(__file__).parent.parent.parent
DATA = ROOT / "data" / "processed"
LOG = DATA / "audit_log.jsonl"
load_dotenv(ROOT / ".env")
client = anthropic.Anthropic()
POLICY = (ROOT / "policy" / "sanctions_policy.md").read_text()
SYSTEM = f"""Jesteś analitykiem compliance w banku. Rozstrzygasz alerty sankcyjne WYŁĄCZNIE według poniższej polityki.

{POLICY}

Odpowiedz tylko JSON-em:
{{"decision": "TRUE_MATCH|FALSE_POSITIVE|ESCALATE", "rule": "R?", "rationale": "...", "confidence": 0.0-1.0,
  "factors": [{{"factor": "name|dob|nationality|passport", "client": "...", "sdn": "...", "result": "MATCH|MISMATCH|PARTIAL|UNKNOWN", "rule": "R?"}}]}}
W "factors" oceń osobno każdy czynnik: name, dob, nationality, passport (brak danych po którejkolwiek stronie = UNKNOWN)."""

MODEL = "claude-haiku-5-5"
CONFIDENCE_THRESHOLD = 0.8
POLICY_VERSION = hashlib.sha256(POLICY.encode()).hexdigest()[:12]

@st.cache_data
def load():
    return pd.read_csv(DATA / "names.csv"), pd.read_csv(DATA / "people.csv")

names, people = load()

def screen(query, threshold=85):
    hits = process.extract(query, names["name"], scorer=fuzz.token_set_ratio,
                           processor=utils.default_process, score_cutoff=threshold, limit=None)
    rows = [{"sdn_id": names.loc[idx, "id"], "matched_name": m, "score": round(s)} for m, s, idx in hits]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("score", ascending=False).drop_duplicates("sdn_id")
    return df.merge(people[["id", "dob", "nationality", "program"]],
                    left_on="sdn_id", right_on="id", how="left").drop(columns="id")

def ask_agent(name, dob, country, hit):
    text = (f"KLIENT: {name}; data ur.: {dob or 'brak danych'}; obywatelstwo: {country or 'brak danych'}; paszport: brak danych\n"
            f"LISTA SDN: {hit['matched_name']}; data ur.: {hit['dob']}; obywatelstwo: {hit['nationality']}; program: {hit['program']}\n"
            f"Podobieństwo nazwiska: {hit['score']}")
    r = client.messages.create(model=MODEL, max_tokens=1500, system=SYSTEM,
                               messages=[{"role": "user", "content": text}])
    t = next(b.text for b in r.content if b.type == "text")
    out = json.loads(t[t.find("{"):t.rfind("}") + 1])
    if not isinstance(out.get("factors"), list):
        out["factors"] = []
    conf = float(out.get("confidence") or 0)
    if out["decision"] != "ESCALATE" and conf < CONFIDENCE_THRESHOLD:
        out["rationale"] = (f"[Auto-eskalacja: pewność {conf} < {CONFIDENCE_THRESHOLD}, "
                            f"agent proponował {out['decision']}] " + str(out.get("rationale", "")))
        out["decision"] = "ESCALATE"
        out["auto_escalated"] = True
    return out

def append_audit(entry):
    prev = "0" * 64
    if LOG.exists():
        lines = LOG.read_text().strip().splitlines()
        if lines:
            prev = json.loads(lines[-1])["hash"]
    entry["prev_hash"] = prev
    text = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    entry["hash"] = hashlib.sha256(text.encode()).hexdigest()
    with open(LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

ICONS = {"TRUE_MATCH": "🚨", "FALSE_POSITIVE": "✅", "ESCALATE": "🟡"}
FACTOR_ICONS = {"MATCH": "✅ MATCH", "MISMATCH": "❌ MISMATCH", "PARTIAL": "🟠 PARTIAL", "UNKNOWN": "❔ UNKNOWN"}

st.title("Sprawdź osobę na liście sankcyjnej OFAC")
tab1, tab2, tab3 = st.tabs(["Jedna osoba", "Lista (CSV)", "Audit log"])

with tab1:
    name = st.text_input("Imię i nazwisko")
    c1, c2 = st.columns(2)
    dob = c1.text_input("Data urodzenia (opcjonalnie)", placeholder="np. 07 Oct 1952")
    country = c2.text_input("Obywatelstwo (opcjonalnie)", placeholder="np. Russia")
    use_agent = st.checkbox("Zapytaj agenta AI o decyzję (max 3 najlepsze trafienia)")

    if st.button("Sprawdź") and name:
        res = screen(name)
        decisions = []
        if use_agent and not res.empty:
            for _, hit in res.head(3).iterrows():
                with st.spinner(f"Agent analizuje: {hit['matched_name']}..."):
                    decisions.append((hit.to_dict(), ask_agent(name, dob, country, hit)))
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
                factors = [f for f in out.get("factors", []) if isinstance(f, dict)]
                if factors:
                    st.markdown("**Ocena czynników**")
                    ft = pd.DataFrame([{
                        "Czynnik": f.get("factor", ""),
                        "Klient": str(f.get("client", "")),
                        "Lista SDN": str(f.get("sdn", "")),
                        "Wynik": FACTOR_ICONS.get(str(f.get("result", "")).upper(), "❔ " + str(f.get("result", ""))),
                        "Reguła": f.get("rule", ""),
                    } for f in factors])
                    st.dataframe(ft, hide_index=True, width="stretch")
                st.write(out.get("rationale"))

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
                            "agent_decision": out["decision"],
                            "agent_rule": out.get("rule"),
                            "agent_confidence": float(out.get("confidence") or 0),
                            "agent_rationale": out.get("rationale"),
                            "auto_escalated": bool(out.get("auto_escalated")),
                            "factors": out.get("factors", []),
                            "final_decision": final,
                            "overridden": final != out["decision"],
                            "reviewer": reviewer,
                            "comment": comment,
                            "model": MODEL,
                            "policy_version": POLICY_VERSION,
                            "confidence_threshold": CONFIDENCE_THRESHOLD,
                        })
                        st.success(f"Zapisano w audit logu: {final}")

with tab2:
    file = st.file_uploader("Wgraj CSV z kolumną 'name' (opcjonalnie: 'dob', 'country')", type="csv")
    batch_agent = st.checkbox("Tryb agenta: agent rozstrzyga najlepsze trafienie każdej osoby (max 20 wierszy)")
    if batch_agent:
        st.warning("Każdy wiersz z trafieniem to jedno płatne zapytanie do API (Haiku: ułamek centa). Limit: 20 wierszy.")
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
                        a = {"decision": "ESCALATE", "rule": "BŁĄD", "confidence": 0,
                             "rationale": f"Błąd agenta: {e}", "factors": []}
                    rec.update({"decyzja": a["decision"], "reguła": a.get("rule"),
                                "pewność": a.get("confidence"),
                                "czynniki": "; ".join(f"{f.get('factor')}: {f.get('result')}"
                                                      for f in a.get("factors", []) if isinstance(f, dict)),
                                "uzasadnienie": a.get("rationale")})
                    append_audit({
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "alert_type": "customer_batch",
                        "customer_name": n, "customer_dob": d, "customer_country": c,
                        "sdn_id": int(hit["sdn_id"]), "sdn_name": hit["matched_name"],
                        "agent_decision": a["decision"], "agent_rule": a.get("rule"),
                        "agent_confidence": float(a.get("confidence") or 0),
                        "agent_rationale": a.get("rationale"),
                        "auto_escalated": bool(a.get("auto_escalated")),
                        "factors": a.get("factors", []),
                        "final_decision": a["decision"], "overridden": False,
                        "reviewer": "auto", "comment": "",
                        "model": MODEL, "policy_version": POLICY_VERSION,
                        "confidence_threshold": CONFIDENCE_THRESHOLD,
                    })
                else:
                    rec.update({"decyzja": "NO_HIT", "reguła": "", "pewność": None, "czynniki": "", "uzasadnienie": ""})
            out.append(rec)
            bar.progress((i + 1) / len(df), text=f"{i + 1}/{len(df)}: {n}")
        bar.empty()
        result = pd.DataFrame(out)
        if batch_agent and "decyzja" in result:
            st.write(result["decyzja"].value_counts().to_dict())
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
        st.dataframe(log_df.iloc[::-1], width="stretch")
    else:
        st.info("Dziennik jest pusty.")