"""Mini-Iris: wspólny silnik agenta z narzędziami (tool use).

Agent nie dostaje wszystkich danych "na tacy" w jednym zapytaniu. Dostaje sam alert i zestaw
narzędzi (wyszukiwanie na liście SDN, pełny wpis z listy, dane klienta z systemu banku,
porównanie dat, ryzyko kraju, kod BIC, wcześniejsze decyzje). Sam decyduje, których użyć,
w pętli zbiera dowody, a na końcu wywołuje narzędzie submit_decision z decyzją.
Każdy krok trafia do "śladu" (trace), który pokazujemy w UI i zapisujemy w audit logu.
"""
from pathlib import Path
from functools import lru_cache
from datetime import datetime
import json, hashlib, re, time

import pandas as pd
import anthropic
from dotenv import load_dotenv
from rapidfuzz import process, fuzz, utils

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data" / "processed"
LOG = DATA / "audit_log.jsonl"
load_dotenv(ROOT / ".env")

# --- Ustawienia (jedno miejsce dla wszystkich stron) ---
MODEL = "claude-haiku-5-5"          # najtańszy model do developmentu
CONFIDENCE_THRESHOLD = 0.8          # poniżej tej pewności decyzja zmienia się na ESCALATE
SCREENING_THRESHOLD = 85            # próg podobieństwa nazwiska, od którego powstaje alert
MAX_STEPS = 8                       # maksymalna liczba rund agenta (bezpiecznik kosztów)
# Cena za 1 mln tokenów (wejście, wyjście) w USD
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}

SANCTIONS_POLICY = (ROOT / "policy" / "sanctions_policy.md").read_text()
PAYMENT_POLICY = (ROOT / "policy" / "payment_policy.md").read_text()


def policy_version(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# Kraje wysokiego ryzyka (uproszczenie: mała, ręcznie wpisana lista)
HIGH_RISK_COUNTRIES = ["Iran", "Korea Północna", "Syria", "Kuba", "Rosja", "Białoruś", "Krym"]
HIGH_RISK = {"iran", "north korea", "korea, north", "dprk", "syria", "cuba", "russia", "belarus", "crimea",
             "korea północna", "kuba", "rosja", "białoruś", "krym"}

# Kod kraju w BIC (znaki 5-6) -> nazwa kraju
BIC_COUNTRIES = {"IR": "Iran", "KP": "North Korea", "SY": "Syria", "CU": "Cuba", "RU": "Russia", "BY": "Belarus",
                 "PL": "Poland", "DE": "Germany", "ES": "Spain", "FR": "France", "IT": "Italy", "GB": "United Kingdom",
                 "US": "United States", "NL": "Netherlands", "CH": "Switzerland", "CN": "China", "TR": "Turkey",
                 "AE": "United Arab Emirates", "UA": "Ukraine", "IN": "India", "LB": "Lebanon", "VE": "Venezuela"}

_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# --- Dane ---
@lru_cache(maxsize=1)
def load_data():
    names = pd.read_csv(DATA / "names.csv")
    people = pd.read_csv(DATA / "people.csv")
    customers = pd.read_csv(DATA / "customers.csv")
    return names, people, customers


def _clean(v):
    """NaN -> None, żeby JSON był czytelny."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return v.item() if hasattr(v, "item") else v


def screen(query, threshold=SCREENING_THRESHOLD):
    """Fuzzy matching nazwiska z listą OFAC SDN (osoby + aliasy)."""
    names, people, _ = load_data()
    hits = process.extract(query, names["name"], scorer=fuzz.token_set_ratio,
                           processor=utils.default_process, score_cutoff=threshold, limit=None)
    rows = [{"sdn_id": names.loc[idx, "id"], "matched_name": m, "score": round(s)} for m, s, idx in hits]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("score", ascending=False).drop_duplicates("sdn_id")
    return df.merge(people[["id", "dob", "nationality", "program"]],
                    left_on="sdn_id", right_on="id", how="left").drop(columns="id")


def read_log():
    if not LOG.exists():
        return []
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]


def append_audit(entry):
    """Dopisuje wpis do audit logu z hashem poprzedniego wpisu (łańcuch hashy)."""
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


# ============================================================
# NARZĘDZIA AGENTA (zwykłe funkcje Pythona)
# ============================================================
def tool_search_sdn(name, limit=5):
    r = screen(name)
    if r.empty:
        return {"query": name, "hits": [], "note": f"Brak trafień z podobieństwem >= {SCREENING_THRESHOLD}"}
    return {"query": name, "hits": [{k: _clean(v) for k, v in h.items()} for h in r.head(int(limit)).to_dict("records")]}


def tool_get_sdn_entry(sdn_id):
    names, people, _ = load_data()
    row = people[people["id"] == int(sdn_id)]
    if row.empty:
        return {"error": f"Brak wpisu SDN o id {sdn_id}"}
    p = row.iloc[0]
    aliases = [n for n in names.loc[names["id"] == int(sdn_id), "name"] if n != p["name"]]
    return {"sdn_id": int(sdn_id), "name": p["name"], "aliases": aliases[:10],
            "dob": _clean(p["dob"]), "nationality": _clean(p["nationality"]), "passport": _clean(p["passport"]),
            "program": _clean(p["program"]), "title": _clean(p["title"]),
            "remarks": (str(p["remarks"])[:400] if _clean(p["remarks"]) else None)}


def tool_get_customer(customer_id):
    _, _, customers = load_data()
    row = customers[customers["customer_id"] == str(customer_id)]
    if row.empty:
        return {"error": f"Brak klienta {customer_id} w systemie banku"}
    c = row.iloc[0]
    # UWAGA: kolumny 'label' (prawdziwa odpowiedź) celowo NIE zwracamy - agent nie może jej znać.
    return {"customer_id": c["customer_id"], "name": c["name"], "dob": _clean(c["dob"]),
            "nationality": _clean(c["nationality"]), "passport": None}


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2} [A-Za-z]{3} \d{4}")


def _parse_dob(s):
    s = "" if s is None else str(s).strip()
    if not s or s.lower() in ("nan", "none", "brak danych", "brak"):
        return None
    years = sorted({int(y) for y in re.findall(r"\b(1[89]\d\d|20\d\d)\b", s)})
    m = DATE_RE.search(s)
    full = pd.to_datetime(m.group(0), errors="coerce") if m else None
    return {"raw": s, "years": years,
            "full_date": None if full is None or pd.isna(full) else full.strftime("%Y-%m-%d"),
            "approximate": ("circa" in s.lower()) or (" to " in s)}


def tool_compare_dates(client_dob, sdn_dob):
    c, s = _parse_dob(client_dob), _parse_dob(sdn_dob)
    out = {"client": c, "sdn": s}
    if not c or not s or not c["years"] or not s["years"]:
        out.update(comparable=False, note="Brak daty po jednej ze stron - nie da się porównać")
        return out
    diff = min(abs(a - b) for a in c["years"] for b in s["years"])
    same = (c["full_date"] == s["full_date"]) if (c["full_date"] and s["full_date"]) else None
    out.update(comparable=True, min_year_difference=diff, same_full_date=same,
               more_than_2_years_apart=diff > 2)
    return out


def tool_check_country_risk(country):
    c = (country or "").strip()
    high = c.lower() in HIGH_RISK
    return {"country": c or None, "high_risk": high,
            "note": "Kraj na liście krajów wysokiego ryzyka banku (zasada P4)" if high else
                    ("Brak kraju" if not c else "Kraj spoza listy wysokiego ryzyka")}


def tool_lookup_bic(bank):
    text = bank or ""
    m = re.search(r"\(([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\)", text) or \
        re.search(r"\b([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b", text)
    if not m:
        return {"bank": text, "bic": None, "note": "Nie znaleziono kodu BIC"}
    bic = m.group(1)
    code = bic[4:6]
    country = BIC_COUNTRIES.get(code, code)
    return {"bank": text, "bic": bic, "country_code": code, "bank_country": country,
            "bank_country_high_risk": country.lower() in HIGH_RISK}


def tool_get_past_decisions(sdn_id=None, name=None, limit=5):
    rows = read_log()
    out = []
    for r in reversed(rows):
        hit = False
        if sdn_id is not None and r.get("sdn_id") is not None and int(r["sdn_id"]) == int(sdn_id):
            hit = True
        if name and r.get("customer_name") and fuzz.token_set_ratio(
                name, r["customer_name"], processor=utils.default_process) >= 90:
            hit = True
        if hit:
            out.append({"timestamp": r.get("timestamp"), "alert_type": r.get("alert_type", "customer"),
                        "customer_name": r.get("customer_name"), "sdn_name": r.get("sdn_name"),
                        "agent_decision": r.get("agent_decision", r.get("decision")),
                        "final_decision": r.get("final_decision", r.get("decision")),
                        "overridden": r.get("overridden", False), "reviewer": r.get("reviewer"),
                        "comment": r.get("comment")})
        if len(out) >= int(limit):
            break
    return {"count": len(out), "decisions": out,
            "note": "Wcześniejsze decyzje to kontekst, nie dowód. Rozstrzygaj według polityki."}


EXECUTORS = {
    "search_sdn": tool_search_sdn,
    "get_sdn_entry": tool_get_sdn_entry,
    "get_customer": tool_get_customer,
    "compare_dates": tool_compare_dates,
    "check_country_risk": tool_check_country_risk,
    "lookup_bic": tool_lookup_bic,
    "get_past_decisions": tool_get_past_decisions,
}

# --- Opisy narzędzi dla modelu (JSON Schema) ---
TOOL_SPECS = {
    "search_sdn": {
        "description": "Wyszukuje nazwisko/nazwę na liście OFAC SDN (fuzzy matching, osoby i aliasy). "
                       "Zwraca trafienia z sdn_id, podobieństwem (0-100), datą ur., obywatelstwem i programem.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Imię i nazwisko lub nazwa firmy"},
            "limit": {"type": "integer", "description": "Maks. liczba trafień (domyślnie 5)"}},
            "required": ["name"]}},
    "get_sdn_entry": {
        "description": "Pełny wpis z listy SDN: nazwisko, aliasy, data urodzenia, obywatelstwo, paszport, program, uwagi.",
        "input_schema": {"type": "object", "properties": {"sdn_id": {"type": "integer"}}, "required": ["sdn_id"]}},
    "get_customer": {
        "description": "Dane klienta z systemu banku (KYC) po customer_id: nazwisko, data urodzenia, obywatelstwo, paszport.",
        "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]}},
    "compare_dates": {
        "description": "Deterministycznie porównuje datę urodzenia klienta i osoby z listy (różne formaty, 'circa', zakresy). "
                       "Zwraca różnicę lat i czy pełna data jest identyczna. Używaj zamiast liczenia w pamięci.",
        "input_schema": {"type": "object", "properties": {
            "client_dob": {"type": "string"}, "sdn_dob": {"type": "string"}}, "required": ["client_dob", "sdn_dob"]}},
    "check_country_risk": {
        "description": "Sprawdza, czy kraj jest na bankowej liście krajów wysokiego ryzyka (sankcje).",
        "input_schema": {"type": "object", "properties": {"country": {"type": "string"}}, "required": ["country"]}},
    "lookup_bic": {
        "description": "Wyciąga kod BIC/SWIFT z nazwy banku i zwraca kraj banku (znaki 5-6 kodu BIC) oraz czy to kraj wysokiego ryzyka.",
        "input_schema": {"type": "object", "properties": {"bank": {"type": "string"}}, "required": ["bank"]}},
    "get_past_decisions": {
        "description": "Wcześniejsze decyzje z audit logu dla tego samego wpisu SDN (sdn_id) lub podobnego nazwiska klienta, "
                       "w tym korekty analityków. Kontekst, nie dowód.",
        "input_schema": {"type": "object", "properties": {
            "sdn_id": {"type": "integer"}, "name": {"type": "string"}}, "required": []}},
}

SUBMIT_TOOL = {
    "name": "submit_decision",
    "description": "Kończy dochodzenie i zapisuje decyzję. Wywołaj DOKŁADNIE RAZ, gdy masz już dowody z narzędzi.",
    "input_schema": {"type": "object", "properties": {
        "decision": {"type": "string", "enum": ["TRUE_MATCH", "FALSE_POSITIVE", "ESCALATE"]},
        "rule": {"type": "string", "description": "Numer zastosowanej zasady polityki, np. R3 lub P2"},
        "rationale": {"type": "string", "description": "Uzasadnienie po polsku: co się zgadza, co nie, dlaczego ta zasada"},
        "confidence": {"type": "number", "description": "Pewność 0.0-1.0"},
        "evidence": {"type": "array", "items": {"type": "string"},
                     "description": "Konkretne fakty zwrócone przez narzędzia, na których opierasz decyzję"},
        "factors": {"type": "array", "items": {"type": "object", "properties": {
            "factor": {"type": "string"}, "client": {"type": "string"}, "sdn": {"type": "string"},
            "result": {"type": "string", "enum": ["MATCH", "MISMATCH", "PARTIAL", "UNKNOWN"]},
            "rule": {"type": "string"}}, "required": ["factor", "result"]}},
    }, "required": ["decision", "rule", "rationale", "confidence", "evidence", "factors"]},
}


def tools_for(names):
    return [{"name": n, **TOOL_SPECS[n]} for n in names] + [SUBMIT_TOOL]


CUSTOMER_TOOLS = ["get_sdn_entry", "get_customer", "compare_dates", "search_sdn", "get_past_decisions"]
PAYMENT_TOOLS = ["search_sdn", "get_sdn_entry", "check_country_risk", "lookup_bic", "get_past_decisions"]

WORKFLOW = """Pracujesz jak analityk-śledczy: najpierw zbierasz dowody narzędziami, potem decydujesz.
- Nie zgaduj faktów. Opieraj się tylko na danych z alertu i z wyników narzędzi.
- Nie powtarzaj tego samego wywołania narzędzia z tymi samymi argumentami.
- Gdy masz wystarczające dowody, wywołaj submit_decision (dokładnie raz). W "evidence" wypisz konkretne fakty z narzędzi.
- W "factors" oceń osobno każdy czynnik (brak danych po którejkolwiek stronie = UNKNOWN).
- Masz najwyżej {max_steps} rund. Jeśli dowodów nie da się zebrać, wybierz ESCALATE."""

SYSTEM_CUSTOMER = f"""Jesteś analitykiem compliance w banku. Rozstrzygasz alert sankcyjny dotyczący KLIENTA wyłącznie według polityki banku.

{WORKFLOW}

Typowa ścieżka dochodzenia:
1. get_sdn_entry dla wpisu z alertu (pełne dane, aliasy, paszport).
2. get_customer, jeśli alert ma customer_id (dane KYC bywają pełniejsze niż w alercie).
3. compare_dates dla daty urodzenia klienta i osoby z listy. Nigdy nie licz różnicy lat w pamięci.
4. get_past_decisions dla sdn_id, żeby sprawdzić, czy analityk już rozstrzygał ten wpis.
5. search_sdn tylko wtedy, gdy klient może lepiej pasować do innego wpisu z listy.
Pola "factors": name, dob, nationality, passport.

POLITYKA BANKU:
{SANCTIONS_POLICY}"""

SYSTEM_PAYMENT = f"""Jesteś analitykiem compliance w banku. Rozstrzygasz alert ze screeningu PŁATNOŚCI wyłącznie według polityki banku.

{WORKFLOW}

Typowa ścieżka dochodzenia:
1. check_country_risk dla kraju beneficjenta.
2. lookup_bic dla banku beneficjenta (kraj banku z kodu BIC).
3. get_sdn_entry dla najlepszych trafień nazwisk (jeśli są).
4. search_sdn, jeśli któraś strona przelewu nie była jeszcze sprawdzona.
5. get_past_decisions dla trafień, żeby sprawdzić wcześniejsze rozstrzygnięcia.
Pola "factors": beneficiary_name, ordering_party_name, country, bank, payment_purpose.
W polu "client" podaj dane z przelewu, w polu "sdn" dane z listy (lub informację o kraju objętym sankcjami).

POLITYKA BANKU:
{PAYMENT_POLICY}"""


# ============================================================
# PĘTLA AGENTA
# ============================================================
def _cost(model, u):
    pin, pout = PRICES.get(model, PRICES["claude-haiku-5-5"])
    return (u["input_tokens"] * pin + u["cache_write_tokens"] * pin * 1.25 +
            u["cache_read_tokens"] * pin * 0.1 + u["output_tokens"] * pout) / 1e6


def _short(obj, n=400):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


def run_agent(system, case_text, tool_names, model=MODEL, max_steps=MAX_STEPS):
    """Pętla tool use: model wybiera narzędzia, my je wykonujemy i odsyłamy wyniki,
    aż model wywoła submit_decision albo skończą się rundy."""
    tools = tools_for(tool_names)
    system = system.replace("{max_steps}", str(max_steps))
    messages = [{"role": "user", "content": case_text}]
    trace, final = [], None
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "api_calls": 0}
    t0 = time.time()
    stop = None

    for step in range(1, max_steps + 1):
        r = client().messages.create(
            model=model, max_tokens=8000, tools=tools, messages=messages,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}])
        usage["api_calls"] += 1
        usage["input_tokens"] += r.usage.input_tokens
        usage["output_tokens"] += r.usage.output_tokens
        usage["cache_read_tokens"] += getattr(r.usage, "cache_read_input_tokens", 0) or 0
        usage["cache_write_tokens"] += getattr(r.usage, "cache_creation_input_tokens", 0) or 0
        stop = r.stop_reason
        messages.append({"role": "assistant", "content": r.content})

        thought = " ".join(b.text for b in r.content if b.type == "text").strip()
        calls = [b for b in r.content if b.type == "tool_use"]
        if not calls:
            if stop == "end_turn" and step < max_steps:
                trace.append({"step": step, "tool": "(brak)", "input": {}, "output": "Agent nie wywołał narzędzia, przypominamy o submit_decision",
                              "thought": thought})
                messages.append({"role": "user", "content": "Zakończ dochodzenie, wywołując narzędzie submit_decision."})
                continue
            break

        results = []
        for b in calls:
            if b.name == "submit_decision":
                final = dict(b.input)
                trace.append({"step": step, "tool": "submit_decision", "input": {"decision": final.get("decision"),
                              "rule": final.get("rule"), "confidence": final.get("confidence")},
                              "output": "decyzja zapisana", "thought": thought})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": "OK"})
                continue
            fn = EXECUTORS.get(b.name)
            try:
                if fn is None or b.name not in tool_names:
                    raise ValueError(f"Nieznane narzędzie {b.name}")
                out, err = fn(**b.input), False
            except Exception as e:
                out, err = {"error": str(e)}, True
            trace.append({"step": step, "tool": b.name, "input": dict(b.input), "output": _short(out), "thought": thought})
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps(out, ensure_ascii=False, default=str), "is_error": err})
        if final is not None:
            break
        messages.append({"role": "user", "content": results})

    usage["seconds"] = round(time.time() - t0, 1)
    usage["steps"] = len(trace)
    usage["cost_usd"] = round(_cost(model, usage), 6)
    usage["model"] = model
    return final, trace, usage, stop


def finalize(final, trace, usage, stop, high_risk=False):
    """Walidacja + bezpieczniki: zła odpowiedź / niska pewność / kraj wysokiego ryzyka -> ESCALATE."""
    if not isinstance(final, dict) or final.get("decision") not in ("TRUE_MATCH", "FALSE_POSITIVE", "ESCALATE"):
        out = {"decision": "ESCALATE", "rule": "-", "confidence": 0.0, "factors": [], "evidence": [],
               "auto_escalated": True,
               "rationale": f"Agent nie zakończył dochodzenia poprawną decyzją (stop_reason={stop}, kroki={len(trace)}). "
                            "Sprawa trafia do analityka."}
    else:
        out = dict(final)
        for k in ("factors", "evidence"):
            if not isinstance(out.get(k), list):
                out[k] = []
        try:
            conf = float(out.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        out["confidence"] = conf
        if out["decision"] != "ESCALATE" and conf < CONFIDENCE_THRESHOLD:
            out["rationale"] = (f"[Auto-eskalacja: pewność {conf} < {CONFIDENCE_THRESHOLD}, "
                                f"agent proponował {out['decision']}] " + str(out.get("rationale", "")))
            out["decision"] = "ESCALATE"
            out["auto_escalated"] = True
        if high_risk and out["decision"] == "FALSE_POSITIVE":
            out["rationale"] = "[Auto-eskalacja: kraj wysokiego ryzyka (P4)] " + str(out.get("rationale", ""))
            out["decision"] = "ESCALATE"
            out["auto_escalated"] = True
    out["trace"] = trace
    out["usage"] = usage
    return out


def investigate_customer(name, dob, country, hit, customer_id=None, model=MODEL):
    """Alert klienta: dane klienta + trafienie ze screeningu. Resztę agent zbiera sam."""
    case = (f"ALERT SANKCYJNY (klient)\n"
            f"Klient: {name}; data ur.: {dob or 'brak danych'}; obywatelstwo: {country or 'brak danych'}; paszport: brak danych"
            + (f"; customer_id: {customer_id}" if customer_id else "") + "\n"
            f"Trafienie screeningu: sdn_id={int(hit['sdn_id'])}, nazwa na liście: {hit['matched_name']}, "
            f"podobieństwo nazwiska: {hit['score']}\n"
            "Przeprowadź dochodzenie i rozstrzygnij alert.")
    return finalize(*run_agent(SYSTEM_CUSTOMER, case, CUSTOMER_TOOLS, model=model))


def investigate_payment(p, hits, country_flag, model=MODEL):
    lines = ["ALERT SANKCYJNY (płatność)",
             f"Przelew: zleceniodawca: {p['ordering'] or 'brak danych'}; beneficjent: {p['beneficiary'] or 'brak danych'}; "
             f"kraj beneficjenta: {p['country'] or 'brak danych'}; bank beneficjenta: {p['bank'] or 'brak danych'}; "
             f"kwota: {p['amount']} {p['currency']}; tytuł: {p['title'] or 'brak danych'}"]
    if hits:
        for h in hits:
            lines.append(f"Trafienie screeningu ({h['party']}): sdn_id={int(h['sdn_id'])}, {h['matched_name']}, "
                         f"podobieństwo {h['score']}")
    else:
        lines.append("Screening nazwisk: brak trafień na liście SDN.")
    lines.append("Przeprowadź dochodzenie i rozstrzygnij alert.")
    return finalize(*run_agent(SYSTEM_PAYMENT, "\n".join(lines), PAYMENT_TOOLS, model=model), high_risk=country_flag)


# ============================================================
# UI: wspólne elementy dla stron Streamlit
# ============================================================
ICONS = {"TRUE_MATCH": "🚨", "FALSE_POSITIVE": "✅", "ESCALATE": "🟡"}
FACTOR_ICONS = {"MATCH": "✅ MATCH", "MISMATCH": "❌ MISMATCH", "PARTIAL": "🟠 PARTIAL", "UNKNOWN": "❔ UNKNOWN"}


def audit_fields(out):
    """Pola agenta wspólne dla każdego wpisu w audit logu (w tym pełny ślad kroków)."""
    return {
        "agent_decision": out["decision"],
        "agent_rule": out.get("rule"),
        "agent_confidence": float(out.get("confidence") or 0),
        "agent_rationale": out.get("rationale"),
        "agent_evidence": out.get("evidence", []),
        "auto_escalated": bool(out.get("auto_escalated")),
        "factors": out.get("factors", []),
        "agent_trace": out.get("trace", []),
        "agent_usage": out.get("usage", {}),
        "model": out.get("usage", {}).get("model", MODEL),
        "confidence_threshold": CONFIDENCE_THRESHOLD,
    }


def render_trace(out, key=""):
    import streamlit as st
    u = out.get("usage") or {}
    trace = out.get("trace") or []
    st.caption(f"🤖 {u.get('model', MODEL)} · kroki: {u.get('steps', len(trace))} · wywołania API: {u.get('api_calls', '?')} · "
               f"{u.get('input_tokens', 0) + u.get('cache_read_tokens', 0) + u.get('cache_write_tokens', 0):,} tok. wej. / "
               f"{u.get('output_tokens', 0):,} tok. wyj. · ${u.get('cost_usd', 0):.4f} · {u.get('seconds', '?')} s")
    if out.get("evidence"):
        st.markdown("**Dowody wskazane przez agenta**")
        st.markdown("\n".join(f"- {e}" for e in out["evidence"]))
    with st.expander(f"🔎 Ślad dochodzenia agenta (kroki: {len(trace)})"):
        if not trace:
            st.write("Brak kroków.")
        for t in trace:
            st.markdown(f"**Krok {t['step']} · `{t['tool']}`** "
                        + ", ".join(f"{k}={v}" for k, v in (t.get("input") or {}).items()))
            if t.get("thought"):
                st.caption(f"💭 {t['thought']}")
            st.code(str(t.get("output", "")), language="json", wrap_lines=True)


def render_factors(out, client_label="Klient"):
    import streamlit as st
    factors = [f for f in out.get("factors", []) if isinstance(f, dict)]
    if factors:
        st.markdown("**Ocena czynników**")
        st.dataframe(pd.DataFrame([{
            "Czynnik": f.get("factor", ""),
            client_label: str(f.get("client", "")),
            "Lista SDN": str(f.get("sdn", "")),
            "Wynik": FACTOR_ICONS.get(str(f.get("result", "")).upper(), "❔ " + str(f.get("result", ""))),
            "Reguła": f.get("rule", ""),
        } for f in factors]), hide_index=True, width="stretch")
