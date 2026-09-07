# Directive validation diagnostics

The failed `minimax100-20260907T035839Z` run ended at P0 turn 12 after a text-only
response and a rejected `submit_directive` repair. Its event log retained response
shape and the generic `schema_or_ownership` reason, but no rejected arguments or
specific validator result. The exact invalid field cannot be recovered. Existing
raw run and save evidence is preserved; this change cannot repair that past run.

Validation failures now add a `validation_error` object to the existing
`strategy_response_shape` audit. For example:

```json
{"code":"unit_not_owned","path":["tactical_overrides",0,"unit_id"]}
```

The code distinguishes ownership from duplicate orders and identifies invalid
schema enums, values, destinations, targets, containers and argument combinations.
Paths contain only fixed schema field names and bounded input-array indexes. A
map entry with a model-supplied key, including `unit_targets`, is identified by its
parent schema path. Unknown property names, entity IDs, argument values, raw
provider content and exception text are never copied into this object. Unexpected
validation exceptions use `{"code":"unknown","path":[]}`. The diagnostic helper
rechecks its own allowlist and bounds before exposing exception attributes.

The same safe object is included in the next existing `format_repair` metadata.
Adaptive admission counts that complete repaired request before generation. The
provider tool schema, system instructions, limits and retry count are unchanged.
The previous generic category/reason is retained for existing consumers. Valid
responses carry no stale diagnostic, and no action from a rejected directive is
executed. Diagnostics do not authorize accepting, correcting or replaying it.

`DirectiveValidationError` remains a `ValueError`; successful normalization and
its existing rejection messages are preserved. The standalone `coordinate()`
utility keeps its existing behavior for other consumers.

When diagnosing another failure, bind the event prefix, configuration and source;
locate `strategy_response_shape.validation_error` inside the serialized strategy
payload; then join its next format-repair request/count/generation by sequence.
A code/path is a validation result, not proof of the rejected raw payload or of
model reasoning. No raw reply capture is introduced. A final invalid response
still aborts at the existing boundary with no extra model request or game action.

Validation evidence is retained under `runs/directive-diagnostics-evidence` in
the isolated worktree. Repository/mock-provider checks remain separate from native
or live-match acceptance. The failed turn 12 cause remains precisely unknown.
