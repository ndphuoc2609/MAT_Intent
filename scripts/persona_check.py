"""Offline persona regression check; incomplete acceptance probes stay explicit."""
import argparse
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.persona_fixture import seed_nam_creta
from app.persona_store import PersonaStore, migrate_sqlite

UNIT_SUITES = (
    "tests.unit.test_persona_fixture", "tests.unit.test_persona_query",
    "tests.unit.test_persona_reply", "tests.unit.test_persona_store",
    "tests.unit.test_persona_engine", "tests.integration.test_sandbox_server",
)
ACCEPTANCE_GAPS = {
    "C01-C12": "No complete rule-by-rule positive/negative/boundary oracle yet.",
    "probes_01_20": "The required end-to-end matrix is not fully implemented.",
    "live_luna": "Not run by offline self-check; no model calls are made.",
}


def sqlite_preflight():
    with tempfile.TemporaryDirectory(prefix="persona-check-") as directory:
        connection = sqlite3.connect(Path(directory) / "persona.sqlite3")
        try:
            migrate_sqlite(connection)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("SQLite foreign-key check failed")
            persona_id, release_id = seed_nam_creta(PersonaStore(connection))
            release = PersonaStore(connection).get_release_snapshot(persona_id, release_id)["release"]
            if release["status"] != "FIXTURE" or release["approved"]:
                raise RuntimeError("Fixture was not isolated as an unapproved fixture release")
        finally:
            connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("sqlite", "postgres"), required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = {"backend": args.backend, "status": "incomplete", "preflight": "not_run",
              "regression": {}, "gaps": ACCEPTANCE_GAPS}
    if args.backend == "sqlite":
        try:
            sqlite_preflight()
            report["preflight"] = "pass"
            names = UNIT_SUITES
        except Exception as exc:
            report["preflight"] = "fail"
            report["preflight_error"] = type(exc).__name__
            names = ()
    else:
        dsn = os.environ.get("MAT_INTENT_PERSONA_TEST_DSN")
        if not dsn:
            report["preflight"] = "skip: MAT_INTENT_PERSONA_TEST_DSN is unset"
            names = ()
        else:
            os.environ["MAT_INTENT_TEST_DATABASE_URL"] = dsn
            names = ("tests.postgres.test_persona_store_postgres",
                     "tests.postgres.test_engine_postgres")
            report["preflight"] = "configured disposable PostgreSQL test database"
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromNames(names) if names else unittest.TestSuite()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    report["regression"] = {"tests": result.testsRun, "failures": len(result.failures),
                            "errors": len(result.errors), "skipped": len(result.skipped)}
    report["status"] = "failed" if result.failures or result.errors or report["preflight"] == "fail" else "incomplete"
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
