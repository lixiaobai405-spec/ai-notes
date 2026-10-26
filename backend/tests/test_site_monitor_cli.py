"""Real operator process and isolated PostgreSQL; host probes are synthetic."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import text

from app.shared.db import Base, engine

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.file_gc_state import PrivateState


def fingerprint():
    with engine.connect() as connection:
        values = [connection.execute(text(
            'SELECT md5(COALESCE(string_agg(row_to_json(t)::text,\',\' ORDER BY row_to_json(t)::text),\'\')) FROM public."'
            + table.name + '" t')).scalar_one() for table in Base.metadata.sorted_tables]
    return hashlib.sha256("".join(values).encode()).hexdigest()


@pytest.fixture
def workspace(tmp_path):
    private = PrivateState(tmp_path / "config")
    cfg = json.loads((ROOT / "ops/monitoring.example.json").read_text())
    cfg.update(state_root=str(tmp_path / "state"), disks={key: str(tmp_path) for key in cfg["disks"]})
    cfg["database_url_env"] = "MONITOR_CLI_TEST_DATABASE_URL"
    private.write("config.json", cfg)
    env = {**os.environ, cfg["database_url_env"]: engine.url.render_as_string(hide_password=False),
           "DEEPSEEK_API_KEY": "", "STORAGE_BACKEND": "local"}
    # Only replace host collection. Database aggregation, CLI validation,
    # locking, serialization and private atomic persistence are production code.
    source = ("import sys; from unittest.mock import patch; "
              "from tests.test_site_monitor import host_fixture; "
              "from scripts.site_monitor import main; "
              "p=patch('scripts.monitoring_host.collect_host_metrics',host_fixture); p.start(); "
              "raise SystemExit(main(sys.argv[1:]))")
    def cli(command, expected, environment=None):
        result = subprocess.run([sys.executable, "-c", source, command, "--config", str(private.root / "config.json")],
            cwd=ROOT, env=env if environment is None else environment, capture_output=True, text=True, timeout=40)
        assert result.returncode == expected, "monitor CLI unexpected exit"
        assert result.stderr == ""
        assert env[cfg["database_url_env"]] not in result.stdout
        assert str(tmp_path) not in result.stdout
        return json.loads(result.stdout)
    return cfg, env, cli


def test_real_sample_preserves_database_and_reports_empty_queue_without_invented_cost(workspace):
    cfg, env, cli = workspace
    before = fingerprint()
    assert cli("check", 0)["status"] == "valid"
    assert cli("sample", 0)["status"] == "ok"
    assert cli("status", 0)["status"] == "ok"
    saved = PrivateState(cfg["state_root"]).read("monitor.json")
    db = saved["snapshot"]["metrics"]["database"]
    assert db["status"] == "ok"
    assert db["jobs"]["oldest_ready_seconds"] is None
    assert db["usage"]["cost"] is None and db["usage"]["cost_status"] == "unavailable"
    assert db["window_end"] == saved["snapshot"]["observed_at"]
    assert env[cfg["database_url_env"]] not in json.dumps(saved)
    assert before == fingerprint()


def test_missing_database_can_store_unknown_but_cannot_pass_status_gate(workspace):
    cfg, env, cli = workspace
    without_database = {key: value for key, value in env.items() if key != cfg["database_url_env"]}
    assert cli("check", 0, without_database)["status"] == "valid"
    assert cli("sample", 0, without_database)["status"] == "unknown"
    assert cli("status", 2, without_database)["status"] == "unknown"


def test_status_does_not_connect_to_an_unreachable_database(workspace):
    cfg, env, cli = workspace
    cli("sample", 0)
    changed = {**env, cfg["database_url_env"]: "postgresql+psycopg://fake:fake@192.0.2.1:9/other"}
    result = cli("status", 2, changed)
    assert result["status"] == "unknown" and result["code"] == "monitoring_target_changed"
