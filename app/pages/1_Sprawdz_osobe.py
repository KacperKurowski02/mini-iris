from pathlib import Path
import json
import pandas as pd
import streamlit as st
import anthropic
from dotenv import load_dotenv
from rapidfuzz import process, fuzz, utils

ROOT = Path(__file__).parent.parent.parent
DATA = ROOT / "data" / "processed"
load_dotenv(ROOT / ".env")
client = anthropic.Anthropic()
POLICY = (ROOT / "policy" / "sanctions_policy.md").read_text()
SYSTEM = f"""Jesteś analitykiem compliance w banku. Rozstrzygasz alerty sankcyjne WYŁĄCZNIE według poniższej polityki.

{POLICY}

Odpowiedz tylko JSON-em: {{"decision": "TRUE_MATCH|FALSE_POSITIVE|ESCALATE", "rule": "R?", "rationale": "...", "confidence": 0.0-1.0}}"""

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
    r = client.messages.create(model="claude-haiku-5-5", max_tokens=1500, system=SYSTEM,
                               messages=[{"role": "user", "content": text}])
    t = next(b.text for b in r.content if b.type == "text")
    return json.loads(t[t.find("{"):t.rfind("}") + 1])

st.title("Sprawdź osobę na liście sankcyjnej OFAC")
tab1, tab2 = st.tabs(["Jedna osoba", "Lista (CSV)"])

with tab1:
    name = st.text_input("Imię i nazwisko")
    c1, c2 = st.columns(2)
    dob = c1.text_input("Data urodzenia (opcjonalnie)", placeholder="np. 07 Oct 1952")
    country = c2.text_input("Obywatelstwo (opcjonalnie)", placeholder="np. Russia")
    use_agent = st.checkbox("Zapytaj agenta AI o decyzję (max 3 najlepsze trafienia)")

    if st.button("Sprawdź") and name:
        res = screen(name)
        if res.empty:
            st.success("✅ Brak trafień na liście OFAC SDN")
        else:
            st.warning(f"⚠️ {len(res)} potencjalnych trafień")
            st.dataframe(res, width="stretch")
            if use_agent:
                for _, hit in res.head(3).iterrows():
                    with st.spinner(f"Agent analizuje: {hit['matched_name']}..."):
                        out = ask_agent(name, dob, country, hit)
                    icon = {"TRUE_MATCH": "🚨", "FALSE_POSITIVE": "✅", "ESCALATE": "🟡"}.get(out["decision"], "❔")
                    st.subheader(f"{icon} {out['decision']}: {hit['matched_name']}")
                    st.write(f"**Reguła:** {out.get('rule')} · **Pewność:** {out.get('confidence')}")
                    st.write(out.get("rationale"))

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