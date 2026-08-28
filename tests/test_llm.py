"""PR #9: native LLM provider adapters stay below execution authority."""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List

from contextmesh.evidence import submit_evidence
from contextmesh.execute import AuditContext, RunContext, Runner, Verdict
from contextmesh.llm import (
    AuditProposal,
    HTTPRequest,
    HTTPResponse,
    LLMClient,
    LLMConfig,
    LLMConfigurationError,
    LLMProviderError,
    LLMResponseError,
    LLMSchemaError,
    LLMTransportError,
    make_audit_proposer,
    make_worker,
    propose_audit,
    validate_instance,
    validate_schema_definition,
)
from contextmesh.model import AssumptionStatus, NodeType


OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "value": {"type": "string", "minLength": 1},
        "count": {"type": "integer", "minimum": 0},
    },
    "required": ["value", "count"],
    "additionalProperties": False,
}


class QueueTransport:
    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.requests: List[HTTPRequest] = []

    def __call__(self, request: HTTPRequest) -> HTTPResponse:
        self.requests.append(request)
        if not self.items:
            raise AssertionError("fake transport ran out of responses")
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, HTTPResponse)
        return item


def _http(envelope: Any, status: int = 200) -> HTTPResponse:
    return HTTPResponse(
        status=status,
        headers={"content-type": "application/json"},
        body=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
    )


def _structured(provider: str, text: str, *, response_id: str = "response-1") -> HTTPResponse:
    if provider == "openai":
        return _http(
            {
                "id": response_id,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            }
        )
    if provider == "gemini":
        return _http(
            {
                "id": response_id,
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": text}],
                    }
                ],
                "usage": {
                    "total_input_tokens": 3,
                    "total_output_tokens": 4,
                    "total_tokens": 7,
                },
            }
        )
    if provider == "anthropic":
        return _http(
            {
                "id": response_id,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            }
        )
    if provider == "openrouter":
        return _http(
            {
                "id": response_id,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            }
        )
    raise AssertionError(provider)


def _live(provider: str, transport: QueueTransport, **kwargs: Any) -> LLMClient:
    return LLMClient(
        LLMConfig(
            provider=provider,
            model=f"{provider}-model",
            api_key=f"secret-{provider}",
            base_delay=0.01,
            **kwargs,
        ),
        transport=transport,
        sleep=lambda _seconds: None,
    )


class ConfigurationTest(unittest.TestCase):
    def test_live_configuration_reads_only_the_selected_provider_key(self) -> None:
        with self.assertRaisesRegex(LLMConfigurationError, "OPENAI_API_KEY"):
            LLMConfig.live_from_env(
                "openai",
                "model",
                environ={"GEMINI_API_KEY": "wrong-provider-key"},
            )
        config = LLMConfig.live_from_env(
            "openai",
            "model",
            environ={"OPENAI_API_KEY": "right-key", "GEMINI_API_KEY": "other-key"},
        )
        self.assertEqual(config.api_key, "right-key")

    def test_credentials_are_not_exposed_by_repr(self) -> None:
        config = LLMConfig(provider="openai", model="model", api_key="super-secret")
        self.assertNotIn("super-secret", repr(config))

    def test_live_mode_has_no_simulation_fallback(self) -> None:
        config = LLMConfig(provider="openai", model="model", api_key="key")
        with self.assertRaisesRegex(LLMConfigurationError, "never falls back"):
            LLMClient(config, simulator=lambda _p, _s, _sys: {"value": "x", "count": 1})

    def test_simulation_is_explicit_and_needs_a_callback(self) -> None:
        config = LLMConfig.simulation()
        with self.assertRaisesRegex(LLMConfigurationError, "explicit simulator"):
            LLMClient(config)
        with self.assertRaisesRegex(LLMConfigurationError, "provider='simulation'"):
            LLMConfig(provider="openai", model="model", mode="simulation")

    def test_bad_numeric_configuration_fails_closed(self) -> None:
        for field, value in (
            ("timeout", 0),
            ("timeout", float("nan")),
            ("max_attempts", True),
            ("max_attempts", 0),
            ("base_delay", -1),
            ("max_tokens", 0),
        ):
            with self.subTest(field=field, value=value):
                args = {field: value}
                with self.assertRaises(LLMConfigurationError):
                    LLMConfig(provider="openai", model="model", api_key="key", **args)


class SchemaTest(unittest.TestCase):
    def test_portable_schema_accepts_the_contract_subset(self) -> None:
        validate_schema_definition(OUTPUT_SCHEMA)
        validate_instance({"value": "ok", "count": 2}, OUTPUT_SCHEMA)

    def test_schema_requires_closed_objects_and_all_properties_required(self) -> None:
        open_schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }
        with self.assertRaisesRegex(LLMConfigurationError, "additionalProperties"):
            validate_schema_definition(open_schema)

        optional_schema = {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "optional": {"type": "string"},
            },
            "required": ["value"],
            "additionalProperties": False,
        }
        with self.assertRaisesRegex(LLMConfigurationError, "require every property"):
            validate_schema_definition(optional_schema)

    def test_unknown_schema_keyword_is_refused(self) -> None:
        schema = {"type": "string", "pattern": "^x$"}
        with self.assertRaisesRegex(LLMConfigurationError, "unsupported schema keyword"):
            validate_schema_definition(schema)

    def test_instance_validation_does_not_coerce(self) -> None:
        with self.assertRaises(LLMSchemaError):
            validate_instance({"value": "ok", "count": True}, OUTPUT_SCHEMA)
        with self.assertRaises(LLMSchemaError):
            validate_instance({"value": "ok", "count": "2"}, OUTPUT_SCHEMA)
        with self.assertRaises(LLMSchemaError):
            validate_instance({"value": "ok", "count": 2, "extra": 1}, OUTPUT_SCHEMA)


class ProviderContractTest(unittest.TestCase):
    def test_each_provider_uses_its_native_structured_output_contract(self) -> None:
        expected_urls = {
            "openai": "https://api.openai.com/v1/responses",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/interactions",
            "anthropic": "https://api.anthropic.com/v1/messages",
            "openrouter": "https://openrouter.ai/api/v1/chat/completions",
        }
        text = json.dumps({"value": "ok", "count": 2})
        for provider in expected_urls:
            with self.subTest(provider=provider):
                transport = QueueTransport(_structured(provider, text))
                client = _live(provider, transport)
                result = client.complete(
                    "do the work",
                    OUTPUT_SCHEMA,
                    schema_name="contract_output",
                    system="system rule",
                )
                self.assertEqual(result.data, {"value": "ok", "count": 2})
                self.assertEqual(result.provenance.provider, provider)
                self.assertEqual(result.provenance.model, f"{provider}-model")
                self.assertEqual(result.provenance.mode, "live")
                self.assertEqual(result.provenance.attempts, 1)
                self.assertEqual(result.provenance.usage.total_tokens, 7)
                self.assertEqual(result.provenance.response_id, "response-1")

                request = transport.requests[0]
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.url, expected_urls[provider])
                body = json.loads(request.body.decode("utf-8"))
                self.assertEqual(body["model"], f"{provider}-model")
                self._assert_native_request(provider, request, body)

    def _assert_native_request(
        self,
        provider: str,
        request: HTTPRequest,
        body: Dict[str, Any],
    ) -> None:
        if provider == "openai":
            self.assertEqual(request.headers["Authorization"], "Bearer secret-openai")
            format_ = body["text"]["format"]
            self.assertEqual(format_["type"], "json_schema")
            self.assertEqual(format_["name"], "contract_output")
            self.assertIs(format_["strict"], True)
            self.assertEqual(format_["schema"], OUTPUT_SCHEMA)
            self.assertEqual(body["instructions"], "system rule")
            self.assertIs(body["store"], False)
            return
        if provider == "gemini":
            self.assertEqual(request.headers["x-goog-api-key"], "secret-gemini")
            format_ = body["response_format"]
            self.assertEqual(format_["type"], "text")
            self.assertEqual(format_["mime_type"], "application/json")
            self.assertEqual(format_["schema"], OUTPUT_SCHEMA)
            self.assertEqual(body["system_instruction"], "system rule")
            self.assertIs(body["store"], False)
            return
        if provider == "anthropic":
            self.assertEqual(request.headers["x-api-key"], "secret-anthropic")
            self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
            format_ = body["output_config"]["format"]
            self.assertEqual(format_["type"], "json_schema")
            self.assertEqual(format_["schema"], OUTPUT_SCHEMA)
            self.assertEqual(body["system"], "system rule")
            return
        self.assertEqual(request.headers["Authorization"], "Bearer secret-openrouter")
        format_ = body["response_format"]
        self.assertEqual(format_["type"], "json_schema")
        self.assertEqual(format_["json_schema"]["name"], "contract_output")
        self.assertIs(format_["json_schema"]["strict"], True)
        self.assertEqual(format_["json_schema"]["schema"], OUTPUT_SCHEMA)
        self.assertIs(body["provider"]["require_parameters"], True)
        self.assertEqual(body["messages"][0], {"role": "system", "content": "system rule"})

    def test_simulation_never_calls_the_http_transport(self) -> None:
        transport = QueueTransport(AssertionError("network path was reached"))
        client = LLMClient(
            LLMConfig.simulation(),
            transport=transport,
            simulator=lambda _prompt, _schema, _system: {"value": "sim", "count": 1},
        )
        result = client.complete("simulate", OUTPUT_SCHEMA)
        self.assertEqual(result.data, {"value": "sim", "count": 1})
        self.assertEqual(result.provenance.provider, "simulation")
        self.assertEqual(result.provenance.mode, "simulation")
        self.assertEqual(transport.requests, [])


class RetryAndResponseTest(unittest.TestCase):
    def test_429_and_5xx_retry_with_a_bound(self) -> None:
        sleeps: List[float] = []
        transport = QueueTransport(
            _http({"error": "rate"}, status=429),
            _http({"error": "down"}, status=503),
            _structured("openai", json.dumps({"value": "ok", "count": 1})),
        )
        client = LLMClient(
            LLMConfig(
                provider="openai",
                model="model",
                api_key="key",
                max_attempts=3,
                base_delay=0.25,
            ),
            transport=transport,
            sleep=sleeps.append,
        )
        result = client.complete("work", OUTPUT_SCHEMA)
        self.assertEqual(result.provenance.attempts, 3)
        self.assertEqual(sleeps, [0.25, 0.5])
        self.assertEqual(len(transport.requests), 3)

    def test_non_transient_http_failure_is_not_retried(self) -> None:
        transport = QueueTransport(_http({"error": "bad request"}, status=400))
        client = _live("openai", transport, max_attempts=3)
        with self.assertRaises(LLMProviderError) as caught:
            client.complete("work", OUTPUT_SCHEMA)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(len(transport.requests), 1)

    def test_transport_failure_retries_but_never_switches_provider(self) -> None:
        transport = QueueTransport(
            LLMTransportError("offline"),
            _structured("openai", json.dumps({"value": "ok", "count": 1})),
        )
        client = _live("openai", transport, max_attempts=2)
        result = client.complete("work", OUTPUT_SCHEMA)
        self.assertEqual(result.provenance.provider, "openai")
        self.assertEqual(result.provenance.attempts, 2)
        self.assertEqual({request.url for request in transport.requests}, {
            "https://api.openai.com/v1/responses"
        })

    def test_duplicate_keys_and_non_finite_output_are_refused(self) -> None:
        for text in (
            '{"value":"first","value":"second","count":1}',
            '{"value":"x","count":NaN}',
        ):
            with self.subTest(text=text):
                client = _live("openai", QueueTransport(_structured("openai", text)))
                with self.assertRaises(LLMResponseError):
                    client.complete("work", OUTPUT_SCHEMA)

    def test_schema_mismatch_is_refused_even_after_provider_success(self) -> None:
        client = _live(
            "openai",
            QueueTransport(_structured("openai", '{"value":"ok","count":"one"}')),
        )
        with self.assertRaises(LLMSchemaError):
            client.complete("work", OUTPUT_SCHEMA)

    def test_provider_envelope_is_parsed_strictly_too(self) -> None:
        response = HTTPResponse(
            status=200,
            headers={},
            body=b'{"status":"completed","status":"failed"}',
        )
        client = _live("openai", QueueTransport(response))
        with self.assertRaisesRegex(LLMResponseError, "duplicate key"):
            client.complete("work", OUTPUT_SCHEMA)


class AuditAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = Runner("llm-audit-boundary")
        self.task = self.runner.task(
            "inspect",
            run=lambda _ctx: {"checked": True},
            assumes="The observed condition still holds",
        )
        assert self.task.assumption_id is not None
        self.assumption = self.runner.graph.assumptions[self.task.assumption_id]
        self.source = self.runner.graph.add_node(NodeType.SOURCE, "External observation source")
        self.evidence = submit_evidence(
            self.runner.graph,
            text="The observed condition no longer holds",
            source_id=self.source.id,
            external_id="llm-test-evidence-1",
        ).node
        self.context = AuditContext(
            task=self.task,
            output={"checked": True},
            assumption=self.assumption,
            graph=self.runner.graph,
        )

    def _client(self, value: Dict[str, Any]) -> LLMClient:
        return LLMClient(
            LLMConfig.simulation(),
            simulator=lambda _prompt, _schema, _system: dict(value),
        )

    def test_llm_disproof_is_only_a_proposal_and_mutates_nothing(self) -> None:
        before = (
            len(self.runner.graph.nodes),
            len(self.runner.graph.edges),
            self.assumption.status,
            sum(node.invalidated for node in self.runner.graph.nodes.values()),
        )
        proposal = propose_audit(
            self._client(
                {
                    "status": "disproved",
                    "reason": "the evidence contradicts the ground",
                    "evidence_id": self.evidence.id,
                }
            ),
            self.context,
            instruction="Check the standing assumption against available evidence.",
        )
        after = (
            len(self.runner.graph.nodes),
            len(self.runner.graph.edges),
            self.assumption.status,
            sum(node.invalidated for node in self.runner.graph.nodes.values()),
        )
        self.assertIsInstance(proposal, AuditProposal)
        self.assertNotIsInstance(proposal, Verdict)
        self.assertEqual(proposal.evidence_id, self.evidence.id)
        self.assertEqual(before, after)
        self.assertIs(self.assumption.status, AssumptionStatus.ACTIVE)

    def test_disproof_must_name_pre_ingested_live_evidence(self) -> None:
        client = self._client(
            {
                "status": "disproved",
                "reason": "unsupported assertion",
                "evidence_id": "evidence:not-present",
            }
        )
        with self.assertRaisesRegex(LLMSchemaError, "not in the graph"):
            propose_audit(client, self.context, instruction="Check the assumption.")

    def test_non_disproof_cannot_smuggle_an_evidence_binding(self) -> None:
        client = self._client(
            {
                "status": "success",
                "reason": "still holds",
                "evidence_id": self.evidence.id,
            }
        )
        with self.assertRaisesRegex(LLMSchemaError, "hidden verdict channel"):
            propose_audit(client, self.context, instruction="Check the assumption.")

    def test_invalidated_evidence_cannot_support_a_proposal(self) -> None:
        self.evidence.invalidated = True
        client = self._client(
            {
                "status": "disproved",
                "reason": "stale evidence should not count",
                "evidence_id": self.evidence.id,
            }
        )
        with self.assertRaisesRegex(LLMSchemaError, "invalidated"):
            propose_audit(client, self.context, instruction="Check the assumption.")

    def test_audit_proposer_factory_still_returns_a_proposal_not_a_verdict(self) -> None:
        proposer = make_audit_proposer(
            self._client({"status": "success", "reason": "holds", "evidence_id": None}),
            "Check the assumption.",
        )
        proposal = proposer(self.context)
        self.assertIsInstance(proposal, AuditProposal)
        self.assertNotIsInstance(proposal, Verdict)


class WorkerTest(unittest.TestCase):
    def test_worker_carries_provider_provenance_in_durable_output(self) -> None:
        runner = Runner("llm-worker")
        task = runner.task("draft", run=lambda _ctx: {}, assumes="Inputs are valid")
        assert task.assumption_id is not None
        assumption = runner.graph.assumptions[task.assumption_id]
        context = RunContext(
            task=task,
            attempt=1,
            graph=runner.graph,
            assumption=assumption,
            inputs={"source": {"value": "input"}},
        )
        client = LLMClient(
            LLMConfig.simulation(model="sim-v1"),
            simulator=lambda _prompt, _schema, _system: {"value": "done", "count": 1},
        )
        worker = make_worker(client, "Produce the requested object.", OUTPUT_SCHEMA)
        output = worker(context)
        self.assertEqual(output["value"], "done")
        self.assertEqual(output["count"], 1)
        provenance = output["_contextmesh_llm"]
        self.assertEqual(provenance["provider"], "simulation")
        self.assertEqual(provenance["model"], "sim-v1")
        self.assertEqual(provenance["mode"], "simulation")

    def test_worker_schema_cannot_claim_the_reserved_provenance_key(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "_contextmesh_llm": {"type": "string"},
            },
            "required": ["_contextmesh_llm"],
            "additionalProperties": False,
        }
        client = LLMClient(
            LLMConfig.simulation(),
            simulator=lambda _prompt, _schema, _system: {"_contextmesh_llm": "fake"},
        )
        with self.assertRaisesRegex(LLMConfigurationError, "reserved key"):
            make_worker(client, "Do work.", schema)


if __name__ == "__main__":
    unittest.main()
