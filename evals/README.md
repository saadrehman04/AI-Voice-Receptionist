# evals/

Simulated-caller eval suite (built in Phase 6).

- `scenarios/*.yaml`: tenant, caller persona, goal, facts the caller knows, expected outcome.
- `src/receptionist_evals/`: the runner, simulated caller and scorers.
- `make evals` runs the suite. For now it only lists scenarios.
