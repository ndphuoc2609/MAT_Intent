"""Small, explicitly fictional persona used only by the local sandbox."""
from __future__ import annotations


_REVIEWER = "sandbox-fixture-seeder"
_FACTS = (
    ("identity", "name", "Nam", "name", "Mình là Nam.", "Nam"),
    ("occupation", "title", "Kỹ sư cơ khí", "occupation", "Mình làm kỹ sư cơ khí.", "làm kỹ sư cơ khí"),
    ("location", "base.province", "Hà Nội", "residence", "Mình ở Hà Nội.", "ở Hà Nội"),
    ("vehicle", "current.model", "Hyundai Creta", "vehicle_current",
     "Mình đang đi Hyundai Creta.", "đang đi Hyundai Creta"),
)


def seed_nam_creta(store):
    """Create/resume the sandbox-only fixture without duplicating rows."""
    conn = store.conn
    persona = conn.execute("SELECT persona_id,source_kind FROM personas WHERE slug=?", ("nam_creta",)).fetchone()
    if persona and persona[1] != "fictional_fixture":
        raise ValueError("nam_creta slug belongs to a non-fixture persona")
    persona_id = persona[0] if persona else store.create_persona(
        "nam_creta", "Nam Creta · kịch bản hư cấu", source_kind="fictional_fixture")

    registry = {
        ("identity", "identity.name"): "text", ("occupation", "occupation.title"): "text",
        ("location", "location.base.province"): "text", ("vehicle", "vehicle.current.model"): "text",
    }
    for (category, key), value_type in registry.items():
        exists = conn.execute("SELECT 1 FROM persona_key_registry WHERE category=? AND canonical_key=?",
                              (category, key)).fetchone()
        if not exists:
            store.register_key(category, key, value_type=value_type,
                               permitted_templates=["answer_persona"])

    fact_ids = {}
    for category, suffix, value, topic, sentence, claim_text in _FACTS:
        key = "identity.name" if category == "identity" else \
              "occupation.title" if category == "occupation" else \
              "location.base.province" if category == "location" else "vehicle.current.model"
        source_ref = f"fictional_fixture:nam_creta:{key}:v1"
        existing = conn.execute(
            "SELECT fact_id,value_text,source_ref,source_kind FROM persona_facts "
            "WHERE persona_id=? AND category=? AND canonical_key=?",
            (persona_id, category, key)).fetchone()
        if existing:
            if (existing[1], existing[2], existing[3]) != (value, source_ref, "fictional_fixture"):
                raise ValueError(f"nam_creta fixture fact conflicts at {key}")
            fact_id = existing[0]
        else:
            fact_id = store.add_fact(persona_id, category, key, value, source_ref=source_ref,
                source_kind="fictional_fixture", tellable="public", precision="fixture")
        decision = conn.execute(
            "SELECT decision FROM persona_fact_approvals WHERE fact_id=? ORDER BY seq DESC LIMIT 1",
            (fact_id,)).fetchone()
        if not decision:
            store.approve_fact(fact_id, _REVIEWER)
        elif decision[0] != "fixture":
            raise ValueError(f"nam_creta fixture approval conflicts at {key}")
        fact_ids[topic] = (fact_id, source_ref)

    row = conn.execute("SELECT release_id,status FROM persona_releases WHERE persona_id=? ORDER BY created_at DESC LIMIT 1",
                       (persona_id,)).fetchone()
    if row and row[1] == "FIXTURE":
        return persona_id, row[0]
    if row and row[1] != "DRAFT":
        raise ValueError("nam_creta has a non-fixture release")
    release_id = row[0] if row else store.create_release(persona_id,
        style={"tone": "warm", "stance": "peer_user"})

    for fact_id, _ in fact_ids.values():
        exists = conn.execute("SELECT 1 FROM release_facts WHERE release_id=? AND fact_id=?",
                              (release_id, fact_id)).fetchone()
        if not exists:
            store.add_release_fact(release_id, fact_id)

    for topic, (fact_id, source_ref) in fact_ids.items():
        definition = next(item for item in _FACTS if item[3] == topic)
        sentence, claim_text = definition[4], definition[5]
        exists = conn.execute(
            "SELECT 1 FROM persona_utterances WHERE release_id=? AND topic=? AND body=?",
            (release_id, topic, sentence)).fetchone()
        if not exists:
            store.add_utterance(release_id, sentence, purpose="answer_persona", topic=topic,
                required_fact_ids=[fact_id], claims=[{"text": claim_text, "fact_id": fact_id,
                                                       "source_ref": source_ref}], approval="fixture")
    unknown = conn.execute(
        "SELECT 1 FROM persona_utterances WHERE release_id=? AND topic='unknown'",
        (release_id,)).fetchone()
    if not unknown:
        store.add_utterance(release_id, "Chỗ này mình không rõ.", purpose="answer_persona",
                            topic="unknown", approval="fixture")
    store.publish_release(release_id, _REVIEWER)
    return persona_id, release_id
