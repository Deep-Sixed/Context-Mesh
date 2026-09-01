#!/usr/bin/env python3
"""Opt-in live smoke test for the PR #9 provider adapter.

Required environment:

- CONTEXTMESH_LLM_PROVIDER: openai | gemini | anthropic | openrouter
- CONTEXTMESH_LLM_MODEL: provider-native model id
- the selected provider's normal API-key environment variable

This script is intentionally not part of normal CI. The contract suite uses
injected transports and requires no network or provider credentials.
"""

from __future__ import annotations

import json
import os
import sys

from contextmesh.llm import LLMClient, LLMConfig, LLMError

SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok"]},
        "purpose": {"type": "string", "enum": ["contextmesh-pr9-live-acceptance"]},
    },
    "required": ["status", "purpose"],
    "additionalProperties": False,
}


def main() -> int:
    provider = os.environ.get("CONTEXTMESH_LLM_PROVIDER", "")
    model = os.environ.get("CONTEXTMESH_LLM_MODEL", "")
    if not provider or not model:
        print(
            "set CONTEXTMESH_LLM_PROVIDER and CONTEXTMESH_LLM_MODEL before running",
            file=sys.stderr,
        )
        return 2

    try:
        config = LLMConfig.live_from_env(
            provider,
            model,
            timeout=45,
            max_attempts=3,
            base_delay=1.0,
            max_tokens=128,
        )
        result = LLMClient(config).complete(
            (
                "Return the exact requested structured object. "
                "status must be 'ok' and purpose must be "
                "'contextmesh-pr9-live-acceptance'."
            ),
            SCHEMA,
            schema_name="contextmesh_pr9_live_acceptance",
        )
    except LLMError as exc:
        print(f"live acceptance failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"data": result.data, "provenance": result.provenance.to_dict()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
