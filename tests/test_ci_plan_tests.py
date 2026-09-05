"""A missing standalone suite must fail before any test job can turn green."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from ci import plan_tests as planner


def populate(root):
    for relative, pattern in planner.REQUIRED_TEST_ROOTS:
        path = root / relative / pattern.replace("*", "sentinel")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


class InventoryTests(unittest.TestCase):
    def test_complete_inventory_always_selects_python_and_node(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            populate(root)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(0, planner.main([], root=root))
            data = json.loads(output.getvalue())
            self.assertIs(data["python"], True)
            self.assertIs(data["discord"], True)

    def test_every_missing_root_fails(self):
        for relative, _pattern in planner.REQUIRED_TEST_ROOTS:
            with self.subTest(root=relative), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                populate(root)
                target = root / relative
                for child in target.iterdir():
                    child.unlink()
                target.rmdir()
                with self.assertRaises(planner.MissingTestSuite):
                    planner.verify_test_roots(root)

    def test_every_empty_root_fails(self):
        for relative, pattern in planner.REQUIRED_TEST_ROOTS:
            with self.subTest(root=relative), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                populate(root)
                (root / relative / pattern.replace("*", "sentinel")).unlink()
                with self.assertRaises(planner.MissingTestSuite):
                    planner.verify_test_roots(root)

    def test_test_named_directory_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            populate(root)
            relative, pattern = planner.REQUIRED_TEST_ROOTS[0]
            target = root / relative / pattern.replace("*", "sentinel")
            target.unlink()
            target.mkdir()
            with self.assertRaises(planner.MissingTestSuite):
                planner.verify_test_roots(root)


if __name__ == "__main__":
    unittest.main()
