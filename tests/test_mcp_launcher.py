"""The MCP/session launcher carries deployment-owned execution identity."""

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from contextmesh.execute import Runner, TaskRegistry
from contextmesh_mcp.session import (
    DEFAULT_CHECKPOINT,
    Session,
    SessionError,
    main as session_main,
    open_session,
)


def deployment():
    registry = TaskRegistry()
    registry.register_worker(
        "launcher.work.v1", lambda ctx: {"attempt": ctx.attempt, "ok": True}
    )
    return registry


def write_execution_session(directory):
    base = Session.build(rounds=1)
    runner = Runner("launcher plan", graph=base.graph, registry=deployment())
    runner.task(
        "work",
        worker_key="launcher.work.v1",
        assumes="the launch ground holds",
        produces=("Launcher Output",),
    )
    runner.run()
    base.runner = runner
    base.save(directory)


def args_for(directory):
    return argparse.Namespace(
        demo=False,
        session=str(directory),
        rounds=None,
        save=None,
        checkpoint=DEFAULT_CHECKPOINT,
    )


class ExecutionAwareLauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "session"
        write_execution_session(self.directory)

    def test_execution_session_without_registry_fails_before_serving(self):
        with self.assertRaises(SessionError) as caught:
            open_session(args_for(self.directory))
        self.assertIn("TaskRegistry", str(caught.exception))

    def test_open_session_passes_the_deployment_registry_to_restore(self):
        opened = open_session(args_for(self.directory), registry=deployment())
        self.assertIsNotNone(opened.runner)
        self.assertEqual(opened.runner.plan, "launcher plan")
        self.assertEqual(opened.runner["work"].worker_key, "launcher.work.v1")
        self.assertEqual(
            opened.runner.registry.describe()["workers"], ["launcher.work.v1"]
        )

    def test_missing_key_fails_closed_instead_of_guessing_code(self):
        with self.assertRaises(SessionError) as caught:
            open_session(args_for(self.directory), registry=TaskRegistry())
        self.assertIn("launcher.work.v1", str(caught.exception))
        self.assertIn("not registered", str(caught.exception))

    def test_plain_python_session_launcher_accepts_the_same_registry(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = session_main(
                ["--session", str(self.directory)], registry=deployment()
            )
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertIn('"persistent": true', stdout.getvalue())

    def test_mcp_transport_main_forwards_registry_to_open_session(self):
        server = Path(__file__).resolve().parents[1] / "contextmesh_mcp" / "server.py"
        source = server.read_text(encoding="utf-8")
        self.assertIn("registry: Optional[TaskRegistry] = None", source)
        self.assertIn("opened = open_session(args, registry=registry)", source)


if __name__ == "__main__":
    unittest.main()
