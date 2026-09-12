# Architecture Decision Records

One file per decision: `NNN-short-slug.md`, numbered in order of acceptance. A decision is anything that would be expensive to reverse — a new table or service, a change to how the API, worker and providers talk, anything touching tenant isolation, a new dependency on the shared KVM2 box.

Ask the `architect` agent; it returns the record, the main session writes the file. `integrations`, `ai-pipeline` and `reviewer` read this folder before working, so an accepted ADR binds them.

Statuses: `proposed` → `accepted` → `superseded by ADR-NNN`. Never delete a superseded record; it explains why the code looks the way it does.

## Template

```markdown
# ADR-NNN: <title>
Status: proposed | accepted | superseded by ADR-NNN
Date: YYYY-MM-DD

## Context
The forces behind this decision, with file:line evidence.

## Decision
One paragraph, concrete.

## Tenant-isolation check
Where tenant_id lives in this design; how one tenant's data cannot reach another.

## Resource check
RAM / new containers / migrations required. The deploy target is a shared 7.8 GB box.

## Alternatives rejected
Each with one line on why.

## Consequences
Easier, harder, must be revisited when.

## Implementation notes
Files to touch, in order — for integrations / ai-pipeline.
```
