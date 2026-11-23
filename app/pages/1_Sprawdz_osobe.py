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

Odpowiedz tylko JSON-em: {{"decision": "TRUE_MATCH|FALSE_POSITIVE|ESCALATE", "rule": "R?", "rationale": "...", "confidence": 0.0-1.0}}"""
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
    r = client.messages.create(model="MODEL", max_tokens=1500, system=SYSTEM,
                               messages=[{"role": "user", "content": text}])
    t = next(b.text for b in r.content if b.type == "text")
    out = json.loads(t[t.find("{"):t.rfind("}") + 1])
    if out["decision"] != "ESCALATE" and float(out.get("confidence") or 0) < CONFIDENCE_THRESHOLD:
        out["rationale"] = (f"[Auto-eskalacja: pewność {out.get('confidence')} < {CONFIDENCE_THRESHOLD}, "
                            f"agent proponował {out['decision']}] " + out.get("rationale", ""))
        out["decision"] = "ESCALATE"
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
                            "customer_name": last["name"],
                            "customer_dob": last["dob"],
                            "customer_country": last["country"],
                            "sdn_id": int(hit["sdn_id"]),
                            "sdn_name": hit["matched_name"],
                            "agent_decision": out["decision"],
                            "agent_rule": out.get("rule"),
                            "agent_confidence": float(out.get("confidence") or 0),
                            "final_decision": final,
                            "overridden": final != out["decision"],
                            "reviewer": reviewer,
                            "comment": comment,
                            "model": "MODEL",
                        })
                        st.success(f"Zapisano w audit logu: {final}")

with tab2:
    file = st.file_uploader("Wgraj CSV z kolumną 'name'", type="csv")
    if file:
        df = pd.read_csv(file)
        out = []
        for n in df["name"]:
            r = screen(n)
            out.append({"name": n, "trafienia": len(r),
                        "najlepsze_trafienie": r["matched_name"].iloc[0] if len(r) else "",
                        "score": r["score"].iloc[0] if len(r) else None})
        result = pd.DataFrame(out)
        st.dataframe(result, width="stretch")
        st.download_button("Pobierz wyniki", result.to_csv(index=False), "wyniki.csv")

with tab3:
    if LOG.exists():
        rows = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
        st.write(f"{len(rows)} wpisów")
        st.dataframe(pd.DataFrame(rows).iloc[::-1], width="stretch")
    else:
        st.info("Dziennik jest pusty.")