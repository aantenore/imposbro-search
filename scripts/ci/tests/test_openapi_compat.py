from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "openapi-compat.py"
SPEC = importlib.util.spec_from_file_location("openapi_compat", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def contract():
    return {
        "openapi": "3.1.0",
        "paths": {
            "/widgets/{widget_id}": {
                "get": {
                    "operationId": "get_widget",
                    "parameters": [
                        {
                            "name": "widget_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Widget"}
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Widget": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "state": {"type": "string", "enum": ["active", "disabled"]},
                    },
                    "required": ["id"],
                }
            }
        },
    }


class CompatibilityTests(unittest.TestCase):
    def test_identical_and_optional_additions_are_compatible(self):
        old = contract()
        new = copy.deepcopy(old)
        new["components"]["schemas"]["Widget"]["properties"]["label"] = {
            "type": "string"
        }
        self.assertEqual(MODULE.compatibility_errors(old, new), [])

    def test_removed_operation_is_breaking(self):
        old = contract()
        new = copy.deepcopy(old)
        del new["paths"]["/widgets/{widget_id}"]["get"]
        self.assertTrue(MODULE.compatibility_errors(old, new))

    def test_added_required_field_is_breaking(self):
        old = contract()
        new = copy.deepcopy(old)
        new["components"]["schemas"]["Widget"]["required"].append("state")
        self.assertTrue(MODULE.compatibility_errors(old, new))

    def test_enum_narrowing_is_breaking(self):
        old = contract()
        new = copy.deepcopy(old)
        new["components"]["schemas"]["Widget"]["properties"]["state"]["enum"] = [
            "active"
        ]
        self.assertTrue(MODULE.compatibility_errors(old, new))

    def test_operation_id_change_is_breaking(self):
        old = contract()
        new = copy.deepcopy(old)
        new["paths"]["/widgets/{widget_id}"]["get"]["operationId"] = "read_widget"
        self.assertTrue(MODULE.compatibility_errors(old, new))


if __name__ == "__main__":
    unittest.main()
