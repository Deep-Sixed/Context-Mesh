"""7E acceptance: a selective rerun survives real process boundaries.

7A-7D prove the pieces separately: durable worker keys, a restorable ledger, a
resumable execution plan, and an atomic four-companion session. This test asks
the only question that matters after all four exist together: can one process
die while a plan is mid-repair, can a different process resume exactly the stale
closure, and can a third process read back the result without resurrecting the
old implementation or rerunning unrelated work?

The three interpreters share only the session directory. Every process constructs
its own TaskRegistry, because registry contents are deployment configuration and
must never cross the checkpoint boundary.
"""

import json  # noqa: I001
import pathlib
import subprocess
import sys
import tempfile
import unittest


_CREATE = r'''
import json
import sys

from contextmesh.evidence import submit_evidence
from contextmesh.execute import Runner, TaskRegistry
from contextmesh.model import NodeType
from contextmesh_mcp.session import Session


class Advisory:
    def __init__(self):
        self.evidence_id = None

    def publish(self, graph):
        source = graph.add_node(
            NodeType.SOURCE,
            "Security advisory feed",
            id="source:advisory-feed",
            attrs={"origin": "advisory-feed", "retrieved_at": "2026-07-08"},
        )
        self.evidence_id = submit_evidence(
            graph,
            text="CVE-2026-9999 published for argon2id",
            source_id=source.id,
            metadata={"package": "argon2"},
        ).evidence_id
        return self.evidence_id

    def __call__(self, ctx):
        if self.evidence_id and ctx.output.get("impl") == "argon2":
            return ctx.disproved(
                "CVE-2026-9999 published for argon2id", evidence_id=self.evidence_id
            )
        return True


def deployment(advisory):
    registry = TaskRegistry()
    registry.register_worker("auth.schema.v1", lambda ctx: {"ok": True})
    registry.register_worker("auth.hash.argon2.v1", lambda ctx: {"impl": "argon2"})
    registry.register_worker("auth.hash.bcrypt.v1", lambda ctx: {"impl": "bcrypt"})
    registry.register_worker("auth.tokens.v1", lambda ctx: {"ok": True})
    registry.register_auditor("auth.hash.audit.v1", advisory)
    return registry


root = sys.argv[1]
base = Session.build(rounds=2)
advisory = Advisory()
runner = Runner("auth", graph=base.graph, registry=deployment(advisory))
runner.task(
    "schema",
    worker_key="auth.schema.v1",
    assumes="sqlite is fine",
    produces=("Schema",),
)
runner.task(
    "hashing",
    worker_key="auth.hash.argon2.v1",
    auditor_key="auth.hash.audit.v1",
    assumes="Argon2 has no open advisory",
    needs=("schema",),
    produces=("Password Hasher",),
)
runner.task(
    "tokens",
    worker_key="auth.tokens.v1",
    assumes="JWT is fine",
    needs=("schema",),
    produces=("Tokens",),
)
runner.run()
advisory.publish(runner.graph)
runner.recheck()
runner.repair(
    "hashing",
    assumes="bcrypt has no open advisory",
    worker_key="auth.hash.bcrypt.v1",
)
base.runner = runner
base.save(root)

print(json.dumps({
    "generation": base.generation,
    "round": runner.round,
    "head": runner.ledger.head,
    "attempts": {task.name: task.attempt for task in runner.tasks},
    "states": {task.name: task.state.value for task in runner.tasks},
    "hash_worker": runner["hashing"].worker_key,
    "hash_output": runner["hashing"].output,
}, sort_keys=True))
'''


_RESUME = r'''
import json
import sys

from contextmesh.execute import TaskRegistry
from contextmesh.model import NodeType
from contextmesh_mcp.session import Session


class Advisory:
    def __call__(self, ctx):
        if ctx.output.get("impl") == "argon2":
            # Ingested before the save, restored with the graph. The auditor
            # identifies the observation; rule 7 forbids it inventing one.
            observed = next(
                node
                for node in ctx.graph.by_type(NodeType.EVIDENCE, live_only=False)
                if node.provenance is not None
                and node.provenance.source_id == "source:advisory-feed"
            )
            return ctx.disproved(
                "CVE-2026-9999 published for argon2id", evidence_id=observed.id
            )
        return True


def deployment():
    registry = TaskRegistry()
    registry.register_worker("auth.schema.v1", lambda ctx: {"ok": True})
    registry.register_worker("auth.hash.argon2.v1", lambda ctx: {"impl": "argon2"})
    registry.register_worker("auth.hash.bcrypt.v1", lambda ctx: {"impl": "bcrypt"})
    registry.register_worker("auth.tokens.v1", lambda ctx: {"ok": True})
    registry.register_auditor("auth.hash.audit.v1", Advisory())
    return registry


root = sys.argv[1]
session = Session.load(root, registry=deployment())
runner = session.runner
assert runner is not None
before_head = runner.ledger.head
before_len = len(runner.ledger)
report = runner.run()
after_head = runner.ledger.head
session.save(root)

print(json.dumps({
    "generation": session.generation,
    "round": runner.round,
    "before_head": before_head,
    "after_head": after_head,
    "ledger_growth": len(runner.ledger) - before_len,
    "executed": report.executed,
    "cached": report.cached,
    "blocked": report.blocked,
    "failed": report.failed,
    "attempts": {task.name: task.attempt for task in runner.tasks},
    "states": {task.name: task.state.value for task in runner.tasks},
    "hash_worker": runner["hashing"].worker_key,
    "hash_output": runner["hashing"].output,
}, sort_keys=True))
'''


_OBSERVE = r'''
import json
import sys

from contextmesh.execute import TaskRegistry
from contextmesh.model import NodeType
from contextmesh_mcp.session import Session


class Advisory:
    def __call__(self, ctx):
        if ctx.output.get("impl") == "argon2":
            # Ingested before the save, restored with the graph. The auditor
            # identifies the observation; rule 7 forbids it inventing one.
            observed = next(
                node
                for node in ctx.graph.by_type(NodeType.EVIDENCE, live_only=False)
                if node.provenance is not None
                and node.provenance.source_id == "source:advisory-feed"
            )
            return ctx.disproved(
                "CVE-2026-9999 published for argon2id", evidence_id=observed.id
            )
        return True


def deployment():
    registry = TaskRegistry()
    registry.register_worker("auth.schema.v1", lambda ctx: {"ok": True})
    registry.register_worker("auth.hash.argon2.v1", lambda ctx: {"impl": "argon2"})
    registry.register_worker("auth.hash.bcrypt.v1", lambda ctx: {"impl": "bcrypt"})
    registry.register_worker("auth.tokens.v1", lambda ctx: {"ok": True})
    registry.register_auditor("auth.hash.audit.v1", Advisory())
    return registry


root = sys.argv[1]
session = Session.load(root, registry=deployment())
runner = session.runner
assert runner is not None

print(json.dumps({
    "generation": session.generation,
    "round": runner.round,
    "head": runner.ledger.head,
    "ledger_valid": runner.ledger.verify(),
    "attempts": {task.name: task.attempt for task in runner.tasks},
    "states": {task.name: task.state.value for task in runner.tasks},
    "hash_worker": runner["hashing"].worker_key,
    "hash_output": runner["hashing"].output,
}, sort_keys=True))
'''


def _process(script: str, directory: pathlib.Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", script, str(directory)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "child interpreter failed\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return json.loads(proc.stdout)


class CrossProcessSelectiveRerunTest(unittest.TestCase):
    def test_mid_repair_restart_reruns_only_the_stale_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp) / "session"

            created = _process(_CREATE, directory)
            self.assertEqual(created["generation"], 1)
            self.assertEqual(created["round"], 2)
            self.assertEqual(
                created["attempts"], {"schema": 1, "hashing": 1, "tokens": 1}
            )
            self.assertEqual(
                created["states"],
                {"schema": "done", "hashing": "stale", "tokens": "done"},
            )
            self.assertEqual(created["hash_worker"], "auth.hash.bcrypt.v1")
            # The repaired worker has not run yet. This is the dangerous crash
            # boundary: the output is still from argon2 while the durable key
            # already names bcrypt.
            self.assertEqual(created["hash_output"], {"impl": "argon2"})

            resumed = _process(_RESUME, directory)
            self.assertEqual(resumed["generation"], 2)
            self.assertEqual(resumed["round"], 3)
            self.assertEqual(resumed["before_head"], created["head"])
            self.assertNotEqual(resumed["after_head"], created["head"])
            self.assertGreater(resumed["ledger_growth"], 0)
            self.assertEqual(resumed["executed"], ["hashing"])
            self.assertEqual(resumed["cached"], ["schema", "tokens"])
            self.assertEqual(resumed["blocked"], {})
            self.assertEqual(resumed["failed"], {})
            self.assertEqual(
                resumed["attempts"], {"schema": 1, "hashing": 2, "tokens": 1}
            )
            self.assertEqual(
                resumed["states"],
                {"schema": "done", "hashing": "done", "tokens": "done"},
            )
            self.assertEqual(resumed["hash_worker"], "auth.hash.bcrypt.v1")
            self.assertEqual(resumed["hash_output"], {"impl": "bcrypt"})

            observed = _process(_OBSERVE, directory)
            self.assertEqual(observed["generation"], 2)
            self.assertEqual(observed["round"], 3)
            self.assertTrue(observed["ledger_valid"])
            self.assertEqual(observed["head"], resumed["after_head"])
            self.assertEqual(observed["attempts"], resumed["attempts"])
            self.assertEqual(observed["states"], resumed["states"])
            self.assertEqual(observed["hash_worker"], "auth.hash.bcrypt.v1")
            self.assertEqual(observed["hash_output"], {"impl": "bcrypt"})


if __name__ == "__main__":
    unittest.main()
