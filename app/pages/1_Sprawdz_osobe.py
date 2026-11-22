from pathlib import Path
import pandas as pd
import streamlit as st
from rapidfuzz import process, fuzz, utils

DATA = Path(__file__).parent.parent.parent / "data" / "processed"

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

st.title("Sprawdź osobę na liście sankcyjnej OFAC")
tab1, tab2 = st.tabs(["Jedna osoba", "Lista (CSV)"])

with tab1:
    name = st.text_input("Imię i nazwisko")
    if st.button("Sprawdź") and name:
        res = screen(name)
        if res.empty:
            st.success("✅ Brak trafień na liście OFAC SDN")
        else:
            st.warning(f"⚠️ {len(res)} potencjalnych trafień: porównaj datę urodzenia i kraj")
            st.dataframe(res, width="stretch")

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