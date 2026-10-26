"""Frozen schema evidence for the one supported pre-PDF site revision.

The catalog is generated once from a new PostgreSQL 16 database migrated through
344f1b4b059e -> b5ebe7f299de -> 0b3b93086d90 -> c2e41a9f3b67. It contains no
application rows. Its migration hashes use UTF-8 source bytes with CRLF changed
to LF, so an ordinary Windows checkout does not alter the provenance evidence.
Eight CHECK strings also retain their exact PostgreSQL 16 dump/restore spelling:
the parser moves an array text cast onto each literal when recreating that DDL.
Only these two complete strings per named constraint are accepted, with every
other catalog field compared exactly. No general SQL rewrite is performed.
Never regenerate the expected catalog from a database being backed up.
"""

import json
from pathlib import Path
import re

from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql

from site_backup_support import SiteBackupError


LEGACY_REVISION = "c2e41a9f3b67"
LEGACY_TABLE_NAMES = frozenset({
    "alembic_version", "ai_credentials", "audit_logs", "file_tickets",
    "file_variants", "files", "folders", "invite_codes", "jobs", "note_pages",
    "note_tags", "note_versions", "notebooks", "notes", "notifications",
    "outbox_events", "password_resets", "rate_buckets", "sessions", "tags",
    "upload_sessions", "users",
})
LEGACY_CATALOG_PATH = (Path(__file__).resolve().parents[1] / "ops" /
                       "site-backup-profiles" / f"{LEGACY_REVISION}.json")
_MIGRATIONS = frozenset({
    "344f1b4b059e_m0_foundation.py",
    "b5ebe7f299de_m1_notes_organization_and_versions.py",
    "0b3b93086d90_m1_private_files_upload_sessions_and_.py",
    "c2e41a9f3b67_personal_ai_credentials.py",
})
_TABLE_FIELDS = {"columns", "primary_key", "foreign_keys", "unique_constraints",
                 "check_constraints", "indexes"}
_CATALOG_FIELDS = {"format_version", "revision", "postgres_major", "tables",
                   "migration_sources_sha256_lf", "catalog", "check_sqltext_variants"}
_RESTORED_CHECKS = {
    "ai_credentials": {"ai_credential_test_status"},
    "file_variants": {"file_variant_kind"},
    "files": {"file_status"},
    "jobs": {"job_status"},
    "notes": {"note_kind"},
    "upload_sessions": {"upload_status"},
    "users": {"user_role", "user_status"},
}
_CONSTRAINT_FLAGS = {"deferrable", "initially_deferred", "validated"}


def _require(condition):
    if not condition:
        raise SiteBackupError("backup_schema_invalid")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _canonical(value):
    """Keep only JSON schema metadata; no object repr or server identifiers."""
    if isinstance(value, dict):
        _require(all(isinstance(key, str) for key in value))
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    _require(value is None or type(value) in {bool, int, float} or isinstance(value, str))
    return value


def _ordered(items):
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True,
                                                    separators=(",", ":"), allow_nan=False))


def legacy_catalog():
    """Load a fresh copy of the checked-in catalog, never inspect a source DB."""
    try:
        raw = LEGACY_CATALOG_PATH.read_bytes()
        _require(len(raw) <= 1024 * 1024)
        document = json.loads(raw, object_pairs_hook=_pairs)
        _require(isinstance(document, dict) and set(document) == _CATALOG_FIELDS)
        _require(type(document["format_version"]) is int and document["format_version"] == 1)
        _require(document["revision"] == LEGACY_REVISION)
        _require(type(document["postgres_major"]) is int and document["postgres_major"] == 16)
        _require(document["tables"] == sorted(LEGACY_TABLE_NAMES))
        sources = document["migration_sources_sha256_lf"]
        _require(isinstance(sources, dict) and set(sources) == _MIGRATIONS
                 and all(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
                         for digest in sources.values()))
        catalog = document["catalog"]
        _require(isinstance(catalog, dict) and set(catalog) == LEGACY_TABLE_NAMES)
        for table in catalog.values():
            _require(isinstance(table, dict) and set(table) == _TABLE_FIELDS)
            _require(isinstance(table["columns"], list) and bool(table["columns"]))
            names = []
            for column in table["columns"]:
                _require(isinstance(column, dict)
                         and set(column) == {"name", "type", "nullable", "default", "identity", "computed"}
                         and isinstance(column["name"], str) and bool(column["name"])
                         and isinstance(column["type"], str) and bool(column["type"])
                         and type(column["nullable"]) is bool
                         and (column["default"] is None or isinstance(column["default"], str))
                         and all(column[field] is None or isinstance(column[field], dict)
                                 for field in ("identity", "computed")))
                names.append(column["name"])
            _require(names == sorted(set(names)))
            primary = table["primary_key"]
            _require(isinstance(primary, dict)
                     and set(primary) == {"name", "columns", "flags", "dialect_options"}
                     and (primary["name"] is None or isinstance(primary["name"], str))
                     and isinstance(primary["columns"], list) and bool(primary["columns"])
                     and isinstance(primary["dialect_options"], dict)
                     and all(column in names for column in primary["columns"]))
            _flags_valid(primary["flags"])
            for field in _TABLE_FIELDS - {"columns", "primary_key"}:
                _require(isinstance(table[field], list)
                         and all(isinstance(item, dict) for item in table[field]))
            for constraint in table["foreign_keys"] + table["unique_constraints"]:
                _flags_valid(constraint.get("flags"))
        variants = document["check_sqltext_variants"]
        _require(isinstance(variants, dict) and set(variants) == set(_RESTORED_CHECKS))
        for table, constraints in _RESTORED_CHECKS.items():
            _require(isinstance(variants[table], dict) and set(variants[table]) == constraints)
            for name in constraints:
                pair = variants[table][name]
                originals = [item["sqltext"] for item in catalog[table]["check_constraints"]
                             if item.get("name") == name]
                _require(len(originals) == 1 and isinstance(pair, list) and len(pair) == 2
                         and all(isinstance(value, str) and bool(value) for value in pair)
                         and len(set(pair)) == 2 and pair[0] == originals[0])
        return document
    except SiteBackupError:
        raise
    except Exception:
        raise SiteBackupError("backup_schema_invalid") from None


def _flags_valid(flags):
    _require(isinstance(flags, dict) and set(flags) == _CONSTRAINT_FLAGS
             and all(type(value) is bool for value in flags.values()))


def capture_catalog(connection, table_names):
    """Reflect deterministic public-table DDL metadata, without querying rows."""
    try:
        names = set(table_names)
        _require(bool(names) and all(isinstance(name, str)
                                    and re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names))
        inspector = inspect(connection)
        # Reflection omits FK validation and PK/UNIQUE deferral semantics.
        # Read these structural flags directly; never query application rows.
        constraint_flags = {(row["table_name"], row["constraint_name"]): {
            field: row[field] for field in _CONSTRAINT_FLAGS} for row in
            connection.execute(text("""
                SELECT r.relname AS table_name, c.conname AS constraint_name,
                       c.condeferrable AS deferrable, c.condeferred AS initially_deferred,
                       c.convalidated AS validated
                FROM pg_catalog.pg_constraint c
                JOIN pg_catalog.pg_class r ON r.oid=c.conrelid
                JOIN pg_catalog.pg_namespace n ON n.oid=r.relnamespace
                WHERE n.nspname='public' AND c.contype IN ('p','u','f')
            """)).mappings()}
        dialect = postgresql.dialect()
        catalog = {}
        for name in sorted(names):
            columns = [{
                "name": column["name"],
                "type": column["type"].compile(dialect=dialect),
                "nullable": column["nullable"],
                "default": column.get("default"),
                "identity": _canonical(column.get("identity")),
                "computed": _canonical(column.get("computed")),
            } for column in inspector.get_columns(name, schema="public")]
            primary = inspector.get_pk_constraint(name, schema="public")
            foreign = [{
                "name": item.get("name"),
                "columns": list(item["constrained_columns"]),
                "referred_schema": item.get("referred_schema") or "public",
                "referred_table": item["referred_table"],
                "referred_columns": list(item["referred_columns"]),
                "options": _canonical(item.get("options", {})),
                "flags": constraint_flags[(name, item["name"])],
            } for item in inspector.get_foreign_keys(name, schema="public")]
            unique = [{
                "name": item.get("name"),
                "columns": list(item["column_names"]),
                "dialect_options": _canonical(item.get("dialect_options", {})),
                "flags": constraint_flags[(name, item["name"])],
            } for item in inspector.get_unique_constraints(name, schema="public")]
            checks = [{
                "name": item.get("name"), "sqltext": item["sqltext"],
                "dialect_options": _canonical(item.get("dialect_options", {})),
            } for item in inspector.get_check_constraints(name, schema="public")]
            indexes = [{
                "name": item["name"], "unique": item["unique"],
                "columns": list(item["column_names"]),
                "expressions": _canonical(item.get("expressions")),
                "column_sorting": _canonical(item.get("column_sorting", {})),
                "dialect_options": _canonical(item.get("dialect_options", {})),
            } for item in inspector.get_indexes(name, schema="public")]
            catalog[name] = {
                "columns": sorted(columns, key=lambda item: item["name"]),
                "primary_key": {"name": primary.get("name"),
                                "columns": list(primary.get("constrained_columns", [])),
                                "dialect_options": _canonical(primary.get("dialect_options", {})),
                                "flags": constraint_flags[(name, primary["name"])]},
                "foreign_keys": _ordered(foreign),
                "unique_constraints": _ordered(unique),
                "check_constraints": _ordered(checks),
                "indexes": _ordered(indexes),
            }
        return _canonical(catalog)
    except SiteBackupError:
        raise
    except Exception:
        raise SiteBackupError("backup_schema_invalid") from None


def validate_legacy_schema(connection):
    """Reject a c2 stamp hiding any different table or reflected DDL metadata."""
    try:
        document = legacy_catalog()
        _require(set(inspect(connection).get_table_names(schema="public")) == LEGACY_TABLE_NAMES)
        catalog = capture_catalog(connection, LEGACY_TABLE_NAMES)
        for table, constraints in document["check_sqltext_variants"].items():
            for check in catalog[table]["check_constraints"]:
                pair = constraints.get(check.get("name"))
                if pair is not None and check.get("sqltext") in pair:
                    check["sqltext"] = pair[0]
        _require(catalog == document["catalog"])
    except SiteBackupError:
        raise
    except Exception:
        raise SiteBackupError("backup_schema_invalid") from None
