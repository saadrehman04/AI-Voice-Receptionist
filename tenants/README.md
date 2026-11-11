# tenants/

One YAML file per business (`<slug>.yaml`): name, languages, timezone, hours, services, policies,
greeting, handoff contact, FAQ. Validated by a Pydantic schema in `packages/shared`.

Added in Phase 2: `demo-salon.yaml`, `demo-workshop.yaml`.
