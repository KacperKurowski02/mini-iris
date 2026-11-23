import sys
from pathlib import Path
from datetime import datetime
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))  # żeby zaimportować app/iris_agent.py
from iris_agent import (PAYMENT_POLICY, HIGH_RISK, policy_version, ICONS, screen, append_audit,
                        investigate_payment, audit_fields, render_trace, render_factors)

POLICY_VERSION = policy_version(PAYMENT_POLICY)


def ask_agent(p, hits, country_flag):
    """Agent z narzędziami: sprawdza kraj, kod BIC banku, wpisy SDN i wcześniejsze decyzje, potem decyduje."""
    return investigate_payment(p, hits, country_flag)


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
                render_factors(out, client_label="Przelew")
                st.write(out.get("rationale"))
                render_trace(out)

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
                            **audit_fields(out),
                            "final_decision": final,
                            "overridden": final != out["decision"],
                            "reviewer": reviewer,
                            "comment": comment,
                            "policy_version": POLICY_VERSION,
                        })
                        st.success(f"Zapisano w audit logu: {final}")
