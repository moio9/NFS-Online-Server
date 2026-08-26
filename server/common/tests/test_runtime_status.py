from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from common.runtime_status import RuntimeStatusPublisher


class RuntimeStatusPublisherTests(unittest.TestCase):
    def test_start_publishes_immediately_and_stop_removes_own_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime" / "classic-status.json"
            publisher = RuntimeStatusPublisher(
                path,
                lambda: {"component": "classic", "games": {}},
                name="classic-test",
                interval=60.0,
            )

            publisher.start()
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], 1)
            self.assertEqual(payload["pid"], os.getpid())
            self.assertEqual(payload["component"], "classic")
            self.assertGreater(payload["updated_at"], 0)

            publisher.stop()
            self.assertFalse(path.exists())

    def test_stop_does_not_remove_snapshot_replaced_by_another_process(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "carbon-status.json"
            publisher = RuntimeStatusPublisher(
                path,
                lambda: {"component": "carbon"},
                name="carbon-test",
                interval=60.0,
            )

            publisher.start()
            replacement = {"schema": 1, "pid": os.getpid() + 1}
            path.write_text(json.dumps(replacement), encoding="utf-8")
            publisher.stop()

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                replacement,
            )


if __name__ == "__main__":
    unittest.main()
