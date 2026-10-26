import json
from pathlib import Path
import unittest

from app.main import app


class OpenAPIContractTests(unittest.TestCase):
    def test_exported_schema_matches_committed_file(self):
        expected = json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"
        committed = Path(__file__).resolve().parents[1] / "openapi.json"
        self.assertEqual(committed.read_text(encoding="utf-8"), expected)

    def test_validation_response_description_is_stable_across_python_versions(self):
        descriptions = {
            operation["responses"]["422"]["description"]
            for path in app.openapi()["paths"].values()
            for operation in path.values()
            if "422" in operation.get("responses", {})
        }
        self.assertEqual(descriptions, {"Unprocessable Content"})

    def test_export_size_response_description_is_stable_across_python_versions(self):
        descriptions = {
            operation["responses"]["413"]["description"]
            for path in app.openapi()["paths"].values()
            for operation in path.values()
            if "413" in operation.get("responses", {})
        }
        self.assertEqual(descriptions, {"Content Too Large"})
