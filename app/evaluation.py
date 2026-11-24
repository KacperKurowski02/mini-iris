"""Mini-Iris: ewaluacja agenta na próbce alertów z prawdziwą odpowiedzią (ground truth).

Uruchamianie: strona "5 · Ewaluacja" w aplikacji albo w terminalu:
    python app/evaluation.py --n 24 --model claude-haiku-5-5 --mode tools

Wyniki zapisują się w folderze eval/ (jest w gicie, żeby było je widać na GitHubie):
    eval/runs.csv            - jedno podsumowanie na przebieg (do porównań wersji)
    eval/results/<run>.csv   - wynik dla każdego alertu, z pełnym śladem agenta
"""
from pathlib import Path
from datetime import datetime
import json, argparse, sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import iris_agent as A

EVAL_DIR = A.ROOT / "eval"
RESULTS_DIR = EVAL_DIR / "results"
RUNS = EVAL_DIR / "runs.csv"
SEED = 42
# Próbka warstwowa: zawsze te same proporcje i to samo ziarno losowania,
# więc różne wersje agenta są porównywane na tych samych alertach.
STRATA = {"true_match": 8, "lookalike": 12, "clean": 4}


def policy_expected(label, client_dob, sdn_dob):
    """Co powinna powiedzieć polityka (R1-R8), wyliczone deterministycznie, bez LLM."""
    if label == "true_match":
        return "TRUE_MATCH"
    cmp = A.tool_compare_dates(client_dob, sdn_dob)
    if not cmp.get("comparable"):
        return "ESCALATE"          # R6: brak daty po stronie listy
    if cmp["min_year_difference"] > 2:
        return "FALSE_POSITIVE"    # R3
    return "ESCALATE"              # R5: data pasuje, obywatelstwo nie


def build_sample(n=24):
    """Warstwowa próbka alertów z alerts.csv z etykietami prawdy."""
    alerts = pd.read_csv(A.DATA / "alerts.csv")
    _, people, customers = A.load_data()
    df = (alerts.merge(customers[["customer_id", "dob", "nationality"]], on="customer_id", how="left")
                .merge(people[["id", "dob"]].rename(columns={"id": "sdn_id", "dob": "sdn_dob"}), on="sdn_id", how="left"))
    total = sum(STRATA.values())
    parts = []
    for label, k in STRATA.items():
        pool = df[df["label"] == label]
        k = max(1, round(k * n / total))
        parts.append(pool.sample(min(k, len(pool)), random_state=SEED))
    s = pd.concat(parts).sample(frac=1, random_state=SEED).reset_index(drop=True)
    s["expected"] = s["label"].map(lambda l: "TRUE_MATCH" if l == "true_match" else "FALSE_POSITIVE")
    s["policy_expected"] = [policy_expected(l, d, sd) for l, d, sd in zip(s["label"], s["dob"], s["sdn_dob"])]
    return s


def compute_metrics(r):
    """r: DataFrame z kolumnami expected, policy_expected, decision, cost_usd, seconds, steps."""
    tm = r["expected"] == "TRUE_MATCH"
    fp = ~tm
    d = r["decision"]
    n = len(r)
    return {
        "n": n,
        "missed_true_matches": int((tm & (d == "FALSE_POSITIVE")).sum()),       # musi być 0
        "true_matches_blocked": f"{int((tm & (d == 'TRUE_MATCH')).sum())}/{int(tm.sum())}",
        "false_positives_auto_closed": f"{int((fp & (d == 'FALSE_POSITIVE')).sum())}/{int(fp.sum())}",
        "fp_reduction": round(float((fp & (d == "FALSE_POSITIVE")).sum() / max(fp.sum(), 1)), 3),
        "wrong_blocks": int((fp & (d == "TRUE_MATCH")).sum()),
        "escalation_rate": round(float((d == "ESCALATE").mean()), 3),
        "policy_accuracy": round(float((d == r["policy_expected"]).mean()), 3),
        "avg_steps": round(float(r["steps"].mean()), 1),
        "avg_seconds": round(float(r["seconds"].mean()), 1),
        "avg_cost_usd": round(float(r["cost_usd"].mean()), 5),
        "total_cost_usd": round(float(r["cost_usd"].sum()), 4),
        "errors": int(r["error"].astype(bool).sum()) if "error" in r else 0,
    }


def run_eval(n=24, model=A.MODEL, mode="tools", note="", progress=None):
    sample = build_sample(n)
    A.PAST_DECISIONS_ENABLED = False
    A.FEEDBACK_ENABLED = False
    rows = []
    try:
        for i, a in enumerate(sample.to_dict("records")):
            hit = {"sdn_id": a["sdn_id"], "matched_name": a["matched_name"], "score": a["score"]}
            err = ""
            try:
                out = A.investigate_customer(a["customer_name"], a["dob"], a["nationality"], hit,
                                             customer_id=a["customer_id"], model=model, mode=mode)
            except Exception as e:  # np. brak klucza API, limit zapytań
                err = f"{type(e).__name__}: {e}"
                out = {"decision": "ESCALATE", "rule": "BŁĄD", "confidence": 0.0, "rationale": err,
                       "trace": [], "usage": {}, "evidence": [], "factors": []}
            u = out.get("usage", {})
            rows.append({
                "customer_id": a["customer_id"], "customer_name": a["customer_name"], "sdn_id": int(a["sdn_id"]),
                "sdn_name": a["matched_name"], "score": a["score"], "label": a["label"],
                "expected": a["expected"], "policy_expected": a["policy_expected"],
                "decision": out["decision"], "rule": out.get("rule"), "confidence": out.get("confidence"),
                "auto_escalated": bool(out.get("auto_escalated")),
                "review": (out.get("review") or {}).get("verdict", ""), "revisions": out.get("revisions", 0),
                "rationale": out.get("rationale"), "steps": u.get("steps", 0), "api_calls": u.get("api_calls", 0),
                "tools_used": " → ".join(t["tool"] for t in out.get("trace", [])),
                "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
                "cost_usd": u.get("cost_usd", 0.0), "seconds": u.get("seconds", 0.0), "error": err,
                "trace": json.dumps(out.get("trace", []), ensure_ascii=False, default=str),
                "evidence": json.dumps(out.get("evidence", []), ensure_ascii=False),
            })
            if progress:
                progress(i + 1, len(sample), a["customer_name"], out["decision"])
    finally:
        A.PAST_DECISIONS_ENABLED = True
        A.FEEDBACK_ENABLED = True

    r = pd.DataFrame(rows)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"_{mode}_{model.replace('claude-', '')}"
    summary = {"run_id": run_id, "timestamp": datetime.now().isoformat(timespec="seconds"),
               "model": model, "mode": mode, "prompt_version": A.prompt_version(mode),
               "policy_version": A.policy_version(A.SANCTIONS_POLICY), "seed": SEED, "note": note,
               **compute_metrics(r)}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    r.to_csv(RESULTS_DIR / f"{run_id}.csv", index=False)
    runs = pd.read_csv(RUNS) if RUNS.exists() else pd.DataFrame()
    pd.concat([runs, pd.DataFrame([summary])], ignore_index=True).to_csv(RUNS, index=False)
    return summary, r


def load_runs():
    return pd.read_csv(RUNS) if RUNS.exists() else pd.DataFrame()


def load_results(run_id):
    return pd.read_csv(RESULTS_DIR / f"{run_id}.csv")


README_COLS = [("model", "Model"), ("mode", "Mode"), ("n", "Alerts"), ("missed_true_matches", "Missed true matches"),
               ("true_matches_blocked", "True matches blocked"), ("false_positives_auto_closed", "FPs auto-closed"),
               ("wrong_blocks", "Wrong blocks"), ("escalation_rate", "Escalation rate"),
               ("policy_accuracy", "Policy accuracy"), ("avg_steps", "Avg steps"),
               ("avg_seconds", "Avg s / alert"), ("avg_cost_usd", "Avg $ / alert")]


def markdown_table(runs):
    head = "| " + " | ".join(h for _, h in README_COLS) + " |\n|" + "---|" * len(README_COLS) + "\n"
    body = ""
    for r in runs.to_dict("records"):
        vals = []
        for c, _ in README_COLS:
            v = r.get(c)
            if c in ("escalation_rate", "policy_accuracy"):
                v = f"{float(v):.0%}"
            elif c == "avg_cost_usd":
                v = f"${float(v):.4f}"
            elif c == "mode":
                v = {"tools": "tool-use agent", "multi": "multi-agent (investigator → adjudicator → reviewer)",
                     "single": "single call"}.get(v, v)
            vals.append(str(v))
        body += "| " + " | ".join(vals) + " |\n"
    return head + body


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--model", default=A.MODEL)
    ap.add_argument("--mode", choices=list(A.MODES), default="tools")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    if args.n > 30:
        sys.exit("Limit kosztów: maksymalnie 30 alertów.")
    s, _ = run_eval(args.n, args.model, args.mode, args.note,
                    progress=lambda i, t, name, d: print(f"{i}/{t} {name}: {d}"))
    print(json.dumps(s, indent=2, ensure_ascii=False))
