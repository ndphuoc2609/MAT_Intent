"""Immutable persona facts and release bindings, isolated from intent state."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

SQLITE_MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "sqlite" / "005_persona.sql"
SQLITE_MIGRATIONS = (
    (5, SQLITE_MIGRATION),
    (6, SQLITE_MIGRATION.with_name("006_persona_integrity.sql")),
    (7, SQLITE_MIGRATION.with_name("007_lead_drafts.sql")),
    (8, SQLITE_MIGRATION.with_name("008_peer_conversation_state.sql")),
    (9, SQLITE_MIGRATION.with_name("009_persona_generation.sql")),
    (10, SQLITE_MIGRATION.with_name("010_generated_facts.sql")),
    (11, SQLITE_MIGRATION.with_name("011_persona_memory_reservations.sql")),
)
SOURCE_KINDS = {"fictional_fixture", "user_provided", "official_source", "document", "other"}


class _MemoryReservationConflict(Exception):
    pass
TELLABILITY = {"public", "ask_only", "never"}
MEMORY_PRIVACY = {"public", "private", "sensitive"}
MEMORY_APPROVALS = {"draft", "approved", "fixture", "rejected"}
VOICE_GUIDE_KEYS = {
    "approval", "privacy", "consent_ref", "provenance", "data_origin", "tone",
    "habits", "speaker_habits", "reactions", "interests", "boundaries",
    "few_shots", "human_approved_example_target",
}
VOICE_EXAMPLE_KEYS = {
    "example_id", "topic", "customer_text", "reply_text", "fact_ids",
    "approval", "privacy", "consent_ref", "provenance", "source_kind", "source_ref",
}


def _id(value=None):
    result = str(uuid.UUID(str(value))) if value else str(uuid.uuid4())
    if uuid.UUID(result).version != 4:
        raise ValueError("IDs must be UUIDv4")
    return result


def migrate_sqlite(conn: sqlite3.Connection):
    """Apply the additive, versioned persona schema to an explicitly supplied DB."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version, path in SQLITE_MIGRATIONS:
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
            continue
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + path.read_text(encoding="utf-8") +
                               f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ({version}, CURRENT_TIMESTAMP);\nCOMMIT;")
        except Exception:
            conn.rollback()
            raise


class PersonaStore:
    """Repository over the engine SQLite connection or PostgreSQL state adapter."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn.conn if hasattr(conn, "conn") else conn
        self.sqlite = isinstance(self.conn, sqlite3.Connection)
        if self.sqlite:
            self.conn.row_factory = sqlite3.Row

    @staticmethod
    def _dict_row(cursor, row):
        if isinstance(row, sqlite3.Row):
            return dict(row)
        names = [col[0] if isinstance(col, tuple) else col.name for col in cursor.description]
        return dict(zip(names, row))

    @contextmanager
    def _atomic(self):
        if not self.sqlite:
            with self.conn.raw.transaction():
                yield
            return
        nested = self.conn.in_transaction
        savepoint = "persona_" + uuid.uuid4().hex
        self.conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}" if nested else "COMMIT")
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}" if nested else "ROLLBACK")
            if nested:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _has_memory_schema(self):
        if self.sqlite:
            return bool(self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona_memories'").fetchone())
        return bool(self.conn.execute("SELECT to_regclass('persona_memories')").fetchone()[0])

    def create_persona(self, slug, display_name, *, source_kind, persona_id=None):
        if source_kind not in SOURCE_KINDS or not slug.strip() or not display_name.strip():
            raise ValueError("persona requires a valid source_kind, slug and display_name")
        pid = _id(persona_id)
        with self._atomic():
            self.conn.execute(
                "INSERT INTO personas(persona_id,slug,display_name,source_kind) VALUES (?,?,?,?)",
                (pid, slug.strip(), display_name.strip(), source_kind))
            for identifier, kind in ((slug.strip(), "slug"), (display_name.strip(), "display")):
                self.conn.execute("INSERT INTO persona_identifiers(identifier_id,persona_id,identifier,kind) VALUES (?,?,?,?)",
                                  (str(uuid.uuid4()), pid, identifier, kind))
        return pid

    def list_personas(self):
        return [dict(id=row[0], slug=row[1], display_name=row[2], source_kind=row[3], status=row[4])
                for row in self.conn.execute(
                    "SELECT persona_id,slug,display_name,source_kind,status FROM personas ORDER BY slug")]

    def approve_persona(self, persona_id, approved_by):
        row = self.conn.execute("SELECT source_kind,status FROM personas WHERE persona_id=?", (persona_id,)).fetchone()
        if not row:
            raise ValueError("unknown persona")
        if row[0] == "fictional_fixture":
            raise ValueError("fictional fixture cannot be approved as a production persona")
        if not approved_by.strip():
            raise ValueError("approval requires reviewer identity")
        self.conn.execute("UPDATE personas SET status='APPROVED' WHERE persona_id=?", (persona_id,))
        self.conn.commit()

    def add_identifier(self, persona_id, identifier, *, kind="alias", scope="persona"):
        if kind not in {"slug", "display", "alias", "dealer_ref"} or not identifier.strip():
            raise ValueError("invalid persona identifier")
        iid = str(uuid.uuid4())
        self.conn.execute("INSERT INTO persona_identifiers(identifier_id,persona_id,identifier,kind,scope) VALUES (?,?,?,?,?)",
                          (iid, persona_id, identifier.strip(), kind, scope))
        self.conn.commit()
        return iid

    def register_key(self, category, key, *, value_type, unit=None, cardinality="singleton",
                     entity_kind=None, temporal_mode="current", permitted_templates=()):
        if value_type not in {"text", "integer", "number", "boolean", "json"}:
            raise ValueError("unsupported key value_type")
        if cardinality not in {"singleton", "multi"}:
            raise ValueError("cardinality must be singleton or multi")
        self.conn.execute(
            "INSERT INTO persona_key_registry(category,canonical_key,value_type,unit,cardinality,entity_kind,temporal_mode,permitted_templates) VALUES (?,?,?,?,?,?,?,?)",
            (category, key, value_type, unit, cardinality, entity_kind, temporal_mode,
             self._json(list(permitted_templates))))
        self.conn.commit()

    def add_fact(self, persona_id, category, key, value, *, source_ref, source_kind,
                 tellable="never", entity_id=None, unit=None, valid_from=None, valid_to=None,
                 as_of=None, precision=None, fact_id=None):
        if source_kind not in SOURCE_KINDS or tellable not in TELLABILITY or not source_ref:
            raise ValueError("fact needs valid source provenance and tellability")
        registry = self.conn.execute(
            "SELECT value_type, cardinality FROM persona_key_registry WHERE category=? AND canonical_key=?",
            (category, key)).fetchone()
        if not registry:
            raise ValueError("unregistered persona key")
        value_type, cardinality = registry[0], registry[1]
        checks = {"text": lambda v: isinstance(v, str),
                  "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                  "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
                  "boolean": lambda v: isinstance(v, bool), "json": lambda v: True}
        if not checks[value_type](value):
            raise ValueError(f"fact value must be {value_type}")
        if cardinality == "multi" and not entity_id:
            raise ValueError("multi fact requires entity_id")
        dates = {}
        for name, item in (("valid_from", valid_from), ("valid_to", valid_to), ("as_of", as_of)):
            try:
                dates[name] = date.fromisoformat(item) if item else None
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be an ISO date") from None
        if dates["valid_from"] and dates["valid_to"] and dates["valid_from"] >= dates["valid_to"]:
            raise ValueError("valid_from must be before valid_to")
        fact = _id(fact_id)
        if value_type == "text": vals = (value, None, None, None, None)
        elif value_type == "integer": vals = (None, value, None, None, None)
        elif value_type == "number": vals = (None, None, value, None, None)
        elif value_type == "boolean": vals = (None, None, None, int(value), None)
        else: vals = (None, None, None, None, self._json(value))
        self.conn.execute(
            "INSERT INTO persona_facts(fact_id,persona_id,entity_id,category,canonical_key,value_type,value_text,value_integer,value_number,value_boolean,value_json,unit,valid_from,valid_to,as_of,precision,tellable,source_ref,source_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fact, persona_id, entity_id, category, key, value_type, *vals, unit,
             valid_from, valid_to, as_of, precision, tellable, source_ref, source_kind))
        self.conn.commit()
        return fact

    def approve_fact(self, fact_id, approved_by, *, approved=True):
        if not approved_by.strip():
            raise ValueError("approval requires reviewer identity")
        source = self.conn.execute("SELECT p.source_kind FROM persona_facts f JOIN personas p ON p.persona_id=f.persona_id WHERE f.fact_id=?",
                                   (fact_id,)).fetchone()
        if not source:
            raise ValueError("unknown fact")
        decision = ("fixture" if source[0] == "fictional_fixture" else "approved") if approved else "rejected"
        approval_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO persona_fact_approvals(approval_id,fact_id,decision,approved_by) VALUES (?,?,?,?)",
            (approval_id, fact_id, decision, approved_by))
        self.conn.commit()
        return approval_id

    def create_release(self, persona_id, *, release_id=None, style=None):
        rid = _id(release_id)
        self.conn.execute("INSERT INTO persona_releases(release_id,persona_id,style) VALUES (?,?,?)",
                          (rid, persona_id, self._json(style or {})))
        self.conn.commit()
        return rid

    def update_release(self, release_id, *, style):
        cur = self.conn.execute("UPDATE persona_releases SET style=? WHERE release_id=? AND status='DRAFT'",
                                (self._json(style), release_id))
        if cur.rowcount == 0:
            if self.conn.execute("SELECT 1 FROM persona_releases WHERE release_id=?", (release_id,)).fetchone():
                raise ValueError("published release is immutable")
            raise ValueError("unknown release")
        self.conn.commit()

    def add_release_fact(self, release_id, fact_id):
        release = self.conn.execute("SELECT persona_id,status FROM persona_releases WHERE release_id=?",
                                    (release_id,)).fetchone()
        if not release:
            raise ValueError("unknown release")
        if release[1] != "DRAFT":
            raise ValueError("published release is immutable")
        approval = self.conn.execute(
            "SELECT approval_id,decision FROM persona_fact_approvals WHERE fact_id=? ORDER BY seq DESC LIMIT 1",
            (fact_id,)).fetchone()
        if not approval or approval[1] not in {"approved", "fixture"}:
            raise ValueError("release membership requires an approved or fixture fact")
        self.conn.execute("INSERT INTO release_facts(release_id,persona_id,fact_id,approval_id) VALUES (?,?,?,?)",
                          (release_id, release[0], fact_id, approval[0]))
        self.conn.commit()

    def add_utterance(self, release_id, body, *, purpose, required_fact_ids=(), topic=None,
                      canonical_key=None, claims=(), style="neutral", hook=False, approved=False,
                      approval=None,
                      utterance_id=None):
        if len(body) > 600:
            raise ValueError("utterance exceeds 600 characters")
        uid = _id(utterance_id)
        if approval is None:
            source = self.conn.execute("SELECT p.source_kind FROM persona_releases r JOIN personas p ON p.persona_id=r.persona_id WHERE r.release_id=?", (release_id,)).fetchone()
            approval = ("fixture" if source and source[0] == "fictional_fixture" else "approved") if approved else "draft"
        if approval not in {"draft", "approved", "fixture", "rejected"}:
            raise ValueError("invalid utterance approval")
        self.conn.execute(
            "INSERT INTO persona_utterances(utterance_id,release_id,body,purpose,topic,canonical_key,required_fact_ids,claims,style,hook_flag,approval) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, release_id, body, purpose, topic, canonical_key, self._json(list(required_fact_ids)),
             self._json(list(claims)), style, int(hook), approval))
        self.conn.commit()
        return uid

    def add_hook(self, release_id, body, *, purpose, required_fact_ids=(), max_per_conversation=1,
                 claims=(), approved=False, approval=None, hook_id=None):
        if len(body) > 600:
            raise ValueError("hook exceeds 600 characters")
        hid = _id(hook_id)
        if approval is None:
            source = self.conn.execute("SELECT p.source_kind FROM persona_releases r JOIN personas p ON p.persona_id=r.persona_id WHERE r.release_id=?", (release_id,)).fetchone()
            approval = ("fixture" if source and source[0] == "fictional_fixture" else "approved") if approved else "draft"
        if approval not in {"draft", "approved", "fixture", "rejected"}:
            raise ValueError("invalid hook approval")
        self.conn.execute(
            "INSERT INTO persona_hooks(hook_id,release_id,text,purpose,required_fact_ids,claims,max_per_conversation,approval) VALUES (?,?,?,?,?,?,?,?)",
            (hid, release_id, body, purpose, self._json(list(required_fact_ids)), self._json(list(claims)), max_per_conversation,
             approval))
        self.conn.commit()
        return hid

    @staticmethod
    def _memory_digest(memory):
        fields = ("memory_id", "persona_uuid", "release_id", "topic", "occurred_at", "precision",
                  "source_ref", "source_kind", "approval", "privacy", "tellable", "fact_ids",
                  "approved_summary", "consent_ref")
        content = {key: memory[key] for key in fields}
        return hashlib.sha256(PersonaStore._json(content).encode()).hexdigest()

    def add_memory(self, release_id, *, topic, occurred_at=None, precision, source_ref, source_kind,
                   approval, privacy, tellable, fact_ids=(), approved_summary, consent_ref=None,
                   memory_id=None):
        """Add one fact-supported memory to a draft release."""
        if not self._has_memory_schema():
            raise ValueError("persona memory schema is not migrated; apply migration 9")
        if (not isinstance(topic, str) or not topic.strip() or
                not isinstance(precision, str) or not precision.strip() or
                not isinstance(source_ref, str) or not source_ref.strip() or
                not isinstance(approved_summary, str) or not approved_summary.strip()):
            raise ValueError("memory requires topic, precision, source_ref and approved_summary")
        from .persona_query import KEY_REGISTRY
        if topic not in KEY_REGISTRY or topic in {"none", "unknown"}:
            raise ValueError("memory topic must match a registered persona topic")
        if source_kind not in SOURCE_KINDS or approval not in MEMORY_APPROVALS:
            raise ValueError("invalid memory source_kind or approval")
        if privacy not in MEMORY_PRIVACY or tellable not in TELLABILITY:
            raise ValueError("invalid memory privacy or tellable value")
        if occurred_at is not None:
            if not isinstance(occurred_at, str):
                raise ValueError("occurred_at must be an ISO date or datetime")
            try:
                datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("occurred_at must be an ISO date or datetime") from None
        if not isinstance(fact_ids, (list, tuple)) or not fact_ids:
            raise ValueError("memory must cite at least one release fact")
        normalized_fact_ids = [_id(fact_id) for fact_id in fact_ids]
        if len(set(normalized_fact_ids)) != len(normalized_fact_ids):
            raise ValueError("memory fact_ids must be unique")

        release = self.conn.execute(
            "SELECT r.persona_id,r.status,p.source_kind,p.status FROM persona_releases r "
            "JOIN personas p ON p.persona_id=r.persona_id WHERE r.release_id=?", (release_id,)
        ).fetchone()
        if not release:
            raise ValueError("unknown release")
        if release[1] != "DRAFT":
            raise ValueError("published release is immutable")
        persona_id, persona_source, persona_status = release[0], release[2], release[3]
        fixture = persona_source == "fictional_fixture"
        if fixture:
            if source_kind != "fictional_fixture" or approval != "fixture":
                raise ValueError("fictional fixture memories must remain fixture-labeled")
        else:
            if source_kind == "fictional_fixture" or approval == "fixture":
                raise ValueError("fictional fixture memory cannot enter a production release")
            if approval == "approved":
                if persona_status != "APPROVED":
                    raise ValueError("approved memory requires an approved persona")
                if not isinstance(consent_ref, str) or not consent_ref.strip():
                    raise ValueError("approved production memory requires consent_ref")

        fact_rows = self.conn.execute(
            "SELECT f.fact_id,f.tellable,a.decision FROM release_facts rf "
            "JOIN persona_facts f ON f.fact_id=rf.fact_id "
            "JOIN persona_fact_approvals a ON a.approval_id=rf.approval_id "
            "WHERE rf.persona_id=? AND rf.release_id=?", (persona_id, release_id)
        ).fetchall()
        facts = {row[0]: row for row in fact_rows}
        expected_approval = "fixture" if fixture else "approved"
        if any(fid not in facts or facts[fid][2] != expected_approval for fid in normalized_fact_ids):
            raise ValueError("memory facts must be approved facts in the same release")
        if approval == "approved" and privacy == "public" and tellable == "public":
            if any(facts[fid][1] != "public" for fid in normalized_fact_ids):
                raise ValueError("public memory requires public supporting facts")
        if any(self.conn.execute("SELECT 1 FROM persona_revocations WHERE fact_id=?", (fid,)).fetchone()
               for fid in normalized_fact_ids):
            raise ValueError("memory cannot cite a revoked fact")

        memory = {
            "memory_id": _id(memory_id), "persona_uuid": persona_id, "release_id": release_id,
            "topic": topic.strip(), "occurred_at": occurred_at, "precision": precision.strip(),
            "source_ref": source_ref.strip(), "source_kind": source_kind, "approval": approval,
            "privacy": privacy, "tellable": tellable, "fact_ids": normalized_fact_ids,
            "approved_summary": approved_summary.strip(),
            "consent_ref": consent_ref.strip() if isinstance(consent_ref, str) and consent_ref.strip() else None,
        }
        memory["checksum"] = self._memory_digest(memory)
        self.conn.execute(
            "INSERT INTO persona_memories(memory_id,persona_id,release_id,topic,occurred_at,precision,source_ref,source_kind,approval,privacy,tellable,fact_ids,approved_summary,consent_ref,checksum) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (memory["memory_id"], memory["persona_uuid"], release_id, memory["topic"], occurred_at,
             memory["precision"], memory["source_ref"], source_kind, approval, privacy, tellable,
             self._json(normalized_fact_ids), memory["approved_summary"], memory["consent_ref"], memory["checksum"]))
        self.conn.commit()
        return memory["memory_id"]

    def publish_release(self, release_id, approved_by):
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise ValueError("release approval requires reviewer identity")
        approved_by = approved_by.strip()
        with self._atomic():
            release = self.conn.execute("SELECT persona_id,status,style FROM persona_releases WHERE release_id=?",
                                        (release_id,)).fetchone()
            if not release:
                raise ValueError("unknown release")
            if release[1] != "DRAFT":
                raise ValueError("release already published")
            invalid = self.conn.execute(
                "SELECT 1 FROM release_facts rf LEFT JOIN persona_fact_approvals a "
                "ON a.approval_id=rf.approval_id AND a.fact_id=rf.fact_id "
                "WHERE rf.release_id=? AND (a.approval_id IS NULL OR a.seq <> "
                "(SELECT MAX(x.seq) FROM persona_fact_approvals x WHERE x.fact_id=rf.fact_id)) LIMIT 1",
                (release_id,)).fetchone()
            if invalid:
                raise ValueError("release fact requires its latest matching approval")
            fact_rows = self.conn.execute(
                "SELECT f.fact_id,f.category,f.canonical_key,f.entity_id,f.valid_from,f.valid_to,f.tellable,a.decision,k.cardinality "
                "FROM release_facts rf JOIN persona_facts f ON f.fact_id=rf.fact_id "
                "JOIN persona_key_registry k ON k.category=f.category AND k.canonical_key=f.canonical_key "
                "JOIN persona_fact_approvals a ON a.approval_id=rf.approval_id "
                "WHERE rf.release_id=? ORDER BY f.fact_id", (release_id,)).fetchall()
            persona = self.conn.execute("SELECT source_kind,status FROM personas WHERE persona_id=?", (release[0],)).fetchone()
            fixture_only = persona[0] == "fictional_fixture"
            expected_approval = "fixture" if fixture_only else "approved"
            for row in fact_rows:
                if row[7] != expected_approval:
                    raise ValueError(f"fact {row[0]} is not {expected_approval}")
            self._check_entity_overlap(fact_rows)
            included = {row[0] for row in fact_rows}
            fact_sources = {r[0]: r[1] for r in self.conn.execute(
                "SELECT f.fact_id,f.source_ref FROM release_facts rf JOIN persona_facts f ON f.fact_id=rf.fact_id WHERE rf.release_id=?",
                (release_id,))}
            for table, keycol, bodycol in (("persona_utterances", "utterance_id", "body"), ("persona_hooks", "hook_id", "text")):
                for row in self.conn.execute(f"SELECT {keycol},{bodycol},required_fact_ids,claims,approval FROM {table} WHERE release_id=?", (release_id,)):
                    if row[4] != expected_approval:
                        raise ValueError(f"{table} item {row[0]} is not {expected_approval}")
                    self._validate_claims(row[1], json.loads(row[2]), json.loads(row[3]), included, fact_sources)
            if persona[0] != "fictional_fixture" and persona[1] != "APPROVED":
                raise ValueError("persona must be approved before release publication")
            fact_details = {row[0]: row for row in fact_rows}
            memory_rows = self.conn.execute(
                    "SELECT memory_id,source_kind,approval,privacy,tellable,fact_ids,consent_ref "
                    "FROM persona_memories WHERE release_id=?", (release_id,)).fetchall() if self._has_memory_schema() else []
            for row in memory_rows:
                if row[2] != expected_approval:
                    raise ValueError(f"memory {row[0]} is not {expected_approval}")
                memory_fact_ids = json.loads(row[5])
                if not memory_fact_ids or not set(memory_fact_ids).issubset(included):
                    raise ValueError(f"memory {row[0]} requires facts from the same release")
                if row[1] == "fictional_fixture" and not fixture_only:
                    raise ValueError(f"memory {row[0]} is fixture-only")
                if not fixture_only and (not row[6] or not row[6].strip()):
                    raise ValueError(f"approved memory {row[0]} requires consent_ref")
                if row[3] == "public" and row[4] == "public" and any(
                        fact_details[fid][6] != "public" for fid in memory_fact_ids):
                    raise ValueError(f"public memory {row[0]} requires public supporting facts")
            public_fact_ids = {row[0] for row in fact_rows if row[6] == "public"}
            self._validate_voice_guide(json.loads(release[2] or "{}"), expected_approval,
                                       public_fact_ids)
            checksum = self._checksum(release_id)
            status = "FIXTURE" if fixture_only else "PUBLISHED"
            self.conn.execute("UPDATE persona_releases SET status=?,checksum=?,approved_by=?,published_at=CURRENT_TIMESTAMP WHERE release_id=? AND status='DRAFT'",
                              (status, checksum, approved_by, release_id))

    @staticmethod
    def _validate_voice_guide(style, expected_approval, public_fact_ids):
        if not isinstance(style, dict):
            raise ValueError("release style must be an object")
        if "voice_guide" not in style:
            return
        guide = style["voice_guide"]
        if not isinstance(guide, dict):
            raise ValueError("voice guide must be an object")
        unsupported = set(guide) - VOICE_GUIDE_KEYS
        if unsupported:
            raise ValueError("voice guide contains unsupported fields")
        if (guide.get("approval") != expected_approval or guide.get("privacy") != "public" or
                expected_approval == "approved" and
                (not isinstance(guide.get("consent_ref"), str) or not guide["consent_ref"].strip())):
            requirement = ("approval=approved, privacy=public and consent_ref" if expected_approval == "approved"
                           else "approval=fixture and privacy=public")
            raise ValueError(f"voice guide requires {requirement}")
        if expected_approval == "fixture" and guide.get("consent_ref") not in (None, ""):
            raise ValueError("fixture voice guide cannot claim production consent")
        if "provenance" in guide and (not isinstance(guide["provenance"], str) or
                                        not guide["provenance"].strip()):
            raise ValueError("voice guide provenance must be nonempty text")
        if "data_origin" in guide and (not isinstance(guide["data_origin"], str) or
                                         not guide["data_origin"].strip()):
            raise ValueError("voice guide data_origin must be nonempty text")
        if "consent_ref" in guide and guide["consent_ref"] is not None and not isinstance(guide["consent_ref"], str):
            raise ValueError("voice guide consent_ref must be text or null")
        if "tone" in guide and (not isinstance(guide["tone"], str) or len(guide["tone"]) > 200):
            raise ValueError("voice guide tone must be short text")
        for key in ("habits", "speaker_habits", "reactions", "interests", "boundaries"):
            value = guide.get(key)
            if value is not None and (not isinstance(value, list) or len(value) > 50 or
                    any(not isinstance(item, str) or not item.strip() or len(item) > 600 for item in value)):
                raise ValueError(f"voice guide {key} must be a list of short text")
        target = guide.get("human_approved_example_target")
        if target is not None and (not isinstance(target, dict) or
                set(target) != {"minimum", "maximum", "collected", "status"} or
                any(type(target.get(key)) is not int or target[key] < 0
                    for key in ("minimum", "maximum", "collected")) or
                not isinstance(target.get("status"), str) or not target["status"].strip()):
            raise ValueError("voice guide human_approved_example_target is invalid")

        examples = guide.get("few_shots")
        if not isinstance(examples, list) or not 10 <= len(examples) <= 20:
            raise ValueError("voice guide requires 10 to 20 few_shots")
        seen_ids = set()
        for example in examples:
            if not isinstance(example, dict):
                raise ValueError("voice example must be an object")
            if set(example) - VOICE_EXAMPLE_KEYS:
                raise ValueError("voice example contains unsupported fields")
            if not all(isinstance(example.get(key), str) and example[key].strip()
                       for key in ("example_id", "topic", "customer_text", "reply_text")):
                raise ValueError("voice example requires id, topic, customer_text and reply_text")
            if (example["example_id"] in seen_ids or len(example["example_id"]) > 200 or
                    len(example["topic"]) > 200 or len(example["customer_text"]) > 20_000 or
                    len(example["reply_text"]) > 600):
                raise ValueError("voice example fields are duplicated or too long")
            seen_ids.add(example["example_id"])
            if (example.get("approval") != expected_approval or example.get("privacy") != "public" or
                    expected_approval == "approved" and
                    (not isinstance(example.get("consent_ref"), str) or not example["consent_ref"].strip())):
                requirement = ("approval=approved, privacy=public and consent_ref" if expected_approval == "approved"
                               else "approval=fixture and privacy=public")
                raise ValueError(f"voice example requires {requirement}")
            if expected_approval == "fixture" and example.get("consent_ref") not in (None, ""):
                raise ValueError("fixture voice example cannot claim production consent")
            if "provenance" in example and (not isinstance(example["provenance"], str) or
                                             not example["provenance"].strip()):
                raise ValueError("voice example provenance must be nonempty text")
            if "source_ref" in example and (not isinstance(example["source_ref"], str) or
                                              not example["source_ref"].strip()):
                raise ValueError("voice example source_ref must be nonempty text")
            if "consent_ref" in example and example["consent_ref"] is not None and not isinstance(example["consent_ref"], str):
                raise ValueError("voice example consent_ref must be text or null")
            if "source_kind" in example and (not isinstance(example["source_kind"], str) or
                                               example["source_kind"] not in SOURCE_KINDS):
                raise ValueError("voice example source_kind is invalid")
            if expected_approval == "approved" and example.get("source_kind") == "fictional_fixture":
                raise ValueError("fixture voice example cannot enter a production release")
            fact_ids = example.get("fact_ids")
            if not isinstance(fact_ids, list) or len(fact_ids) > 30 or \
                    any(not isinstance(fact_id, str) or not fact_id for fact_id in fact_ids) or \
                    len(fact_ids) != len(set(fact_ids)):
                raise ValueError("voice example fact_ids must be a unique list")
            if not set(fact_ids).issubset(public_fact_ids):
                raise ValueError("voice example references a fact outside the public release")

    @staticmethod
    def _validate_claims(body, required_fact_ids, claims, included, fact_sources):
        if not isinstance(claims, list):
            raise ValueError("claim evidence must be a list")
        required = set(required_fact_ids)
        cited = set()
        for claim in claims:
            if (not isinstance(claim, dict) or set(claim) != {"text", "fact_id", "source_ref"} or
                    not isinstance(claim["text"], str) or not claim["text"].strip() or
                    not isinstance(claim["fact_id"], str) or not isinstance(claim["source_ref"], str)):
                raise ValueError("claim evidence must contain text, fact_id and source_ref")
            if claim["text"] not in body:
                raise ValueError("claim text must be present in the utterance")
            fid = claim["fact_id"]
            if fid not in included:
                raise ValueError("claim references a fact outside its release")
            if claim["source_ref"] != fact_sources.get(fid):
                raise ValueError("claim source_ref does not match fact")
            cited.add(fid)
        if cited != required:
            raise ValueError("claim evidence does not match required facts")

    @staticmethod
    def _check_entity_overlap(facts):
        groups = {}
        for fact in facts:
            fid, category, key, entity, start, end = fact[:6]
            groups.setdefault((category, key, entity), []).append((fid, start, end))
        for key, group in groups.items():
            for index, (fid, start, end) in enumerate(group):
                for other_id, other_start, other_end in group[index + 1:]:
                    starts_before_other_end = other_end is None or start is None or start < other_end
                    other_starts_before_end = end is None or other_start is None or other_start < end
                    if starts_before_other_end and other_starts_before_end:
                        raise ValueError(f"overlapping facts for {key}: {fid}, {other_id}")

    def bind_post(self, page_id, post_id, persona_id, release_id):
        with self._atomic():
            row = self.conn.execute("SELECT r.persona_id,r.status,p.source_kind FROM persona_releases r JOIN personas p ON p.persona_id=r.persona_id WHERE r.release_id=?",
                                    (release_id,)).fetchone()
            if (not row or row[0] != persona_id or
                    not (row[1] == "PUBLISHED" or (row[1] == "FIXTURE" and row[2] == "fictional_fixture"))):
                raise ValueError("binding requires an approved release or explicit fictional fixture")
            self.conn.execute("INSERT INTO post_persona_binding(page_id,post_id,persona_id,release_id) VALUES (?,?,?,?)",
                              (page_id, post_id, persona_id, release_id))

    def binding(self, page_id, post_id):
        cur = self.conn.execute("SELECT * FROM post_persona_binding WHERE page_id=? AND post_id=?",
                                (page_id, post_id))
        row = cur.fetchone()
        return self._dict_row(cur, row) if row else None

    def revoke_fact(self, fact_id, reason, revoked_by):
        if not reason.strip() or not revoked_by.strip():
            raise ValueError("revocation requires reason and actor")
        self.conn.execute("INSERT INTO persona_revocations(fact_id,reason,revoked_by) VALUES (?,?,?)",
                          (fact_id, reason, revoked_by))
        self.conn.commit()

    # -- details Luna invented during a conversation; never part of an approved release --
    def _has_generated_schema(self):
        if self.sqlite:
            return bool(self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona_generated_facts'").fetchone())
        return bool(self.conn.execute("SELECT to_regclass('persona_generated_facts')").fetchone()[0])

    _GEN_COLUMNS = ("gen_fact_id", "fact_key", "value_text", "precision", "source_ref",
                    "created_comment_id", "status")

    def generated_rows(self, persona_id, comment_id=None):
        """ACTIVE facts for everyone, plus PENDING ones created by this one comment."""
        rows = self.conn.execute(
            "SELECT gen_fact_id,fact_key,value_text,precision,source_ref,created_comment_id,status "
            "FROM persona_generated_facts WHERE persona_id=? AND "
            "(status='ACTIVE' OR (status='PENDING' AND created_comment_id=?)) "
            "ORDER BY created_at, gen_fact_id LIMIT 300", (persona_id, comment_id or "")).fetchall()
        return [dict(zip(self._GEN_COLUMNS, row)) for row in rows]

    def attach_generated(self, snapshot, comment_id=None):
        """Return the snapshot with generated facts appended; the stored release is untouched."""
        if not isinstance(snapshot, dict) or not self._has_generated_schema():
            return snapshot
        from .persona_fact_policy import as_snapshot_fact
        persona_id, release_id = snapshot["persona_uuid"], snapshot["release_id"]
        rows = self.generated_rows(persona_id, comment_id)
        if not rows:
            return snapshot
        return {**snapshot, "facts": list(snapshot["facts"]) +
                [as_snapshot_fact(row, persona_id, release_id) for row in rows]}

    def live_generated_keys(self, persona_id):
        return {row[0] for row in self.conn.execute(
            "SELECT fact_key FROM persona_generated_facts WHERE persona_id=? AND status IN ('PENDING','ACTIVE')",
            (persona_id,)).fetchall()}

    def generated_created_today(self, persona_id, day):
        return self.conn.execute(
            "SELECT COUNT(*) FROM persona_generated_facts WHERE persona_id=? AND created_at>=? AND status<>'DISCARDED'",
            (persona_id, day)).fetchone()[0]

    def purge_generated(self, persona_id, now_iso, ttl_minutes):
        """Drop PENDING rows nobody sent: they must not block a key forever."""
        now = datetime.fromisoformat(now_iso)
        with self._atomic():
            for gen_id, created in self.conn.execute(
                    "SELECT gen_fact_id,created_at FROM persona_generated_facts WHERE persona_id=? AND status='PENDING'",
                    (persona_id,)).fetchall():
                try:
                    age = (now - datetime.fromisoformat(created)).total_seconds() / 60
                except (TypeError, ValueError):
                    age = ttl_minutes + 1
                if age > ttl_minutes:
                    self.conn.execute("UPDATE persona_generated_facts SET status='DISCARDED',updated_at=? WHERE gen_fact_id=?",
                                      (now_iso, gen_id))

    def propose_generated(self, persona_id, key, value, precision, source_ref, comment_id, now_iso):
        if not self._has_generated_schema():
            raise ValueError("generated fact schema is not migrated; apply migration 10")
        gen_id = _id()
        with self._atomic():
            self.conn.execute(
                "INSERT INTO persona_generated_facts(gen_fact_id,persona_id,fact_key,value_text,precision,source_ref,"
                "created_comment_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'PENDING',?,?)",
                (gen_id, persona_id, key, value, precision, source_ref, comment_id, now_iso, now_iso))
        return gen_id

    def activate_generated(self, comment_id, ids, now_iso):
        for gen_id in ids or ():
            self.conn.execute(
                "UPDATE persona_generated_facts SET status='ACTIVE',updated_at=? "
                "WHERE gen_fact_id=? AND created_comment_id=? AND status='PENDING'", (now_iso, gen_id, comment_id))

    def discard_generated(self, comment_id, now_iso, keep_ids=()):
        keep = set(keep_ids or ())
        for (gen_id,) in self.conn.execute(
                "SELECT gen_fact_id FROM persona_generated_facts WHERE created_comment_id=? AND status='PENDING'",
                (comment_id,)).fetchall():
            if gen_id not in keep:
                self.conn.execute("UPDATE persona_generated_facts SET status='DISCARDED',updated_at=? WHERE gen_fact_id=?",
                                  (now_iso, gen_id))

    def revoke_generated(self, gen_fact_id, reason, revoked_by, now_iso=None):
        if not isinstance(reason, str) or not reason.strip() or not isinstance(revoked_by, str) or not revoked_by.strip():
            raise ValueError("revocation requires reason and actor")
        now_iso = now_iso or datetime.now().astimezone().isoformat()
        with self._atomic():
            cur = self.conn.execute(
                "UPDATE persona_generated_facts SET status='REVOKED',revoke_reason=?,revoked_by=?,updated_at=? "
                "WHERE gen_fact_id=? AND status IN ('PENDING','ACTIVE')", (reason.strip(), revoked_by.strip(), now_iso, gen_fact_id))
        return cur.rowcount == 1

    def list_generated(self, persona_id, limit=100):
        rows = self.conn.execute(
            "SELECT gen_fact_id,fact_key,value_text,precision,source_ref,created_comment_id,status,created_at "
            "FROM persona_generated_facts WHERE persona_id=? AND status IN ('PENDING','ACTIVE','REVOKED') "
            "ORDER BY created_at DESC LIMIT ?", (persona_id, limit)).fetchall()
        return [dict(zip(self._GEN_COLUMNS + ("created_at",), row)) for row in rows]

    def reserved_memory_ids(self, scope):
        return [row[0] for row in self.conn.execute(
            "SELECT memory_id FROM persona_memory_reservations WHERE status IN ('RESERVED','SENDING') "
            "AND page_id=? AND post_id=? "
            "AND thread_root_id=? AND actor_id=? ORDER BY memory_id",
            tuple(scope[key] for key in ("page_id", "post_id", "thread_root_id", "actor_id"))).fetchall()]

    def reserve_memory_delivery(self, scope, memory_ids, dedupe_key, turn_ordinal, now_iso):
        """Claim one story for an in-flight send; delivery history is written only on SENT."""
        memory_ids = sorted(set(memory_ids or ()))
        if not memory_ids:
            return True
        if len(memory_ids) != 1:
            return False
        memory_id = memory_ids[0]
        scope_values = tuple(scope[key] for key in ("page_id", "post_id", "thread_root_id", "actor_id"))
        try:
            with self._atomic():
                if self.conn.execute(
                        "SELECT 1 FROM persona_memory_revocations WHERE memory_id=? LIMIT 1",
                        (memory_id,)).fetchone():
                    raise _MemoryReservationConflict()
                delivered = self.conn.execute(
                    "SELECT MAX(turn_ordinal) FROM persona_memory_deliveries WHERE page_id=? AND post_id=? "
                    "AND thread_root_id=? AND actor_id=? AND memory_ids<>'[]'", scope_values).fetchone()[0]
                if delivered is not None and turn_ordinal - delivered < 5:
                    raise _MemoryReservationConflict()
                row = self.conn.execute(
                    "SELECT dedupe_key,memory_id FROM persona_memory_reservations WHERE page_id=? AND post_id=? "
                    "AND thread_root_id=? AND actor_id=?", scope_values).fetchone()
                if row:
                    if row[0] != dedupe_key or row[1] != memory_id:
                        raise _MemoryReservationConflict()
                    return True
                self.conn.execute(
                    "INSERT INTO persona_memory_reservations(dedupe_key,page_id,post_id,thread_root_id,"
                    "actor_id,memory_id,turn_ordinal,reserved_at,status) VALUES (?,?,?,?,?,?,?,?, 'RESERVED')",
                    (dedupe_key, *scope_values, memory_id, turn_ordinal, now_iso))
        except _MemoryReservationConflict:
            return False
        except Exception:
            # A concurrent worker can win the unique conversation reservation after our SELECT.
            row = self.conn.execute(
                "SELECT dedupe_key,memory_id FROM persona_memory_reservations WHERE page_id=? AND post_id=? "
                "AND thread_root_id=? AND actor_id=?", scope_values).fetchone()
            if row and (row[0] != dedupe_key or row[1] != memory_id):
                return False
            raise
        return True

    def start_memory_delivery(self, dedupe_key):
        """Linearize revocation against send: only RESERVED can transition to SENDING."""
        with self._atomic():
            row = self.conn.execute(
                "SELECT memory_id,status FROM persona_memory_reservations WHERE dedupe_key=?",
                (dedupe_key,)).fetchone()
            if not row:
                return False
            if row[1] == "SENDING":
                return True
            if self.conn.execute(
                    "SELECT 1 FROM persona_memory_revocations WHERE memory_id=? LIMIT 1", (row[0],)).fetchone():
                return False
            cur = self.conn.execute(
                "UPDATE persona_memory_reservations SET status='SENDING' WHERE dedupe_key=? AND status='RESERVED'",
                (dedupe_key,))
        return cur.rowcount == 1

    def release_memory_reservation(self, dedupe_key):
        return self.conn.execute("DELETE FROM persona_memory_reservations WHERE dedupe_key=?",
                                 (dedupe_key,)).rowcount

    def revoke_memory(self, memory_id, reviewer, reason):
        if not self._has_memory_schema():
            raise ValueError("persona memory schema is not migrated; apply migration 9")
        if not isinstance(reason, str) or not reason.strip() or not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("memory revocation requires reason and reviewer")
        if not self.conn.execute("SELECT 1 FROM persona_memories WHERE memory_id=?", (memory_id,)).fetchone():
            raise ValueError("unknown memory")
        with self._atomic():
            self.conn.execute(
                "INSERT INTO persona_memory_revocations(memory_id,reason,revoked_by) VALUES (?,?,?)",
                (memory_id, reason.strip(), reviewer.strip()))
            self.conn.execute(
                "DELETE FROM persona_memory_reservations WHERE memory_id=? AND status='RESERVED'", (memory_id,))
        return memory_id

    def revoked_memory_ids(self, persona_id, release_id):
        if not self._has_memory_schema():
            return []
        return [row[0] for row in self.conn.execute(
            "SELECT DISTINCT rv.memory_id FROM persona_memory_revocations rv "
            "JOIN persona_memories m ON m.memory_id=rv.memory_id "
            "WHERE m.persona_id=? AND m.release_id=? ORDER BY rv.memory_id", (persona_id, release_id))]

    def revocation_epoch(self):
        return self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM persona_revocations").fetchone()[0]

    def revoked_fact_ids(self, persona_id, release_id):
        return [r[0] for r in self.conn.execute(
            "SELECT rv.fact_id FROM persona_revocations rv JOIN release_facts rf ON rf.fact_id=rv.fact_id "
            "WHERE rf.persona_id=? AND rf.release_id=? ORDER BY rv.seq", (persona_id, release_id))]

    def tellable_facts(self, persona_id, release_id, *, on_date=None):
        on_date = on_date or date.today()
        cur = self.conn.execute(
            "SELECT f.* FROM release_facts rf JOIN persona_facts f ON f.fact_id=rf.fact_id "
            "JOIN persona_fact_approvals a ON a.approval_id=rf.approval_id "
            "LEFT JOIN persona_revocations rv ON rv.fact_id=f.fact_id "
            "WHERE rf.release_id=? AND rf.persona_id=? AND a.decision IN ('approved','fixture') AND f.tellable='public' AND rv.fact_id IS NULL "
            "AND (f.valid_from IS NULL OR f.valid_from<=?) AND (f.valid_to IS NULL OR ?<f.valid_to)",
            (release_id, persona_id, on_date.isoformat(), on_date.isoformat()))
        return [self._dict_row(cur, row) for row in cur.fetchall()]

    def _release_content(self, release_id):
        release = self.conn.execute("SELECT release_id,persona_id,style FROM persona_releases WHERE release_id=?",
                                    (release_id,)).fetchone()
        if not release:
            raise ValueError("unknown release")
        fact_cur = self.conn.execute(
            "SELECT f.fact_id,f.persona_id,f.entity_id,f.category,f.canonical_key,f.value_type,"
            "f.value_text,f.value_integer,f.value_number,f.value_boolean,f.value_json,f.unit,"
            "f.valid_from,f.valid_to,f.as_of,f.precision,f.tellable,f.source_ref,f.source_kind,a.decision,a.approved_by "
            "FROM release_facts rf JOIN persona_facts f ON f.fact_id=rf.fact_id "
            "JOIN persona_fact_approvals a ON a.approval_id=rf.approval_id "
            "WHERE rf.release_id=? ORDER BY f.fact_id", (release_id,))
        facts = [self._dict_row(fact_cur, row) for row in fact_cur.fetchall()]
        utterance_cur = self.conn.execute(
            "SELECT utterance_id,body,purpose,topic,canonical_key,required_fact_ids,claims,style,hook_flag,approval "
            "FROM persona_utterances WHERE release_id=? ORDER BY utterance_id", (release_id,))
        utterances = [self._dict_row(utterance_cur, row) for row in utterance_cur.fetchall()]
        hook_cur = self.conn.execute(
            "SELECT hook_id,text,purpose,required_fact_ids,claims,max_per_conversation,approval "
            "FROM persona_hooks WHERE release_id=? ORDER BY hook_id", (release_id,))
        hooks = [self._dict_row(hook_cur, row) for row in hook_cur.fetchall()]
        memories = []
        if self._has_memory_schema():
            memory_cur = self.conn.execute(
                "SELECT memory_id,persona_id,release_id,topic,occurred_at,precision,source_ref,source_kind,approval,privacy,tellable,fact_ids,approved_summary,consent_ref,checksum "
                "FROM persona_memories WHERE release_id=? ORDER BY memory_id", (release_id,))
            memories = [self._dict_row(memory_cur, row) for row in memory_cur.fetchall()]
        for memory in memories:
            memory["persona_uuid"] = memory.pop("persona_id")
            memory["fact_ids"] = json.loads(memory["fact_ids"])
        content = {"release_id": release[0], "persona_id": release[1], "style": json.loads(release[2]),
                   "facts": facts, "utterances": utterances, "hooks": hooks}
        if memories:
            content["memories"] = memories
        return content

    def _checksum(self, release_id):
        return hashlib.sha256(self._json(self._release_content(release_id)).encode()).hexdigest()

    def get_release_snapshot(self, persona_id, release_id=None, *, on_date=None, allowed_keys=()):
        if release_id is None:
            row = self.conn.execute(
                "SELECT release_id FROM persona_releases WHERE persona_id=? AND status IN ('PUBLISHED','FIXTURE') ORDER BY published_at DESC,release_id DESC LIMIT 1",
                (persona_id,)).fetchone()
            release_id = row[0] if row else None
        row = self.conn.execute(
            "SELECT r.status,r.checksum,r.approved_by,r.published_at,p.slug,p.display_name,p.source_kind,p.status "
            "FROM persona_releases r JOIN personas p ON p.persona_id=r.persona_id "
            "WHERE r.release_id=? AND r.persona_id=?", (release_id, persona_id)).fetchone()
        expected_status = "FIXTURE" if row and row[6] == "fictional_fixture" else "PUBLISHED"
        expected_approval = "fixture" if expected_status == "FIXTURE" else "approved"
        if not row or row[0] != expected_status or not row[1] or row[1] != self._checksum(release_id):
            raise ValueError("release is missing, unpublished, or checksum verification failed")
        if on_date is None: on_date = date.today()
        elif isinstance(on_date, str): on_date = date.fromisoformat(on_date)
        include = set(allowed_keys)
        payload = self._release_content(release_id)
        facts = []
        for fact in payload["facts"]:
            if fact["decision"] != expected_approval or fact["tellable"] != "public":
                continue
            if include and fact["canonical_key"] not in include:
                continue
            if fact["valid_from"] and on_date.isoformat() < fact["valid_from"]:
                continue
            if fact["valid_to"] and on_date.isoformat() >= fact["valid_to"]:
                continue
            if self.conn.execute("SELECT 1 FROM persona_revocations WHERE fact_id=?", (fact["fact_id"],)).fetchone():
                continue
            if self.conn.execute(
                    "SELECT 1 FROM release_facts rf JOIN persona_fact_approvals original ON original.approval_id=rf.approval_id "
                    "JOIN persona_fact_approvals later ON later.fact_id=rf.fact_id AND later.seq>original.seq "
                    "WHERE rf.release_id=? AND rf.fact_id=? AND later.decision='rejected' LIMIT 1",
                    (release_id, fact["fact_id"])).fetchone():
                continue
            vtype = fact["value_type"]
            if vtype == "text": value = fact["value_text"]
            elif vtype == "integer": value = fact["value_integer"]
            elif vtype == "number": value = fact["value_number"]
            elif vtype == "boolean": value = bool(fact["value_boolean"])
            else: value = json.loads(fact["value_json"])
            facts.append({"fact_id": fact["fact_id"], "category": fact["category"],
                          "key": fact["canonical_key"], "entity_id": fact["entity_id"],
                          "value": value, "value_type": vtype, "unit": fact["unit"],
                          "valid_from": fact["valid_from"], "valid_to": fact["valid_to"],
                          "as_of": fact["as_of"], "precision": fact["precision"],
                          "tellable": fact["tellable"], "source_ref": fact["source_ref"],
                          "source_kind": fact["source_kind"], "approval": fact["decision"]})
        utterances = [{**u, "required_fact_ids": json.loads(u["required_fact_ids"]), "claims": json.loads(u["claims"])}
                      for u in payload["utterances"] if u["approval"] == expected_approval and
                      set(json.loads(u["required_fact_ids"])).issubset({f["fact_id"] for f in facts})]
        hooks = [{**h, "required_fact_ids": json.loads(h["required_fact_ids"]), "claims": json.loads(h["claims"])}
                 for h in payload["hooks"] if h["approval"] == expected_approval and
                 set(json.loads(h["required_fact_ids"])).issubset({f["fact_id"] for f in facts})]
        included = {f["fact_id"] for f in facts}
        revoked_memory_ids = self.revoked_memory_ids(persona_id, release_id)
        memories = []
        for memory in payload.get("memories", []):
            if memory["approval"] != expected_approval or memory["memory_id"] in revoked_memory_ids:
                continue
            if memory["checksum"] != self._memory_digest(memory):
                raise ValueError("memory checksum verification failed")
            if not set(memory["fact_ids"]).issubset(included):
                continue
            memory = dict(memory)
            memories.append(memory)
        return {"persona": {"id": persona_id, "slug": row[4], "display_name": row[5],
                            "source_kind": row[6], "status": row[7]},
                "release": {"id": release_id, "status": row[0], "checksum": row[1],
                            "approved": row[0] == "PUBLISHED", "fixture_only": row[0] == "FIXTURE",
                            "approved_by": row[2],
                            "published_at": row[3], "style": payload["style"]},
                "fixture_only": row[0] == "FIXTURE", "facts": facts,
                "utterances": utterances, "hooks": hooks, "memories": memories,
                "revocation_epoch": self.revocation_epoch(),
                "revoked_fact_ids": self.revoked_fact_ids(persona_id, release_id),
                "revoked_memory_ids": revoked_memory_ids}

    release_snapshot = get_release_snapshot

    def get_binding_snapshot(self, page_id, post_id):
        binding = self.binding(page_id, post_id)
        if not binding:
            return None
        snapshot = self.get_release_snapshot(binding["persona_id"], binding["release_id"])
        release_id = binding["release_id"]
        from .persona_reply import utterance_checksum
        utterances = []
        for row in snapshot["utterances"]:
            claims = [{"text": claim["text"], "required_fact_ids": [claim["fact_id"]],
                       "evidence": [{"fact_id": claim["fact_id"], "source_ref": claim["source_ref"]}]}
                      for claim in row["claims"]]
            utterance = {
                "utterance_id": row["utterance_id"], "persona_uuid": binding["persona_id"],
                "release_id": release_id, "text": row["body"], "purpose": row["purpose"],
                "topic": row["topic"], "key_conditions": [],
                "required_fact_ids": row["required_fact_ids"], "claims": claims,
                "style": row["style"], "hook": bool(row["hook_flag"]),
                "approval": row["approval"], "source_kind": snapshot["persona"]["source_kind"],
            }
            utterance["checksum"] = utterance_checksum(utterance)
            utterances.append(utterance)
        # Hooks share utterance validation and the release-bound dispatch check.
        for hook in snapshot['hooks']:
            item = dict(utterance_id=hook['hook_id'], persona_uuid=binding['persona_id'],
                release_id=release_id, text=hook['text'], purpose='answer_persona',
                topic=hook['purpose'], required_fact_ids=hook['required_fact_ids'],
                claims=hook['claims'], approval=hook['approval'],
                source_kind=snapshot['persona']['source_kind'], hook=True)
            item['checksum'] = utterance_checksum(item)
            utterances.append(item)
        return {
            **binding, "persona_uuid": binding["persona_id"], "release_id": release_id,
            "approved": snapshot["release"]["approved"],
            "source_kind": snapshot["persona"]["source_kind"],
            "fixture_only": snapshot["fixture_only"], "snapshot": snapshot,
            "memories": snapshot["memories"], "revoked_memory_ids": snapshot["revoked_memory_ids"],
            "facts": [{**f, "persona_uuid": binding["persona_id"], "release_id": release_id,
                       "canonical_key": f["key"]} for f in snapshot["facts"]],
            "utterances": utterances,
        }

    def record_root_analysis(self, page_id, post_id, thread_id, root_comment_id, actor_id,
                             semantic, reply_text, *, persona_snapshot, persona_id, release_id,
                             root_text="",
                             tags=(), model_version=None, schema_version=None, source_kind="sandbox"):
        return self.save_root_analysis(
            page_id=page_id, post_id=post_id, thread_root_id=thread_id,
            root_comment_id=root_comment_id, root_actor_id=actor_id, root_text=root_text,
            semantic=semantic, tags=tags,
            persona_id=persona_id, release_id=release_id, persona_snapshot=persona_snapshot,
            reply_text=reply_text, model_version=model_version, schema_version=schema_version,
            source_kind=source_kind)

    def root_analysis(self, page_id, post_id, thread_id):
        return self.get_root_analysis(page_id, post_id, thread_id)

    def save_root_analysis(self, *, page_id, post_id, thread_root_id, root_comment_id,
                           root_actor_id, root_text,
                           semantic, tags, persona_id, release_id, persona_snapshot,
                           reply_text=None, model_version=None, schema_version=None,
                           source_kind="sandbox"):
        values = (page_id, post_id, thread_root_id, root_comment_id, root_actor_id, root_text,
                  self._json(semantic), self._json(tags), persona_id, release_id,
                  self._json(persona_snapshot), reply_text, model_version, schema_version, source_kind)
        with self._atomic():
            existing = self.conn.execute(
                "SELECT page_id,post_id,thread_root_id,root_comment_id,root_actor_id,root_text,semantic,tags,persona_id,release_id,persona_snapshot,reply_text,model_version,schema_version,source_kind FROM persona_root_analyses WHERE page_id=? AND post_id=? AND thread_root_id=?",
                (page_id, post_id, thread_root_id)).fetchone()
            if existing and tuple(existing) != values:
                raise ValueError("root analysis already exists with different content")
            if not existing:
                self.conn.execute(
                    "INSERT INTO persona_root_analyses(page_id,post_id,thread_root_id,root_comment_id,root_actor_id,root_text,semantic,tags,persona_id,release_id,persona_snapshot,reply_text,model_version,schema_version,source_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values)
        return self.get_root_analysis(page_id, post_id, thread_root_id)

    def get_root_analysis(self, page_id, post_id, thread_root_id):
        cur = self.conn.execute(
            "SELECT * FROM persona_root_analyses WHERE page_id=? AND post_id=? AND thread_root_id=?",
            (page_id, post_id, thread_root_id))
        row = cur.fetchone()
        if not row: return None
        result = self._dict_row(cur, row)
        for key in ("semantic", "tags", "persona_snapshot"):
            result[key] = json.loads(result[key])
        return result

    def verify_release(self, release_id):
        row = self.conn.execute("SELECT status,checksum FROM persona_releases WHERE release_id=?", (release_id,)).fetchone()
        return bool(row and row[0] in {"PUBLISHED", "FIXTURE"} and row[1] == self._checksum(release_id))
