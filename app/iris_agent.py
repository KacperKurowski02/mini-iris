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


PAST_DECISIONS_ENABLED = True  # ewaluacja wyłącza, żeby wynik nie zależał od zawartości audit logu


def tool_get_past_decisions(sdn_id=None, name=None, limit=5):
    rows = read_log() if PAST_DECISIONS_ENABLED else []
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



# ============================================================
# PĘTLA FEEDBACKU: decyzje analityków jako przykłady dla agenta
# ============================================================
FEEDBACK_ENABLED = True   # ewaluacja wyłącza, żeby wynik był powtarzalny
FEEDBACK_K = 3            # ile najbardziej podobnych przykładów dostaje agent
FEEDBACK_MIN_SCORE = 0.45  # minimalne podobieństwo sprawy (0-1)


def analyst_examples(rows=None):
    """Sprawy rozstrzygnięte przez człowieka: rozwiązania z kolejki ESCALATE, korekty agenta,
    decyzje z komentarzem analityka. Tylko ostateczne decyzje (TRUE_MATCH / FALSE_POSITIVE)."""
    rows = read_log() if rows is None else rows
    out = []
    for r in rows:
        if str(r.get("reviewer") or "auto") == "auto":
            continue
        if r.get("final_decision") not in ("TRUE_MATCH", "FALSE_POSITIVE"):
            continue
        if (r.get("alert_type") == "analyst_resolution" or r.get("overridden")
                or str(r.get("comment") or "").strip()):
            out.append(r)
    return out


def _same_id(a, b):
    try:
        return a is not None and b is not None and int(a) == int(b)
    except (TypeError, ValueError):
        return False


def example_similarity(case, ex):
    """Proste, wytłumaczalne podobieństwo dwóch spraw (0-1) + lista powodów."""
    score, why = 0.0, []
    if _same_id(case.get("sdn_id"), ex.get("sdn_id")):
        score += 0.5
        why.append("ten sam wpis SDN")
    if case.get("name") and ex.get("customer_name"):
        s = fuzz.token_set_ratio(str(case["name"]), str(ex["customer_name"]), processor=utils.default_process) / 100
        if s >= 0.85:
            score += 0.3 * s
            why.append(f"podobne nazwisko ({s:.0%})")
    c1, c2 = str(case.get("country") or "").strip().lower(), str(ex.get("customer_country") or "").strip().lower()
    if c1 and c1 == c2:
        score += 0.1
        why.append("ten sam kraj")
    ex_type = ex.get("resolved_alert_type") or ex.get("alert_type") or "customer"
    if case.get("alert_type", "customer") == ex_type.replace("customer_batch", "customer"):
        score += 0.1
        why.append("ten sam typ alertu")
    return round(score, 2), why


def find_similar_examples(case, k=FEEDBACK_K, min_score=FEEDBACK_MIN_SCORE, rows=None):
    """Retrieval: k najbardziej podobnych spraw rozstrzygniętych przez analityków."""
    if not FEEDBACK_ENABLED:
        return []
    scored = []
    for ex in analyst_examples(rows):
        score, why = example_similarity(case, ex)
        if score >= min_score:
            scored.append((score, ex.get("timestamp") or "", ex, why))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [{"example_id": str(ex.get("hash", ""))[:10], "similarity": score, "why": why,
             "timestamp": ex.get("timestamp"), "customer_name": ex.get("customer_name"),
             "customer_dob": ex.get("customer_dob"), "customer_country": ex.get("customer_country"),
             "sdn_id": ex.get("sdn_id"), "sdn_name": ex.get("sdn_name"),
             "agent_decision": ex.get("agent_decision"), "final_decision": ex.get("final_decision"),
             "reviewer": ex.get("reviewer"), "comment": ex.get("comment") or ""}
            for score, _, ex, why in scored[:k]]


def format_examples(examples):
    if not examples:
        return ""
    lines = ["PRZYKŁADY Z DECYZJI ANALITYKÓW W PODOBNYCH SPRAWACH (wskazówka, jak bank stosuje politykę; "
             "to NIE jest dowód w tej sprawie: fakty sprawdź narzędziami i rozstrzygaj według polityki):"]
    for i, e in enumerate(examples, 1):
        lines.append(f"{i}. [podobieństwo {e['similarity']}: {', '.join(e['why'])}] Klient: {e['customer_name']} "
                     f"(ur. {e.get('customer_dob') or 'brak danych'}, kraj: {e.get('customer_country') or 'brak danych'}) "
                     f"vs lista: {e.get('sdn_name') or 'brak'}. Agent: {e.get('agent_decision')} → analityk: "
                     f"{e['final_decision']}. Komentarz analityka: \"{e['comment'] or 'brak'}\"")
    return "\n".join(lines) + "\n"


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
        "confidence": {"type": "number", "description": "Pewność 0.0-1.0 (osobne pole, nie wpisuj jej do uzasadnienia)"},
        "rationale": {"type": "string", "description": "Uzasadnienie po polsku: co się zgadza, co nie, dlaczego ta zasada"},
        "evidence": {"type": "array", "items": {"type": "string"},
                     "description": "Konkretne fakty zwrócone przez narzędzia, na których opierasz decyzję"},
        "factors": {"type": "array", "items": {"type": "object", "properties": {
            "factor": {"type": "string"}, "client": {"type": "string"}, "sdn": {"type": "string"},
            "result": {"type": "string", "enum": ["MATCH", "MISMATCH", "PARTIAL", "UNKNOWN"]},
            "rule": {"type": "string"}}, "required": ["factor", "result"]}},
    }, "required": ["decision", "rule", "rationale", "confidence", "evidence", "factors"]},
}


FINDINGS_TOOL = {
    "name": "submit_findings",
    "description": "Kończy zbieranie dowodów i przekazuje dossier decydentowi. NIE podejmuj decyzji. Wywołaj dokładnie raz.",
    "input_schema": {"type": "object", "properties": {
        "facts": {"type": "array", "description": "Ustalone fakty, każdy z narzędzia, które go zwróciło",
                  "items": {"type": "object", "properties": {
                      "factor": {"type": "string", "description": "np. name, dob, nationality, passport, country, bank"},
                      "client": {"type": "string"}, "sdn": {"type": "string"},
                      "source_tool": {"type": "string"}, "note": {"type": "string"}},
                      "required": ["factor", "source_tool"]}},
        "summary": {"type": "string", "description": "Krótkie podsumowanie ustaleń, bez decyzji"},
        "open_questions": {"type": "array", "items": {"type": "string"},
                           "description": "Czego nie udało się ustalić"},
    }, "required": ["facts", "summary", "open_questions"]},
}

REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Wynik niezależnej kontroli (zasada czterech oczu). Wywołaj dokładnie raz.",
    "input_schema": {"type": "object", "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REVISE"]},
        "issues": {"type": "array", "items": {"type": "string"},
                   "description": "Konkretne błędy w decyzji (puste przy APPROVE)"},
        "missing_evidence": {"type": "array", "items": {"type": "string"},
                             "description": "Czego śledczy powinien jeszcze dowieść (puste, jeśli dowody są kompletne)"},
    }, "required": ["verdict", "issues", "missing_evidence"]},
}


def tools_for(names, final_tool=SUBMIT_TOOL):
    return [{"name": n, **TOOL_SPECS[n]} for n in names] + [final_tool]


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


def run_agent(system, case_text, tool_names, model=MODEL, max_steps=MAX_STEPS, final_tool=SUBMIT_TOOL, agent="Agent"):
    """Pętla tool use: model wybiera narzędzia, my je wykonujemy i odsyłamy wyniki,
    aż model wywoła narzędzie końcowe (final_tool, np. submit_decision) albo skończą się rundy."""
    tools = tools_for(tool_names, final_tool)
    final_name = final_tool["name"]
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
                trace.append({"step": step, "agent": agent, "tool": "(brak)", "input": {},
                              "output": f"Agent nie wywołał narzędzia, przypominamy o {final_name}", "thought": thought})
                messages.append({"role": "user", "content": f"Zakończ, wywołując narzędzie {final_name}."})
                continue
            break

        results = []
        for b in calls:
            if b.name == final_name:
                final = dict(b.input)
                shown = ({k: final.get(k) for k in ("decision", "rule", "confidence")} if final_name == "submit_decision"
                         else {"verdict": final.get("verdict")} if final_name == "submit_review" else {})
                trace.append({"step": step, "agent": agent, "tool": final_name, "input": shown,
                              "output": _short(final, 600), "thought": thought})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": "OK"})
                continue
            fn = EXECUTORS.get(b.name)
            try:
                if fn is None or b.name not in tool_names:
                    raise ValueError(f"Nieznane narzędzie {b.name}")
                out, err = fn(**b.input), False
            except Exception as e:
                out, err = {"error": str(e)}, True
            trace.append({"step": step, "agent": agent, "tool": b.name, "input": dict(b.input), "output": _short(out),
                          "thought": thought})
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


LEAK_RE = re.compile(r'<parameter name="(\w+)">(.*?)(?=</parameter>|<parameter name=|</invoke>|$)', re.S)


def repair_tool_input(final):
    """Naprawa znanego błędu modelu: czasem wpisuje dalsze pola (np. confidence) DO ŚRODKA tekstu uzasadnienia,
    w postaci '...</rationale> <parameter name="confidence">0.95'. Wtedy pole confidence jest puste, a decyzja
    byłaby niesłusznie eskalowana. Odzyskujemy te pola i czyścimy uzasadnienie."""
    if not isinstance(final, dict):
        return final
    out = dict(final)
    for key in ("rationale", "rule"):
        text = out.get(key)
        if not isinstance(text, str) or ("<parameter name=" not in text and "</" not in text):
            continue
        cut = min([i for i in (text.find("</"), text.find("<parameter name=")) if i >= 0])
        for name, raw in LEAK_RE.findall(text[cut:]):
            raw = raw.strip()
            if out.get(name) not in (None, "", []):
                continue
            try:
                out[name] = json.loads(raw)
            except ValueError:
                out[name] = raw
        out[key] = text[:cut].strip()
        out["repaired"] = True
    return out


def finalize(final, trace, usage, stop, high_risk=False):
    """Walidacja + bezpieczniki: zła odpowiedź / niska pewność / kraj wysokiego ryzyka -> ESCALATE."""
    final = repair_tool_input(final)
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


# ============================================================
# ZESPÓŁ AGENTÓW: Śledczy -> Decydent -> Recenzent
# ============================================================
# Każdy agent ma jedną rolę i własny prompt. Śledczy ma narzędzia, ale nie może decydować.
# Decydent widzi tylko dossier śledczego (nie może sam szukać faktów). Recenzent sprawdza decyzję
# i może odesłać sprawę: po więcej dowodów do Śledczego albo do poprawki do Decydenta.
MAX_REVISIONS = 2

INVESTIGATOR_RULES = """Jesteś ŚLEDCZYM w zespole compliance banku. Twoje jedyne zadanie: zebrać dowody narzędziami.
NIE podejmujesz decyzji i nie piszesz, czy to trafienie, czy nie. Decyzję podejmie inny agent na podstawie Twojego dossier.
- Opieraj się tylko na wynikach narzędzi. Każdy fakt podaj ze źródłem (source_tool).
- Nie powtarzaj tych samych wywołań. Masz najwyżej {max_steps} rund.
- Gdy masz dowody, wywołaj submit_findings (dokładnie raz). Braki danych wpisz do open_questions."""

INVESTIGATOR_CUSTOMER = f"""{INVESTIGATOR_RULES}

Zbierz: pełny wpis SDN (get_sdn_entry), dane KYC klienta (get_customer, jeśli jest customer_id),
porównanie dat urodzenia (compare_dates, zawsze narzędziem), wcześniejsze decyzje (get_past_decisions).
Fakty dla czynników: name, dob, nationality, passport.

Polityka banku (żebyś wiedział, które dowody są ważne):
{SANCTIONS_POLICY}"""

INVESTIGATOR_PAYMENT = f"""{INVESTIGATOR_RULES}

Zbierz: ryzyko kraju beneficjenta (check_country_risk), kraj banku z BIC (lookup_bic), wpisy SDN dla trafień
(get_sdn_entry), brakujące strony przelewu (search_sdn), wcześniejsze decyzje (get_past_decisions).
Fakty dla czynników: beneficiary_name, ordering_party_name, country, bank, payment_purpose.

Polityka banku (żebyś wiedział, które dowody są ważne):
{PAYMENT_POLICY}"""

ADJUDICATOR_RULES = """Jesteś DECYDENTEM w zespole compliance banku. Dostajesz alert i dossier dowodów od śledczego.
Nie masz narzędzi do szukania faktów: rozstrzygasz WYŁĄCZNIE na podstawie dossier i polityki.
- Nie zakładaj faktów spoza dossier. Brak danych = UNKNOWN.
- Jeśli są uwagi recenzenta, odnieś się do każdej z nich.
- Wywołaj submit_decision (dokładnie raz): decyzja, numer zasady, uzasadnienie, evidence (fakty z dossier), factors, pewność."""

ADJUDICATOR_CUSTOMER = f"""{ADJUDICATOR_RULES}
Pola "factors": name, dob, nationality, passport.

POLITYKA BANKU:
{SANCTIONS_POLICY}"""

ADJUDICATOR_PAYMENT = f"""{ADJUDICATOR_RULES}
Pola "factors": beneficiary_name, ordering_party_name, country, bank, payment_purpose.

POLITYKA BANKU:
{PAYMENT_POLICY}"""

REVIEWER_RULES = """Jesteś RECENZENTEM (zasada czterech oczu) w zespole compliance banku. Sprawdzasz decyzję innego agenta.
Sprawdź kolejno:
1. Czy zastosowana zasada naprawdę pasuje do faktów z dossier?
2. Czy każdy fakt w uzasadnieniu i evidence jest w dossier (nic nie zostało wymyślone)?
3. Czy TRUE_MATCH nie opiera się na samym nazwisku (R7/P5)?
4. Czy FALSE_POSITIVE lub TRUE_MATCH nie zapadły mimo brakujących kluczowych danych (wtedy powinno być ESCALATE)?
5. Czy pewność jest adekwatna do dowodów?
APPROVE, jeśli decyzja jest zgodna z polityką. REVISE z konkretnymi uwagami w issues, jeśli nie.
Jeśli brakuje dowodów, które śledczy mógł zdobyć narzędziami, wpisz je do missing_evidence.
Nie żądaj rzeczy niemożliwych (np. paszportu, którego nie ma w żadnym źródle). Wywołaj submit_review (dokładnie raz)."""

REVIEWER_CUSTOMER = f"""{REVIEWER_RULES}

POLITYKA BANKU:
{SANCTIONS_POLICY}"""

REVIEWER_PAYMENT = f"""{REVIEWER_RULES}

POLITYKA BANKU:
{PAYMENT_POLICY}"""


def _merge_usage(total, u):
    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "api_calls", "cost_usd"):
        total[k] = total.get(k, 0) + (u.get(k) or 0)
    return total


def run_team(case, investigator_system, tool_names, adjudicator_system, reviewer_system,
             model=MODEL, high_risk=False, max_revisions=MAX_REVISIONS):
    t0 = time.time()
    trace, usage, stop = [], {}, None

    def call(system, text, tools, final_tool, agent, max_steps):
        nonlocal stop
        final, tr, u, stop = run_agent(system, text, tools, model=model, max_steps=max_steps,
                                       final_tool=final_tool, agent=agent)
        trace.extend(tr)
        _merge_usage(usage, u)
        return final

    def as_json(x):
        return json.dumps(x, ensure_ascii=False, indent=1, default=str)

    dossier = call(investigator_system, case + "\nZbierz dowody do tego alertu.", tool_names, FINDINGS_TOOL,
                   "Investigator", MAX_STEPS) or {"facts": [], "summary": "Śledczy nie dostarczył dossier.",
                                                   "open_questions": ["brak dossier"]}
    feedback, decision, review, revisions = "", None, None, 0
    for rnd in range(max_revisions + 1):
        revisions = rnd
        decision = call(adjudicator_system, f"{case}\n\nDOSSIER ŚLEDCZEGO:\n{as_json(dossier)}{feedback}",
                        [], SUBMIT_TOOL, "Adjudicator", 2)
        review = call(reviewer_system, f"{case}\n\nDOSSIER ŚLEDCZEGO:\n{as_json(dossier)}\n\n"
                                       f"DECYZJA DO SPRAWDZENIA:\n{as_json(decision)}",
                      [], REVIEW_TOOL, "Reviewer", 2) or {"verdict": "REVISE", "issues": ["Recenzent nie odpowiedział"],
                                                         "missing_evidence": []}
        if review.get("verdict") == "APPROVE" or rnd == max_revisions:
            break
        if review.get("missing_evidence"):
            more = call(investigator_system,
                        f"{case}\n\nTWOJE DOTYCHCZASOWE DOSSIER:\n{as_json(dossier)}\n\n"
                        "RECENZENT PROSI O UZUPEŁNIENIE:\n- " + "\n- ".join(map(str, review["missing_evidence"]))
                        + "\nZbierz brakujące dowody i zwróć pełne, uzupełnione dossier.",
                        tool_names, FINDINGS_TOOL, "Investigator", MAX_STEPS)
            if more:
                dossier = more
        feedback = ("\n\nUWAGI RECENZENTA DO POPRZEDNIEJ DECYZJI (popraw je):\n- "
                    + "\n- ".join(map(str, review.get("issues") or ["brak szczegółów"])))

    for i, t in enumerate(trace, 1):
        t["step"] = i
    usage.update(seconds=round(time.time() - t0, 1), steps=len(trace), model=model,
                 cost_usd=round(usage.get("cost_usd", 0), 6))
    out = finalize(decision, trace, usage, stop, high_risk=high_risk)
    if review.get("verdict") != "APPROVE" and out["decision"] != "ESCALATE":
        out["rationale"] = (f"[Auto-eskalacja: recenzent nie zatwierdził decyzji {out['decision']} "
                            f"po {revisions} poprawkach] " + str(out.get("rationale", "")))
        out["decision"] = "ESCALATE"
        out["auto_escalated"] = True
    out.update(mode="multi", review=review, revisions=revisions, dossier=dossier)
    return out



SYSTEM_SINGLE = f"""Jesteś analitykiem compliance w banku. Rozstrzygasz alert sankcyjny dotyczący KLIENTA wyłącznie według polityki banku.
Wszystkie dane masz w treści alertu (nie masz innych narzędzi). Od razu wywołaj submit_decision.
W "factors" oceń osobno: name, dob, nationality, passport (brak danych po którejkolwiek stronie = UNKNOWN).

POLITYKA BANKU:
{SANCTIONS_POLICY}"""

# Tryby agenta (do porównań w ewaluacji)
MODES = {"tools": "Agent z narzędziami (wiele kroków)",
         "multi": "Zespół agentów: Śledczy → Decydent → Recenzent",
         "single": "Jedno zapytanie (bez narzędzi, baseline)"}


def prompt_version(mode="tools"):
    """Hash promptu systemowego + opisów narzędzi: zmienia się, gdy zmienisz prompt albo narzędzia."""
    if mode == "multi":
        text = (INVESTIGATOR_CUSTOMER + ADJUDICATOR_CUSTOMER + REVIEWER_CUSTOMER
                + json.dumps(tools_for(CUSTOMER_TOOLS, FINDINGS_TOOL), sort_keys=True))
    else:
        text = (SYSTEM_CUSTOMER + json.dumps(tools_for(CUSTOMER_TOOLS), sort_keys=True)) if mode == "tools" else SYSTEM_SINGLE
    text += json.dumps(SUBMIT_TOOL, sort_keys=True) + json.dumps(REVIEW_TOOL, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def investigate_customer(name, dob, country, hit, customer_id=None, model=MODEL, mode="tools"):
    """Alert klienta: dane klienta + trafienie ze screeningu. Resztę agent zbiera sam.
    mode="single": baseline bez narzędzi - wszystkie dane od razu w treści, jedno wywołanie modelu."""
    case = (f"ALERT SANKCYJNY (klient)\n"
            f"Klient: {name}; data ur.: {dob or 'brak danych'}; obywatelstwo: {country or 'brak danych'}; paszport: brak danych"
            + (f"; customer_id: {customer_id}" if customer_id else "") + "\n"
            f"Trafienie screeningu: sdn_id={int(hit['sdn_id'])}, nazwa na liście: {hit['matched_name']}, "
            f"podobieństwo nazwiska: {hit['score']}\n")
    examples = find_similar_examples({"alert_type": "customer", "name": name, "country": country,
                                      "sdn_id": hit["sdn_id"]})
    case += format_examples(examples)
    out = _investigate_customer(case, hit, model, mode)
    out["examples"] = examples
    return out


def _investigate_customer(case, hit, model, mode):
    if mode == "single":
        sdn = tool_get_sdn_entry(hit["sdn_id"])
        case += (f"Wpis SDN: data ur.: {sdn.get('dob') or 'brak danych'}; obywatelstwo: {sdn.get('nationality') or 'brak danych'}; "
                 f"paszport: {sdn.get('passport') or 'brak danych'}; program: {sdn.get('program')}\n"
                 "Rozstrzygnij alert.")
        out = finalize(*run_agent(SYSTEM_SINGLE, case, [], model=model, max_steps=2))
        out["mode"] = "single"
        return out
    if mode == "multi":
        return run_team(case, INVESTIGATOR_CUSTOMER, CUSTOMER_TOOLS, ADJUDICATOR_CUSTOMER, REVIEWER_CUSTOMER, model=model)
    case += "Przeprowadź dochodzenie i rozstrzygnij alert."
    return finalize(*run_agent(SYSTEM_CUSTOMER, case, CUSTOMER_TOOLS, model=model))


def investigate_payment(p, hits, country_flag, model=MODEL, mode="tools"):
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
    top = max(hits, key=lambda h: h["score"]) if hits else None
    examples = find_similar_examples({"alert_type": "payment", "name": p["beneficiary"], "country": p["country"],
                                      "sdn_id": top["sdn_id"] if top else None})
    if examples:
        lines.append(format_examples(examples).rstrip())
    out = _investigate_payment(lines, country_flag, model, mode)
    out["examples"] = examples
    return out


def _investigate_payment(lines, country_flag, model, mode):
    if mode == "multi":
        return run_team("\n".join(lines), INVESTIGATOR_PAYMENT, PAYMENT_TOOLS, ADJUDICATOR_PAYMENT, REVIEWER_PAYMENT,
                        model=model, high_risk=country_flag)
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
        "agent_mode": out.get("mode", "tools"),
        "agent_review": out.get("review"),
        "agent_revisions": out.get("revisions", 0),
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "agent_examples": [{"example_id": e["example_id"], "similarity": e["similarity"],
                            "final_decision": e["final_decision"]} for e in out.get("examples", [])],
    }


AGENT_ICONS = {"Investigator": "🕵️", "Adjudicator": "⚖️", "Reviewer": "🔍"}


def render_trace(out, key=""):
    import streamlit as st
    if out.get("review"):
        rv = out["review"]
        msg = (f"🔍 Recenzent: **{rv.get('verdict', '?')}** · poprawki: {out.get('revisions', 0)}"
               + ("".join(f"\n- {i}" for i in rv.get("issues", [])) if rv.get("issues") else ""))
        (st.success if rv.get("verdict") == "APPROVE" else st.warning)(msg)
    if out.get("examples"):
        with st.expander(f"📚 Agent dostał przykłady od analityków: {len(out['examples'])}"):
            for e in out["examples"]:
                st.markdown(f"- **{e['customer_name']}** ↔ {e.get('sdn_name') or '—'} · agent: {e.get('agent_decision')} "
                            f"→ analityk **{e['final_decision']}** · podobieństwo {e['similarity']} ({', '.join(e['why'])})"
                            + (f"\n  > {e['comment']}" if e.get("comment") else ""))
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
            who = f"{AGENT_ICONS.get(t.get('agent'), '')} {t['agent']} · " if t.get("agent") not in (None, "Agent") else ""
            st.markdown(f"**Krok {t['step']} · {who}`{t['tool']}`** "
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
