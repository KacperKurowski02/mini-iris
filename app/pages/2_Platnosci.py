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
POLICY = (ROOT / "policy" / "payment_policy.md").read_text()
SYSTEM = f"""Jesteś analitykiem compliance w banku. Rozstrzygasz alerty z screeningu PŁATNOŚCI WYŁĄCZNIE według poniższej polityki.

{POLICY}

Odpowiedz tylko JSON-em:
{{"decision": "TRUE_MATCH|FALSE_POSITIVE|ESCALATE", "rule": "P?", "rationale": "...", "confidence": 0.0-1.0,
  "factors": [{{"factor": "beneficiary_name|ordering_party_name|country|bank|payment_purpose", "client": "...", "sdn": "...", "result": "MATCH|MISMATCH|PARTIAL|UNKNOWN", "rule": "P?"}}]}}
W "factors" oceń osobno każdy czynnik: beneficiary_name, ordering_party_name, country, bank, payment_purpose. W polu "client" podaj dane z przelewu, w polu "sdn" dane z listy (lub informację o kraju objętym sankcjami)."""

MODEL = "claude-haiku-5-5"
CONFIDENCE_THRESHOLD = 0.8
POLICY_VERSION = hashlib.sha256(POLICY.encode()).hexdigest()[:12]

# Kraje wysokiego ryzyka (uproszczenie: mała, ręcznie wpisana lista)
HIGH_RISK = {"iran", "north korea", "korea, north", "dprk", "syria", "cuba", "russia", "belarus", "crimea",
             "korea północna", "kuba", "rosja", "białoruś", "krym"}

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

def ask_agent(p, hits, country_flag):
    lines = [f"PRZELEW: zleceniodawca: {p['ordering']}; beneficjent: {p['beneficiary']}; kraj beneficjenta: {p['country'] or 'brak danych'}; "
             f"bank beneficjenta: {p['bank'] or 'brak danych'}; kwota: {p['amount']} {p['currency']}; tytuł: {p['title'] or 'brak danych'}",
             f"Kraj beneficjenta na liście krajów wysokiego ryzyka: {'TAK' if country_flag else 'NIE'}"]
    if hits:
        for h in hits:
            lines.append(f"TRAFIENIE SDN ({h['party']}): {h['matched_name']}; obywatelstwo: {h['nationality']}; "
                         f"data ur.: {h['dob']}; program: {h['program']}; podobieństwo nazwiska: {h['score']}")
    else:
        lines.append("Brak trafień nazwisk na liście SDN.")
    r = client.messages.create(model=MODEL, max_tokens=1500, system=SYSTEM,
                               messages=[{"role": "user", "content": "\n".join(lines)}])
    t = next(b.text for b in r.content if b.type == "text")
    out = json.loads(t[t.find("{"):t.rfind("}") + 1])
    if not isinstance(out.get("factors"), list):
        out["factors"] = []
    conf = float(out.get("confidence") or 0)
    if out["decision"] != "ESCALATE" and conf < CONFIDENCE_THRESHOLD:
        out["rationale"] = (f"[Auto-eskalacja: pewność {conf} < {CONFIDENCE_THRESHOLD}, "
                            f"agent proponował {out['decision']}] " + str(out.get("rationale", "")))
        out["decision"] = "ESCALATE"
    if country_flag and out["decision"] == "FALSE_POSITIVE":
        out["rationale"] = "[Auto-eskalacja: kraj wysokiego ryzyka (P4)] " + str(out.get("rationale", ""))
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
FACTOR_ICONS = {"MATCH": "✅ MATCH", "MISMATCH": "❌ MISMATCH", "PARTIAL": "🟠 PARTIAL", "UNKNOWN": "❔ UNKNOWN"}

# Przykładowe przelewy do demo (jedno kliknięcie)
SAMPLES = {
    "(wpisz ręcznie)": None,
    "Przykład 1: fałszywy alarm (czynsz w Hiszpanii)": dict(
        ordering="Jan Kowalski", beneficiary="Ismael Garcia", country="Spain",
        bank="Banco Santander, Madrid (BSCHESMM)", amount=1200.0, currency="EUR",
        title="Czynsz za mieszkanie, listopad 2026"),
    "Przykład 2: prawdziwe trafienie (Iran)": dict(
        ordering="Nowak Import Sp. z o.o.", beneficiary="Morteza Rezaie", country="Iran",
        bank="Bank Sepah, Tehran (SEPBIRTH)", amount=48000.0, currency="USD",
        title="Consulting services"),
    "Przykład 3: do eskalacji (Syria, bez trafienia nazwiska)": dict(
        ordering="Polmax Trade S.A.", beneficiary="Global Supply LLC", country="Syria",
        bank="Commercial Bank of Syria, Damascus (CBOSSYDA)", amount=15500.0, currency="USD",
        title="Payment for goods"),
}
FIELDS = ["ordering", "beneficiary", "country", "bank", "amount", "currency", "title"]

def fill_sample():
    s = SAMPLES.get(st.session_state["sample"])
    if s:
        for k in FIELDS:
            st.session_state[f"pay_{k}"] = s[k]

st.title("Screening płatności")
st.caption("Sprawdza beneficjenta i zleceniodawcę przelewu na liście OFAC SDN oraz kraj beneficjenta.")

st.selectbox("Szybkie demo: wybierz przykładowy przelew", list(SAMPLES), key="sample", on_change=fill_sample)

c1, c2 = st.columns(2)
ordering = c1.text_input("Zleceniodawca", key="pay_ordering")
beneficiary = c2.text_input("Beneficjent", key="pay_beneficiary")
country = c1.text_input("Kraj beneficjenta", key="pay_country", placeholder="np. Spain")
bank = c2.text_input("Bank beneficjenta / BIC", key="pay_bank")
amount = c1.number_input("Kwota", min_value=0.0, step=100.0, key="pay_amount")
currency = c2.selectbox("Waluta", ["PLN", "EUR", "USD", "GBP", "CHF"], key="pay_currency")
title = st.text_input("Tytuł przelewu", key="pay_title")
use_agent = st.checkbox("Zapytaj agenta AI o decyzję", value=True)

if st.button("Sprawdź przelew") and (beneficiary or ordering):
    p = dict(ordering=ordering, beneficiary=beneficiary, country=country, bank=bank,
             amount=amount, currency=currency, title=title)
    hits = []
    for party, n in [("beneficjent", beneficiary), ("zleceniodawca", ordering)]:
        if n:
            r = screen(n)
            for h in r.head(3).to_dict("records"):
                hits.append({**h, "party": party})
    country_flag = country.strip().lower() in HIGH_RISK
    out = None
    if use_agent and (hits or country_flag):
        with st.spinner("Agent analizuje przelew..."):
            out = ask_agent(p, hits, country_flag)
    st.session_state["pay_last"] = {"p": p, "hits": hits, "flag": country_flag, "out": out}

last = st.session_state.get("pay_last")
if last:
    p, hits, flag, out = last["p"], last["hits"], last["flag"], last["out"]
    if not hits and not flag:
        st.success("✅ Brak trafień: przelew zwolniony automatycznie")
    else:
        st.divider()
        left, right = st.columns(2)
        with left:
            st.markdown("#### Stary system: tylko nazwiska i kraj")
            for h in hits:
                st.error(f"🔔 ALERT ({h['party']}): {h['matched_name']} (podobieństwo {h['score']})")
            if flag:
                st.error(f"🔔 ALERT: kraj wysokiego ryzyka ({p['country']})")
            st.write("Przelew zostaje wstrzymany. Analityk musi ręcznie sprawdzić strony, kraj, bank i tytuł, "
                     "a klient czeka na przelew.")
        with right:
            st.markdown("#### Mini-Iris: z agentem AI")
            if out is None:
                st.info("Agent nie był pytany (zaznacz pole wyżej).")
            else:
                st.subheader(f"{ICONS.get(out['decision'], '❔')} {out['decision']}")
                st.write(f"**Reguła:** {out.get('rule')} · **Pewność:** {out.get('confidence')}")
                factors = [f for f in out.get("factors", []) if isinstance(f, dict)]
                if factors:
                    st.markdown("**Ocena czynników**")
                    st.dataframe(pd.DataFrame([{
                        "Czynnik": f.get("factor", ""),
                        "Przelew": str(f.get("client", "")),
                        "Lista SDN": str(f.get("sdn", "")),
                        "Wynik": FACTOR_ICONS.get(str(f.get("result", "")).upper(), "❔ " + str(f.get("result", ""))),
                        "Reguła": f.get("rule", ""),
                    } for f in factors]), hide_index=True, width="stretch")
                st.write(out.get("rationale"))

                with st.form("pay_review"):
                    st.markdown("**Przegląd analityka**")
                    verdict = st.radio("Decyzja", ["Zatwierdzam decyzję agenta",
                                                   "Zmieniam na: TRUE_MATCH",
                                                   "Zmieniam na: FALSE_POSITIVE"])
                    comment = st.text_input("Komentarz")
                    reviewer = st.text_input("Analityk", value="analityk_1")
                    if st.form_submit_button("Zapisz do audit logu"):
                        final = out["decision"] if verdict.startswith("Zatwierdzam") else verdict.split(": ")[1]
                        top = hits[0] if hits else {}
                        append_audit({
                            "timestamp": datetime.now().isoformat(timespec="seconds"),
                            "alert_type": "payment",
                            "customer_name": p["beneficiary"],
                            "customer_country": p["country"],
                            "ordering_party": p["ordering"],
                            "beneficiary_bank": p["bank"],
                            "amount": float(p["amount"]),
                            "currency": p["currency"],
                            "payment_title": p["title"],
                            "high_risk_country": flag,
                            "sdn_id": int(top["sdn_id"]) if top else None,
                            "sdn_name": top.get("matched_name"),
                            "agent_decision": out["decision"],
                            "agent_rule": out.get("rule"),
                            "agent_confidence": float(out.get("confidence") or 0),
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
