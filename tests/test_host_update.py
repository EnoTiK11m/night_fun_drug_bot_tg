import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import host_update


class HostUpdateTests(unittest.TestCase):
    def test_preflight_failure_keeps_old_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            issues = []

            def command(*args, **kwargs):
                calls.append(args)
                if args[:2] == ("git", "clone"):
                    return ""
                if args[:3] == ("git", "rev-parse", "HEAD"):
                    return "b" * 40
                if args[:2] == ("docker", "build"):
                    raise RuntimeError("build failed")
                raise AssertionError(args)

            with (
                patch.object(host_update, "PROJECT", root),
                patch.object(host_update, "STATE", root / "state.json"),
                patch.object(host_update, "PENDING", root / "issue.txt"),
                patch.object(host_update, "container_image", return_value="sha256:old"),
                patch.object(host_update, "run", side_effect=command),
                patch.object(host_update, "compose") as compose,
                patch.object(host_update, "report_failure", side_effect=lambda *x: issues.append(x)),
            ):
                with self.assertRaises(RuntimeError):
                    host_update.deploy()
            compose.assert_not_called()
            self.assertEqual(issues[0][1:3], ("build", "old containers remained running"))

    def test_failed_health_restarts_previous_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            switched = []
            issues = []

            def command(*args, **kwargs):
                if args[:2] == ("git", "clone"):
                    return ""
                if args[:3] == ("git", "rev-parse", "HEAD"):
                    return "b" * 40
                if args[:2] in (("docker", "build"), ("docker", "run")):
                    return ""
                raise AssertionError(args)

            def compose(*args, **kwargs):
                if args[:1] == ("exec",):
                    return ""
                if args[:1] == ("up",):
                    self.assertEqual(args[-1], "bot")
                    switched.append(Path(kwargs["override"]).read_text(encoding="utf-8"))
                    return ""
                raise AssertionError(args)

            with (
                patch.object(host_update, "PROJECT", root),
                patch.object(host_update, "STATE", root / "state.json"),
                patch.object(host_update, "PENDING", root / "issue.txt"),
                patch.object(host_update, "container_image", side_effect=["sha256:oldbot", "sha256:oldbackup"]),
                patch.object(host_update, "run", side_effect=command),
                patch.object(host_update, "compose", side_effect=compose),
                patch.object(host_update, "health_status", return_value=(False, "bot: state=false false 1")),
                patch.object(host_update, "healthy", return_value=True),
                patch.object(host_update.time, "sleep"),
                patch.object(host_update, "report_failure", side_effect=lambda *x: issues.append(x)),
            ):
                with self.assertRaises(RuntimeError):
                    host_update.deploy()
            self.assertEqual(len(switched), 2)
            self.assertIn("candidate-", switched[0])
            self.assertIn("sha256:oldbot", switched[1])
            self.assertIn("sha256:oldbackup", switched[1])
            self.assertEqual(issues[0][1:3], ("health", "previous images restarted"))
            self.assertIn("bot: state=", issues[0][3])


if __name__ == "__main__":
    unittest.main()
