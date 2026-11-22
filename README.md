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
3. **Screening**: fuzzy name matching (RapidFuzz `token_sort_ratio` ≥ 85) produces 249 alerts, ~91% of them false positives, which is the problem real banks face. `03_screening.ipynb`
4. **Agent**: Claude reads each alert plus the bank policy (`policy/sanctions_policy.md`) and returns structured JSON: decision, rule applied, evidence per attribute, confidence. `04_agent.ipynb`
5. **Audit log**: every decision stored in a SHA-256 hash chain; any later edit is detectable. `05_audit.ipynb`
6. **Dashboard**: Streamlit view of screening funnel, agent metrics, confusion matrix and per-alert rationale. `app/dashboard.py`

## Design choices

- **Policy as text, not code.** Compliance can change rules without a deploy; the agent cites the rule it applied.
- **Escalate when unsure.** A missed sanctioned person is far costlier than a human review, so the policy forbids TRUE_MATCH/FALSE_POSITIVE on name alone.
- **Explainability.** Every decision carries a rationale and evidence, as regulators require.

## Run it
pip install pandas rapidfuzz faker anthropic python-dotenv streamlit
1. Download SDN.CSV, ALT.CSV, ADD.CSV from OFAC into `data/raw/`
2. Add `ANTHROPIC_API_KEY=...` to a `.env` file in the repo root
3. Run notebooks 01 → 05 in order
4. `streamlit run app/dashboard.py`

## Limitations

Synthetic customers; small LLM sample (cost-bounded); single-attribute fuzzy matching. Next steps: larger evaluation, model comparison (Haiku vs. Sonnet), transliteration-aware matching.