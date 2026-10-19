import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXACT_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9,._-]+\])?==(?P<version>[^;\s]+)"
    r"(?:\s*;\s*.+)?$"
)


def normalized_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_manifest(name):
    includes = []
    packages = {}
    invalid_lines = []

    for line_number, raw_line in enumerate(
        (PROJECT_ROOT / name).read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-i "):
            continue
        if line.startswith("-r "):
            includes.append(line[3:].strip())
            continue

        match = EXACT_REQUIREMENT.fullmatch(line)
        if match is None:
            invalid_lines.append((line_number, line))
            continue

        package = normalized_name(match.group("name"))
        packages.setdefault(package, []).append(line_number)

    return includes, packages, invalid_lines


class DependencyManifestConsistencyTests(unittest.TestCase):
    def test_runtime_and_development_declarations_are_exact_and_unique(self):
        runtime_includes, runtime_packages, runtime_invalid = read_manifest(
            "requirements.txt"
        )
        dev_includes, dev_packages, dev_invalid = read_manifest("requirements-dev.txt")

        self.assertEqual(runtime_includes, [])
        self.assertEqual(runtime_invalid, [])
        self.assertTrue(runtime_packages)
        self.assertEqual(
            {name: lines for name, lines in runtime_packages.items() if len(lines) > 1},
            {},
        )

        self.assertEqual(dev_includes, ["requirements.txt"])
        self.assertEqual(dev_invalid, [])
        self.assertEqual(
            {name: lines for name, lines in dev_packages.items() if len(lines) > 1},
            {},
        )
        self.assertEqual(set(runtime_packages) & set(dev_packages), set())

    def test_pipenv_cannot_reappear_as_a_competing_authority(self):
        self.assertFalse((PROJECT_ROOT / "Pipfile").exists())
        self.assertFalse((PROJECT_ROOT / "Pipfile.lock").exists())
