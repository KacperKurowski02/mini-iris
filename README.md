# Mini-Iris: LLM adjudication of sanctions screening alerts

End-to-end prototype of an AI agent that resolves sanctions-screening alerts against a written bank policy, inspired by Silent Eight's Iris.

## Results (sample of 21 alerts, Claude Haiku)

| Metric | Value |
|---|---|
| Accuracy vs. ground truth | **95%** |
| False positives auto-closed | **15 / 16 (94%)** |
| True matches missed | **0** |
| Escalated to human | 1 |
| Avg. time per decision | **3.2 s** |

## Pipeline

1. **Sanctions list**: OFAC SDN list (individuals + aliases, ~16k names) parsed and cleaned. `01_explore_sdn.ipynb`
2. **Customers**: 5,220 synthetic customers (Faker), incl. 20 real SDN matches and 200 lookalikes, each with a ground-truth label. `02_customers.ipynb`
3. **Screening**: fuzzy name matching (RapidFuzz `token_sort_ratio` ≥ 85 in the notebook, `token_set_ratio` in the app) produces 249 alerts, ~91% of them false positives, which is the problem real banks face. `03_screening.ipynb`
4. **Agent**: Claude reads each alert plus the bank policy (`policy/sanctions_policy.md`) and returns structured JSON: decision, rule applied, evidence per attribute, confidence. `04_agent.ipynb` (first, single-call version; the app now uses the tool-using agent below)
5. **Audit log**: every decision stored in a SHA-256 hash chain; any later edit is detectable. `05_audit.ipynb`
6. **Dashboard**: Streamlit view of screening funnel, agent metrics, confusion matrix and per-alert rationale. `app/dashboard.py`

## Agentic investigation (tool use)

In the app the agent is not a single prompt. `app/iris_agent.py` runs an Anthropic **tool-use loop**: the agent gets only the alert and a set of tools, decides which evidence it needs, calls tools over several rounds and finishes by calling `submit_decision`.

| Tool | What it returns |
|---|---|
| `get_sdn_entry` | Full SDN record: aliases, DOB, nationality, passport, program, remarks |
| `get_customer` | Bank KYC data for a customer id (ground-truth label deliberately withheld) |
| `compare_dates` | Deterministic DOB comparison (handles `circa`, ranges, mixed formats), so the model never does date arithmetic in its head |
| `search_sdn` | Fuzzy search of any name against the SDN list |
| `check_country_risk` | High-risk / sanctioned jurisdiction check |
| `lookup_bic` | Bank country from the BIC/SWIFT code |
| `get_past_decisions` | Earlier decisions and analyst overrides for the same SDN entry or a similar name |
| `submit_decision` | Terminal tool: decision, rule, rationale, cited evidence, per-factor scores, confidence |

Guardrails around the loop: max 8 rounds, unknown tools / tool errors are returned to the model as errors, a missing or malformed decision becomes ESCALATE, and the confidence threshold and high-risk-country rule are enforced in code after the model decides. Every step (tool, arguments, result) plus token usage and cost is shown in the UI as an investigation trace and stored in the hash-chained audit log.

## Interactive app (Streamlit)

`streamlit run app/dashboard.py` opens a multi-page app:

| Page | What it does |
|---|---|
| **Dashboard** | Screening funnel, agent accuracy, confusion matrix on the evaluation sample. |
| **1 · Sprawdź osobę** | Screen one person (or a CSV batch) against OFAC SDN. The agent adjudicates each hit; old system vs. Mini-Iris shown side by side. Batch mode runs the agent on up to 20 rows and writes every decision to the audit log. |
| **2 · Płatności** | Separate **payment screening agent** with its own policy (`policy/payment_policy.md`, rules P1–P6): screens beneficiary and ordering party, flags high-risk countries, three one-click demo payments. |
| **3 · Kolejka analityka** | Analyst queue of open ESCALATE cases, ops metrics (decisions by type, human override rate, auto-escalations) and a live **audit-chain verification** button. |

Key controls:

- **Human in the loop.** Every agent decision goes through an analyst review form (approve or override) before it is logged; overrides are tracked.
- **Confidence threshold.** Any TRUE_MATCH / FALSE_POSITIVE below 0.8 confidence is auto-escalated to a human, and the reason is written into the rationale.
- **Policy versioning.** Each log entry stores a hash of the exact policy text, the model and the threshold used, so every decision can be reproduced.
- **Investigation trace.** Each decision stores the full list of tool calls, results, tokens and cost (`agent_trace`, `agent_usage`).
- **Per-factor explanations.** The agent scores each attribute (name, DOB, nationality, passport; for payments: parties, country, bank, purpose) as MATCH / MISMATCH / PARTIAL / UNKNOWN with the rule applied.
- **Tamper-evident audit log.** Customer, batch and payment decisions share one SHA-256 hash chain (`data/processed/audit_log.jsonl`).

## Design choices

- **Policy as text, not code.** Compliance can change rules without a deploy; the agent cites the rule it applied.
- **Escalate when unsure.** A missed sanctioned person is far costlier than a human review, so the policy forbids TRUE_MATCH/FALSE_POSITIVE on name alone.
- **Explainability.** Every decision carries a rationale and evidence, as regulators require.

## Run it
pip install pandas rapidfuzz faker anthropic python-dotenv streamlit
1. Download SDN.CSV, ALT.CSV, ADD.CSV from OFAC into `data/raw/`
2. Add `ANTHROPIC_API_KEY=...` to a `.env` file in the repo root
3. Run notebooks 01 → 05 in order
4. `streamlit run app/dashboard.py` and open the pages in the sidebar

## Limitations

Synthetic customers; small LLM sample (cost-bounded); single-attribute fuzzy matching. Next steps: larger evaluation, model comparison (Haiku vs. Sonnet), transliteration-aware matching.