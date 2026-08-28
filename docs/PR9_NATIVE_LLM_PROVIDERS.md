# PR #9 — Native LLM provider adapters

PR #9 adds a provider layer that can supply structured worker output and audit
*proposals* without moving the execution-authority boundary established by PRs
#7 and #8.

The implementation lives in `contextmesh/llm.py`. It uses only the Python
standard library, so the core package remains Python 3.9+ with zero required
runtime dependencies.

## Non-negotiable invariant

An LLM response is **data**, not authority.

A worker may use validated structured model output as its task output. An LLM
used for auditing may only return an `AuditProposal`:

```text
provider response
      ↓
strict JSON decode
      ↓
local schema validation
      ↓
AuditProposal(status, reason, evidence_id)
      ↓
DEPLOYMENT-OWNED AUDITOR CODE
      ↓
ctx.ok / ctx.fail / evidence-bound ctx.disproved
```

`AuditProposal` is deliberately not `Verdict`. The provider layer never calls
`AssumptionLedger.reject`, never invalidates a node, never creates a
contradiction edge, and never turns a client/model supplied status into a native
belief mutation.

A proposed disproof must name an already-ingested, live `evidence` node. This
keeps PR #8's sequence intact: observation enters first; registered code decides
whether it is sufficient to disprove ground.

## Providers

Live mode supports four explicit providers:

| Provider | Endpoint | Credential |
| --- | --- | --- |
| OpenAI | `POST /v1/responses` | `OPENAI_API_KEY` |
| Gemini | `POST /v1beta/interactions` | `GEMINI_API_KEY` |
| Anthropic | `POST /v1/messages` | `ANTHROPIC_API_KEY` |
| OpenRouter | `POST /api/v1/chat/completions` | `OPENROUTER_API_KEY` |

There is no provider auto-detection and no credential fallback. Selecting
OpenAI does not cause the client to try a Gemini, Anthropic, or OpenRouter key.
A live provider failure does not fall back to a simulator or another provider.

Each request uses the provider's native structured-output mechanism and the
result is validated again locally against a conservative common JSON Schema
subset. Provider-side schema enforcement is an optimization and compatibility
control, not the trust boundary.

Reference contracts used by this PR:

- OpenAI Responses structured output: `text.format` with `type=json_schema`.
- Gemini Interactions structured output: `response_format` with
  `mime_type=application/json` and `schema`.
- Anthropic Messages structured output: `output_config.format` with
  `type=json_schema`.
- OpenRouter structured output: `response_format.type=json_schema`, with
  `provider.require_parameters=true` so routing cannot silently choose a
  provider that ignores the requested parameter.

## Fail-closed parsing

Both the provider envelope and the model's structured payload are parsed with a
strict JSON decoder. The adapter refuses:

- duplicate object keys;
- `NaN` and infinities;
- non-UTF-8 provider bodies;
- non-object structured output;
- missing or extra fields relative to the schema;
- type coercion such as `true` becoming integer `1`;
- a provider refusal or incomplete/truncated response;
- unsupported JSON Schema keywords outside the portable subset.

The common schema subset requires closed objects (`additionalProperties=false`)
and requires every declared object property. That is intentionally narrower
than any one provider's full schema dialect.

## Retry boundary

Retries are bounded by `max_attempts`. Only transport failures, HTTP 429, and
HTTP 5xx are retryable. Other HTTP failures stop immediately. Backoff is local
and deterministic from `base_delay`; there is no provider switch during a
retry sequence.

Provider credentials are excluded from dataclass `repr` output, and transport
errors are normalized without echoing request headers or bodies.

## Simulation

Simulation is a separate explicit mode:

```python
from contextmesh.llm import LLMClient, LLMConfig

client = LLMClient(
    LLMConfig.simulation(),
    simulator=lambda prompt, schema, system: {"value": "deterministic"},
)
```

`provider="simulation"` is required in simulation mode, an API key is refused,
and a simulator callback must be supplied. A live client is not allowed to
carry a simulator callback. Provenance therefore cannot claim a live provider
when no live request happened.

## Worker integration

`make_worker()` returns an ordinary `Runner` worker. The model output is schema
validated before it is returned, and Context Mesh appends a reserved
`_contextmesh_llm` record containing provider, model, mode, attempts, token
usage, and response id when available.

The caller's output schema may not claim that reserved key.

## Audit integration

`propose_audit()` and `make_audit_proposer()` return `AuditProposal`, not
`Verdict`. A proposal with `status="disproved"` is valid only if its
`evidence_id` names a live evidence node already present in the graph. A
`success` or `fail` proposal may not smuggle an evidence id as a second verdict
channel.

The adapter performs no graph mutation while producing or validating a
proposal. Deployment-owned auditor code remains responsible for interpreting a
proposal and, if appropriate, invoking the existing PR #8 evidence-bound native
recheck path.

## Verification

`tests/test_llm.py` is entirely network-free and covers:

- provider-specific request shapes for OpenAI, Gemini, Anthropic, and OpenRouter;
- provider-specific response extraction and token/provenance accounting;
- exact credential selection and secret-safe representation;
- strict schema and instance validation;
- bounded retry behavior and non-transient fail-closed behavior;
- duplicate-key/non-finite JSON refusal;
- explicit simulation with no live fallback;
- the audit authority boundary and pre-ingested evidence requirement;
- worker provenance and reserved-output-key protection.

`.github/workflows/llm-provider-contracts.yml` runs this contract suite
separately from the repository's full matrix so a provider-boundary regression
is visible as its own gate.

A real provider smoke test is intentionally opt-in rather than a required CI
secret. `ops/llm-live-acceptance.py` runs one strict structured-output request
when `CONTEXTMESH_LLM_PROVIDER`, `CONTEXTMESH_LLM_MODEL`, and that provider's
native API-key environment variable are supplied. The normal test suite never
requires network access or provider credentials.
