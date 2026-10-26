"""Frozen c2 schema rules are checked without connecting to any database."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy.dialects import postgresql


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import site_backup_profiles as profiles
from site_backup_support import SiteBackupError


class ProfileTests(unittest.TestCase):
    def assert_invalid(self, function, *arguments):
        with self.assertRaises(SiteBackupError) as caught:
            function(*arguments)
        self.assertEqual(caught.exception.code, "backup_schema_invalid")
        self.assertEqual(str(caught.exception), "backup_schema_invalid")

    def test_frozen_catalog_matches_legacy_migrations_and_has_no_new_pdf_or_ai_tables(self):
        document = profiles.legacy_catalog()
        self.assertEqual(document["revision"], "c2e41a9f3b67")
        self.assertEqual(len(document["catalog"]), 22)
        self.assertEqual(sum(len(table["columns"]) for table in document["catalog"].values()), 161)
        self.assertFalse({"pdf_imports", "ai_conversations", "ai_runs", "ai_steps", "ai_saved_notes"}
                         & set(document["catalog"]))
        columns = {column["name"] for column in document["catalog"]["note_pages"]["columns"]}
        self.assertNotIn("pdf_page_number", columns)
        self.assertIn("file_id", columns)
        for filename, expected in document["migration_sources_sha256_lf"].items():
            source = (ROOT / "backend" / "alembic" / "versions" / filename).read_bytes()
            self.assertEqual(hashlib.sha256(source.replace(b"\r\n", b"\n")).hexdigest(), expected)
        # Mutating a returned document cannot replace the next expected catalog.
        document["catalog"]["files"]["columns"].clear()
        self.assertTrue(profiles.legacy_catalog()["catalog"]["files"]["columns"])

    def test_bad_catalog_is_rejected_without_exposing_paths_or_source_text(self):
        document = profiles.legacy_catalog()
        mutations = [
            {**document, "revision": "2ef16e7ca892"},
            {**document, "format_version": True},
            {**document, "postgres_major": 17},
            {**document, "tables": document["tables"][:-1]},
            {**document, "catalog": {"files": document["catalog"]["files"]}},
            {**document, "unexpected": "SENSITIVE_SENTINEL"},
        ]
        bad_column = deepcopy(document)
        bad_column["catalog"]["files"]["columns"][0]["nullable"] = "false"
        mutations.append(bad_column)
        for kind in ("duplicate", "missing_original", "unknown_third"):
            bad_variant = deepcopy(document)
            pair = bad_variant["check_sqltext_variants"]["users"]["user_role"]
            if kind == "duplicate":
                pair[1] = pair[0]
            elif kind == "missing_original":
                pair[0] = "true"
            else:
                pair.append("true")
            mutations.append(bad_variant)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SENSITIVE_SENTINEL.json"
            with patch.object(profiles, "LEGACY_CATALOG_PATH", path):
                for value in mutations:
                    with self.subTest(value=value.get("revision")):
                        path.write_text(json.dumps(value), encoding="utf-8")
                        self.assert_invalid(profiles.legacy_catalog)
                path.write_text('{"revision":"one","revision":"two"}', encoding="utf-8")
                self.assert_invalid(profiles.legacy_catalog)
                path.unlink()
                self.assert_invalid(profiles.legacy_catalog)

    def inspector(self):
        inspector = Mock()
        inspector.get_columns.return_value = [
            {"name": "value", "type": postgresql.VARCHAR(20), "nullable": True,
             "default": "'two  spaces'::character varying", "comment": "ignored"},
            {"name": "owner_id", "type": postgresql.UUID(), "nullable": False, "default": None},
            {"name": "id", "type": postgresql.UUID(), "nullable": False, "default": None},
        ]
        inspector.get_pk_constraint.return_value = {"name": "items_pkey", "constrained_columns": ["id"]}
        inspector.get_foreign_keys.return_value = [{
            "name": "fk_owner", "constrained_columns": ["owner_id", "id"],
            "referred_schema": None, "referred_table": "owners", "referred_columns": ["owner_id", "id"],
            "options": {},
        }]
        inspector.get_unique_constraints.return_value = [
            {"name": "unique_value", "column_names": ["owner_id", "value"],
             "duplicates_index": "unique_value", "dialect_options": {"postgresql_nulls_not_distinct": False}},
        ]
        inspector.get_check_constraints.return_value = [
            {"name": "positive", "sqltext": "value <> 'two  spaces'::text"},
        ]
        inspector.get_indexes.return_value = [
            {"name": "items_value", "unique": False, "column_names": ["value", "owner_id"],
             "column_sorting": {"value": ("desc",)}, "dialect_options": {"postgresql_include": []}},
        ]
        return inspector

    def connection(self):
        connection = Mock()
        connection.execute.return_value.mappings.return_value = [
            {"table_name": "items", "constraint_name": name, "deferrable": False,
             "initially_deferred": False, "validated": True}
            for name in ("items_pkey", "fk_owner", "unique_value")
        ]
        return connection

    def test_capture_canonicalizes_reflection_order_but_preserves_semantic_column_order(self):
        inspector = self.inspector()
        with patch.object(profiles, "inspect", return_value=inspector):
            first = profiles.capture_catalog(self.connection(), {"items"})
            inspector.get_columns.return_value.reverse()
            inspector.get_foreign_keys.return_value[0]["referred_schema"] = "public"
            self.assertEqual(profiles.capture_catalog(self.connection(), {"items"}), first)
            table = first["items"]
            self.assertEqual([column["name"] for column in table["columns"]], ["id", "owner_id", "value"])
            self.assertEqual(table["columns"][0]["type"], "UUID")
            self.assertEqual(table["columns"][2]["type"], "VARCHAR(20)")
            self.assertEqual(table["columns"][2]["default"], "'two  spaces'::character varying")
            inspector.get_indexes.return_value[0]["column_names"].reverse()
            self.assertNotEqual(profiles.capture_catalog(self.connection(), {"items"}), first)

    def test_capture_retains_type_nullable_default_and_constraint_changes(self):
        for kind in ("type", "nullable", "default", "foreign_key", "check", "unique"):
            with self.subTest(kind=kind):
                inspector = self.inspector()
                with patch.object(profiles, "inspect", return_value=inspector):
                    before = profiles.capture_catalog(self.connection(), {"items"})
                    if kind in {"type", "nullable", "default"}:
                        inspector.get_columns.return_value[0][kind] = {
                            "type": postgresql.TEXT(), "nullable": False, "default": "'changed'::text",
                        }[kind]
                    elif kind == "foreign_key":
                        inspector.get_foreign_keys.return_value[0]["referred_columns"].reverse()
                    elif kind == "check":
                        inspector.get_check_constraints.return_value[0]["sqltext"] = "true"
                    else:
                        inspector.get_unique_constraints.return_value[0]["column_names"] = ["value"]
                    self.assertNotEqual(profiles.capture_catalog(self.connection(), {"items"}), before)

    def test_capture_distinguishes_unvalidated_foreign_keys_omitted_by_sqlalchemy(self):
        connection = self.connection()
        with patch.object(profiles, "inspect", return_value=self.inspector()):
            before = profiles.capture_catalog(connection, {"items"})
            next(row for row in connection.execute.return_value.mappings.return_value
                 if row["constraint_name"] == "fk_owner")["validated"] = False
            after = profiles.capture_catalog(connection, {"items"})
        self.assertNotEqual(before, after)
        self.assertFalse(after["items"]["foreign_keys"][0]["flags"]["validated"])

    def test_capture_preserves_primary_and_unique_deferral_semantics(self):
        for name in ("items_pkey", "unique_value"):
            with self.subTest(constraint=name):
                connection = self.connection()
                with patch.object(profiles, "inspect", return_value=self.inspector()):
                    before = profiles.capture_catalog(connection, {"items"})
                    flags = next(row for row in connection.execute.return_value.mappings.return_value
                                 if row["constraint_name"] == name)
                    flags.update(deferrable=True, initially_deferred=True)
                    self.assertNotEqual(profiles.capture_catalog(connection, {"items"}), before)

    def test_capture_retains_primary_key_index_include_columns(self):
        inspector = self.inspector()
        with patch.object(profiles, "inspect", return_value=inspector):
            before = profiles.capture_catalog(self.connection(), {"items"})
            inspector.get_pk_constraint.return_value["dialect_options"] = {"postgresql_include": ["value"]}
            after = profiles.capture_catalog(self.connection(), {"items"})
        self.assertNotEqual(before, after)
        self.assertEqual(after["items"]["primary_key"]["dialect_options"], {"postgresql_include": ["value"]})

    def test_legacy_validation_rejects_missing_extra_or_structurally_changed_tables(self):
        document = profiles.legacy_catalog()
        inspector = SimpleNamespace(get_table_names=lambda **kwargs: sorted(profiles.LEGACY_TABLE_NAMES))
        with patch.object(profiles, "inspect", return_value=inspector), \
                patch.object(profiles, "capture_catalog", return_value=deepcopy(document["catalog"])) as capture:
            profiles.validate_legacy_schema(object())
            column = next(item for item in capture.return_value["files"]["columns"]
                          if item["name"] == "object_key")
            column["nullable"] = True
            self.assert_invalid(profiles.validate_legacy_schema, object())
            capture.return_value = document["catalog"]
            for names in (profiles.LEGACY_TABLE_NAMES - {"files"}, profiles.LEGACY_TABLE_NAMES | {"ai_runs"}):
                inspector.get_table_names = lambda **kwargs: sorted(names)
                self.assert_invalid(profiles.validate_legacy_schema, object())

    def test_reflection_failures_share_existing_sanitized_exception_class(self):
        with patch.object(profiles, "inspect", side_effect=RuntimeError("SENSITIVE_SENTINEL")):
            self.assert_invalid(profiles.capture_catalog, object(), {"items"})
            self.assert_invalid(profiles.validate_legacy_schema, object())

    def test_only_two_trusted_complete_check_spellings_are_accepted(self):
        document = profiles.legacy_catalog()
        inspector = SimpleNamespace(get_table_names=lambda **kwargs: sorted(profiles.LEGACY_TABLE_NAMES))
        restored = deepcopy(document["catalog"])
        for table, variants in document["check_sqltext_variants"].items():
            for check in restored[table]["check_constraints"]:
                if check["name"] in variants:
                    check["sqltext"] = variants[check["name"]][1]
        with patch.object(profiles, "inspect", return_value=inspector), \
                patch.object(profiles, "capture_catalog", side_effect=lambda *args: deepcopy(restored)):
            profiles.validate_legacy_schema(object())
        for kind in ("literal", "operator", "unknown_third", "not_valid"):
            with self.subTest(kind=kind):
                changed = deepcopy(restored)
                check = next(item for item in changed["users"]["check_constraints"]
                             if item["name"] == "user_role")
                if kind == "literal":
                    check["sqltext"] = check["sqltext"].replace("'admin'", "'other'")
                elif kind == "operator":
                    check["sqltext"] = check["sqltext"].replace(" = ANY", " <> ANY")
                elif kind == "unknown_third":
                    check["sqltext"] = "(" + check["sqltext"] + ") OR true"
                else:
                    check["dialect_options"] = {"not_valid": True}
                with patch.object(profiles, "inspect", return_value=inspector), \
                        patch.object(profiles, "capture_catalog", return_value=changed):
                    self.assert_invalid(profiles.validate_legacy_schema, object())


if __name__ == "__main__":
    unittest.main()
