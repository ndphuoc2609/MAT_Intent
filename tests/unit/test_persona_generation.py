import hashlib
import json
import unittest

from app.persona_generation import (
    allowed_generation_facts,
    generation_digest,
    validate_generated_reply,
)


def fact(fact_id="f-car", key="vehicle.current.model", value="Hyundai Creta", **changes):
    row = {
        "fact_id": fact_id,
        "persona_uuid": "p1",
        "release_id": "r1",
        "category": key.split(".", 1)[0],
        "key": key,
        "value": value,
        "value_type": "text" if isinstance(value, str) else "integer",
        "unit": None,
        "valid_from": None,
        "valid_to": None,
        "as_of": None,
        "precision": "exact",
        "tellable": "public",
        "privacy": "public",
        "source_ref": f"approved:source:{fact_id}",
        "source_kind": "official_source",
        "approval": "approved",
    }
    row.update(changes)
    return row


def snapshot(facts=None, **changes):
    value = {
        "persona_uuid": "p1",
        "release_id": "r1",
        "source_kind": "official_source",
        "approved": True,
        "fixture_only": False,
        "release": {"id": "r1", "status": "PUBLISHED", "approved": True,
                    "fixture_only": False, "checksum": "release-checksum"},
        "facts": [fact()] if facts is None else facts,
        "memories": [],
    }
    value.update(changes)
    return value


def claim(text="Mình đang đi Hyundai Creta.", *, subject="persona",
          predicate="vehicle.current.model", fact_ids=("f-car",),
          memory_ids=(), message_ids=()):
    return {"text": text, "subject": subject, "predicate": predicate,
            "fact_ids": list(fact_ids), "memory_ids": list(memory_ids),
            "message_ids": list(message_ids)}


def candidate(text="Mình đang đi Hyundai Creta.", *, claims=None, fact_ids=None,
              memory_ids=None, asks_question=False, question=None):
    claims = [claim(text)] if claims is None else claims
    return {
        "reply_text": text,
        "fact_ids": sorted({value for item in claims for value in item["fact_ids"]})
        if fact_ids is None else fact_ids,
        "memory_ids": sorted({value for item in claims for value in item["memory_ids"]})
        if memory_ids is None else memory_ids,
        "claims": claims,
        "asks_question": asks_question,
        "question": question,
    }


def memory_checksum(memory):
    payload = {key: value for key, value in memory.items()
               if key not in {"checksum", "eligible_for_generation"}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PersonaGenerationTests(unittest.TestCase):
    def test_validate_generated_post_accepts_grounded_content_with_one_question(self):
        from app.persona_generation import validate_generated_post

        text = "Mình đang đi Hyundai Creta. Bạn hay đi xe gì?"
        post = {"post_text": text, "fact_ids": ["f-car"], "memory_ids": [],
                "claims": [claim("Mình đang đi Hyundai Creta.")], "asks_question": True,
                "question": {"kind": "conversation", "field": None, "text": "Bạn hay đi xe gì?"}}
        result = validate_generated_post(post, snapshot())
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["text"], text)

    def test_validate_generated_post_rejects_overlength_and_multiple_questions(self):
        from app.persona_generation import validate_generated_post

        too_long = {"post_text": "x" * 3001, "fact_ids": [], "memory_ids": [],
                    "claims": [], "asks_question": False, "question": None}
        self.assertEqual(validate_generated_post(too_long, snapshot())["reason"], "INVALID_CANDIDATE")

        text = "Mình đang đi Hyundai Creta. Bạn đi phố hay xa? Bạn đang ở đâu?"
        post = {"post_text": text, "fact_ids": ["f-car"], "memory_ids": [],
                "claims": [claim("Mình đang đi Hyundai Creta.")], "asks_question": True,
                "question": {"kind": "conversation", "field": None, "text": "Bạn đang ở đâu?"}}
        self.assertEqual(validate_generated_post(post, snapshot())["reason"], "REPLY_LIMIT_EXCEEDED")

    def test_validate_generated_post_rejects_unsupported_personal_detail_and_conflicting_claims(self):
        from app.persona_generation import validate_generated_post

        personal = "Mình đang đi Hyundai Creta, vợ mình cũng thích xe này."
        unsupported = {"post_text": personal, "fact_ids": ["f-car"], "memory_ids": [],
                       "claims": [claim(personal)], "asks_question": False, "question": None}
        self.assertEqual(validate_generated_post(unsupported, snapshot())["reason"],
                         "UNSUPPORTED_PERSONAL_DETAIL")

        first = fact("gen-dalat", "gen.trip.destination", "Đà Lạt",
                     source_kind="generated", approval="generated", generated=True)
        second = fact("gen-vungtau", "gen.trip.destination", "Vũng Tàu",
                      source_kind="generated", approval="generated", generated=True)
        text = "Mình đi Đà Lạt. Mình đi Vũng Tàu."
        post = {"post_text": text, "fact_ids": ["gen-dalat", "gen-vungtau"], "memory_ids": [],
                "claims": [claim("Mình đi Đà Lạt.", predicate="gen.trip", fact_ids=("gen-dalat",)),
                           claim("Mình đi Vũng Tàu.", predicate="gen.trip", fact_ids=("gen-vungtau",))],
                "asks_question": False, "question": None}
        self.assertEqual(validate_generated_post(post, snapshot([first, second]))["reason"],
                         "CONFLICTING_PERSONA_CLAIMS")

    def test_generated_fact_uses_its_own_provenance_for_real_personas(self):
        generated = fact("gen-trip", "gen.trip.destination", "Đà Lạt",
                         source_kind="generated", approval="generated", generated=True)
        real = snapshot([generated])
        text = "Cuối tuần mình đi Đà Lạt."
        result = validate_generated_reply(
            candidate(text, claims=[claim(text, predicate="gen.trip", fact_ids=("gen-trip",))]), real)
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(allowed_generation_facts(real), [generated])

        fixture = snapshot([generated], source_kind="fictional_fixture", approved=False,
                           fixture_only=True,
                           release={"id": "r1", "status": "FIXTURE", "approved": False,
                                    "fixture_only": True, "checksum": "fixture-checksum"})
        self.assertEqual(len(allowed_generation_facts(fixture, allow_fictional_fixture=True)), 1)
        self.assertEqual(validate_generated_reply(
            candidate(text, claims=[claim(text, predicate="gen.trip", fact_ids=("gen-trip",))]),
            fixture, allow_fictional_fixture=True)["status"], "OK")

    def test_generated_fact_requires_exact_persona_and_release_ids(self):
        generated = fact("gen-trip", "gen.trip.destination", "Đà Lạt",
                         source_kind="generated", approval="generated", generated=True)
        for changed in (dict(persona_uuid=None), dict(release_id=None), dict(persona_uuid="p2")):
            with self.subTest(changed=changed):
                self.assertEqual(allowed_generation_facts(snapshot([dict(generated, **changed)])), [])

    def test_generated_key_cannot_masquerade_as_an_approved_release_fact(self):
        masquerade = fact("gen-trip", "gen.trip.destination", "Đà Lạt")
        self.assertEqual(allowed_generation_facts(snapshot([masquerade])), [])

    def test_generated_fact_provenance_cannot_be_used_for_ordinary_fact_keys(self):
        masquerade = fact("gen-car", "vehicle.current.model", "Hyundai Creta",
                          source_kind="generated", approval="generated", generated=True)
        self.assertEqual(allowed_generation_facts(snapshot([masquerade])), [])

    def test_reply_conflicting_with_persona_authored_post_is_blocked(self):
        authored = fact("post-destination", "travel.weekend.destination", "Đà Lạt")
        reply_fact = fact("reply-destination", "travel.weekend.destination", "Vũng Tàu")
        text = "Cuối tuần mình đi Vũng Tàu."
        wrapped = snapshot([authored, reply_fact], persona_authored_post={
            "text": "Cuối tuần mình đã đi Đà Lạt.",
            "persona_uuid": "p1", "release_id": "r1", "fact_ids": ["post-destination"],
        })
        result = validate_generated_reply(
            candidate(text, claims=[claim(text, predicate="travel.weekend.destination",
                                          fact_ids=("reply-destination",))]), wrapped)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "PERSONA_POST_CONTRADICTION")

    def test_reply_cannot_replace_authored_trip_destination_with_another_generated_key(self):
        authored = fact("post-trip", "gen.trip.weekend_getaway", "Cuối tuần mình hay đi Đà Lạt",
                        source_kind="generated", approval="generated", generated=True)
        reply_fact = fact("reply-trip", "gen.trip.weekend_route", "Cuối tuần mình hay đi Vũng Tàu",
                          source_kind="generated", approval="generated", generated=True)
        text = "Cuối tuần mình hay đi Vũng Tàu."
        wrapped = snapshot([authored, reply_fact], persona_authored_post={
            "text": "Cuối tuần mình hay đi Đà Lạt.", "persona_uuid": "p1",
            "release_id": "r1", "fact_ids": ["post-trip"]})
        result = validate_generated_reply(candidate(text, claims=[claim(
            text, predicate="gen.trip", fact_ids=("reply-trip",))]), wrapped)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "PERSONA_POST_CONTRADICTION")

    def test_filters_to_public_approved_current_facts_bound_to_release(self):
        rows = [fact(),
                fact("private", "identity.age_years", 31, value_type="integer", unit="years",
                     privacy="private"),
                fact("draft", "occupation.title", "Kỹ sư", approval="draft"),
                fact("foreign", "location.base.province", "Đà Nẵng", persona_uuid="p2"),
                fact("secret", "identity.name", "Nam", tellable="never"),
                fact("revoked", "identity.name", "Nam")]
        allowed = allowed_generation_facts(snapshot(rows), revoked_ids={"revoked"})
        self.assertEqual([row["fact_id"] for row in allowed], ["f-car"])

    def test_excludes_undated_current_mileage_and_malformed_validity(self):
        rows = [fact("mileage", "vehicle.current.mileage_km", 28000,
                     value_type="integer", unit="km", precision="approximate_undated"),
                fact("bad-date", "vehicle.current.model", "Ford", valid_from="not-a-date")]
        self.assertEqual(allowed_generation_facts(snapshot(rows)), [])

    def test_fixture_facts_require_explicit_sandbox_opt_in_and_fixture_approval(self):
        fixture = fact(source_kind="fictional_fixture", approval="fixture")
        value = snapshot([fixture], source_kind="fictional_fixture", approved=False,
                         fixture_only=True,
                         release={"id": "r1", "status": "FIXTURE", "approved": False,
                                  "fixture_only": True, "checksum": "fixture-checksum"})
        self.assertEqual(allowed_generation_facts(value), [])
        self.assertEqual(len(allowed_generation_facts(value, allow_fictional_fixture=True)), 1)
        blocked = validate_generated_reply(candidate(), value)
        self.assertEqual(blocked["status"], "REVIEW")
        allowed = validate_generated_reply(candidate(), value, allow_fictional_fixture=True)
        self.assertEqual(allowed["status"], "OK")

    def test_accepts_grounded_paraphrase_and_emits_reproducible_manifest(self):
        text = "Mình đang sử dụng chiếc Creta."
        result = validate_generated_reply(
            candidate(text, claims=[claim(text)]), snapshot())
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["text"], text)
        self.assertEqual(result["fact_ids"], ["f-car"])
        manifest = result["manifest"]
        self.assertEqual(manifest["candidate_digest"], generation_digest(candidate(text, claims=[claim(text)])))
        self.assertEqual(manifest["facts"][0]["fact_id"], "f-car")
        self.assertEqual(manifest["facts"][0]["source_ref"], "approved:source:f-car")
        self.assertEqual(manifest["digest"], generation_digest({k: v for k, v in manifest.items() if k != "digest"}))

    def test_accepts_one_sentence_with_multiple_claims_and_claim_scoped_sources(self):
        occupation = fact("f-job", "occupation.title", "Kỹ sư cơ khí")
        text = "Mình đang đi Hyundai Creta và làm kỹ sư cơ khí."
        claims = [
            claim(text, predicate="vehicle.current.model", fact_ids=("f-car",)),
            claim(text, predicate="occupation.title", fact_ids=("f-job",)),
        ]
        result = validate_generated_reply(candidate(text, claims=claims), snapshot([fact(), occupation]))
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["fact_ids"], ["f-car", "f-job"])

    def test_joined_claim_span_cannot_use_wrong_predicate_or_license_unknown_entity(self):
        occupation = fact("f-job", "occupation.title", "Kỹ sư cơ khí")
        facts = [fact(), occupation]
        text = "Mình đang đi Hyundai Creta và làm kỹ sư cơ khí."
        wrong_predicate = [
            claim(text, predicate="vehicle.current.model", fact_ids=("f-job",)),
            claim(text, predicate="occupation.title", fact_ids=("f-job",)),
        ]
        result = validate_generated_reply(candidate(text, claims=wrong_predicate), snapshot(facts))
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "CLAIM_PREDICATE_MISMATCH")

        invented_text = "Mình đang đi Hyundai Creta và Toyota Vios, làm kỹ sư cơ khí."
        invented_claims = [
            claim(invented_text, predicate="vehicle.current.model", fact_ids=("f-car",)),
            claim(invented_text, predicate="occupation.title", fact_ids=("f-job",)),
        ]
        result = validate_generated_reply(candidate(invented_text, claims=invented_claims), snapshot(facts))
        self.assertEqual(result["status"], "REVIEW")

    def test_other_claim_spans_and_subjects_cannot_license_persona_details(self):
        car = fact()
        past_car = fact("f-history", "vehicle.history.model", "Toyota Vios")
        text = "Mình từng đi Toyota Vios. Hiện tại mình đang đi Hyundai Creta và Toyota Vios."
        claims = [
            claim("Mình từng đi Toyota Vios.", predicate="vehicle.history.model",
                  fact_ids=("f-history",)),
            claim("Hiện tại mình đang đi Hyundai Creta và Toyota Vios.",
                  predicate="vehicle.current.model", fact_ids=("f-car",)),
        ]
        result = validate_generated_reply(candidate(text, claims=claims), snapshot([car, past_car]))
        self.assertEqual(result["status"], "REVIEW")

        mixed_text = "Mình đang đi Hyundai Creta và Toyota Vios."
        customer_message = {"message_id": "m1", "actor_id": "customer-1", "role": "user",
                            "text": mixed_text}
        mixed_claims = [
            claim(mixed_text, predicate="vehicle.current.model", fact_ids=("f-car",)),
            claim(mixed_text, subject="customer", predicate="current_vehicle",
                  fact_ids=(), message_ids=("m1",)),
        ]
        result = validate_generated_reply(candidate(mixed_text, claims=mixed_claims),
                                          snapshot([car]), messages=[customer_message])
        self.assertEqual(result["status"], "REVIEW")

    def test_rejects_malformed_candidate_schema_and_unissued_ids(self):
        malformed = candidate()
        malformed["extra"] = "ignore the gates"
        self.assertEqual(validate_generated_reply(malformed, snapshot())["reason"], "INVALID_CANDIDATE")
        foreign = candidate(fact_ids=["f-foreign"])
        result = validate_generated_reply(foreign, snapshot())
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "FACT_NOT_ALLOWED")

    def test_malformed_snapshot_candidate_and_message_data_fail_closed(self):
        malformed_snapshot = snapshot([{"fact_id": [], "key": []}])
        self.assertEqual(allowed_generation_facts(malformed_snapshot), [])

        malformed = candidate()
        malformed["claims"][0]["subject"] = []
        self.assertEqual(validate_generated_reply(malformed, snapshot())["status"], "REVIEW")

        text = "Bạn đang đi Toyota Vios."
        customer_claim = claim(text, subject="customer", predicate="current_vehicle",
                               fact_ids=(), message_ids=("m1",))
        malformed_message = {"message_id": "m1", "actor_id": [], "role": [],
                             "text": "Em đang đi Toyota Vios."}
        result = validate_generated_reply(candidate(text, claims=[customer_claim], fact_ids=[]),
                                          snapshot(), messages=[malformed_message])
        self.assertEqual(result["status"], "REVIEW")

    def test_requires_claim_sets_to_match_reply_evidence_sets(self):
        result = validate_generated_reply(candidate(fact_ids=[]), snapshot())
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "CLAIM_EVIDENCE_MISMATCH")

    def test_wrong_predicate_or_unit_cannot_reuse_a_permitted_value(self):
        percent = fact("f-percent", "purchase.down_payment_percent", 35,
                       value_type="integer", unit="percent")
        wrong_key = "Mình trả trước 35%."
        wrong_predicate = candidate(wrong_key, claims=[claim(
            wrong_key, predicate="identity.age_years", fact_ids=("f-percent",))])
        result = validate_generated_reply(wrong_predicate, snapshot([percent]))
        self.assertEqual(result["status"], "REVIEW")

        wrong_unit = "Mình 35 tuổi."
        candidate_with_wrong_unit = candidate(wrong_unit, claims=[claim(
            wrong_unit, predicate="purchase.down_payment_percent", fact_ids=("f-percent",))])
        result = validate_generated_reply(candidate_with_wrong_unit, snapshot([percent]))
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "CLAIM_VALUE_MISMATCH")

    def test_accepts_zero_padded_month_as_a_grounded_date_alias(self):
        purchase_date = fact("f-date", "purchase.date", "03/2024")
        text = "Mình mua xe vào tháng 3/2024."
        result = validate_generated_reply(
            candidate(text, claims=[claim(text, predicate="purchase.date", fact_ids=("f-date",))]),
            snapshot([purchase_date]))
        self.assertEqual(result["status"], "OK")

    def test_rejects_negated_persona_fact_and_soft_fabricated_family_opinion(self):
        negated = "Mình không đi Hyundai Creta."
        result = validate_generated_reply(candidate(negated, claims=[claim(negated)]), snapshot())
        self.assertEqual(result["status"], "REVIEW")

        embellished = "Mình đi Hyundai Creta, vợ mình cũng thích xe này."
        result = validate_generated_reply(candidate(embellished, claims=[claim(embellished)]), snapshot())
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "UNSUPPORTED_PERSONAL_DETAIL")

    def test_accepts_negation_when_it_matches_an_approved_negative_boolean_fact(self):
        first_car = fact("f-first-car", "vehicle.current.first_car", False,
                         value_type="boolean")
        text = "Đây không phải chiếc xe đầu tiên của mình."
        result = validate_generated_reply(
            candidate(text, claims=[claim(text, predicate="vehicle.current.first_car",
                                          fact_ids=("f-first-car",))]),
            snapshot([first_car]))
        self.assertEqual(result["status"], "OK")

    def test_rejects_unsupported_entity_or_date_outside_claimed_evidence(self):
        text = "Mình đang đi Hyundai Creta. Mình mua xe năm 2020."
        result = validate_generated_reply(candidate(text, claims=[claim()]), snapshot())
        self.assertEqual(result["status"], "REVIEW")

        unknown_model = "Mình đang đi Toyota Vios."
        result = validate_generated_reply(candidate(unknown_model, claims=[claim(unknown_model)]), snapshot())
        self.assertEqual(result["status"], "REVIEW")

    def test_blocks_undated_mileage_even_if_candidate_cites_the_fact(self):
        mileage = fact("f-mileage", "vehicle.current.mileage_km", 28000,
                       value_type="integer", unit="km", precision="approximate_undated")
        text = "Xe mình đi khoảng 28.000 km."
        result = validate_generated_reply(
            candidate(text, claims=[claim(text, predicate="vehicle.current.mileage_km",
                                          fact_ids=("f-mileage",))]), snapshot([mileage]))
        self.assertEqual(result["status"], "REVIEW")

    def test_rejects_transactional_promises_and_system_or_identity_claims(self):
        for text in ("Mình sẽ gọi lại cho bạn chiều nay.",
                     "Bên mình còn xe và có thể nhận cọc.",
                     "Mình không phải AI.",
                     "Mình là người thật.",
                     "Mình sẽ giữ lịch giao xe cho bạn."):
            with self.subTest(text=text):
                result = validate_generated_reply(candidate(text, claims=[]), snapshot())
                self.assertEqual(result["status"], "REVIEW")

    def test_allows_truthful_ai_disclosure_without_claiming_persona_identity(self):
        for text in ("Đây là trợ lý AI hỗ trợ trả lời theo hồ sơ của người viết.",
                     "Đây là nhân vật mô phỏng bằng AI trong sandbox."):
            with self.subTest(text=text):
                result = validate_generated_reply(candidate(text, claims=[]), snapshot())
                self.assertEqual(result["status"], "OK")

    def test_allows_benign_social_reaction_without_persona_facts(self):
        text = "Có gì đâu, mình cùng trao đổi thôi."
        result = validate_generated_reply(candidate(text, claims=[]), snapshot())
        self.assertEqual(result["status"], "OK")

    def test_allows_bounded_acknowledgments_without_persona_claims(self):
        for text in (
                "Mình ghi nhận nhu cầu này nhé.",
                "Được, mình dừng ở đây nhé.",
                "Ừ, vậy mình dừng ở đây nhé.",
                "À, mình hiểu nhầm ý bạn, xin lỗi nhé.",
                "Có gì đâu, bạn cứ đi xem thử nhé."):
            with self.subTest(text=text):
                result = validate_generated_reply(candidate(text, claims=[]), snapshot())
                self.assertEqual(result["status"], "OK", result)

    def test_allows_bounded_epistemic_clauses_but_keeps_the_fact_claim_grounded(self):
        text = "Mình đang đi Hyundai Creta. Còn đi lâu chưa thì mình chưa có thông tin chính xác."
        result = validate_generated_reply(
            candidate(text, claims=[claim("Mình đang đi Hyundai Creta.")]), snapshot())
        self.assertEqual(result["status"], "OK", result)

        # A safe disclaimer may be attached to the same claim span. Negation in
        # that clause must not invalidate the separate, positively grounded car fact.
        joined = "Mình đang đi Hyundai Creta, còn đi lâu chưa thì mình chưa có thông tin chính xác."
        result = validate_generated_reply(
            candidate(joined, claims=[claim("Mình đang đi Hyundai Creta")]), snapshot())
        self.assertEqual(result["status"], "OK", result)

        # If the engine returns one exact span for both grounded fact and
        # uncertainty, only the safe epistemic clause is exempted from the
        # negation check.
        result = validate_generated_reply(
            candidate(joined, claims=[claim(joined)]), snapshot())
        self.assertEqual(result["status"], "OK", result)

        no_source = "Còn ghế trẻ em có vừa không thì mình chưa có thông tin xác nhận."
        result = validate_generated_reply(candidate(no_source, claims=[]), snapshot())
        self.assertEqual(result["status"], "OK", result)

        generic = "Mình không có căn cứ để nói điều đó."
        result = validate_generated_reply(candidate(generic, claims=[]), snapshot())
        self.assertEqual(result["status"], "OK", result)

    def test_epistemic_prefix_cannot_smuggle_unsupported_personal_history(self):
        for text in (
                "Mình chưa từng lái thử Tucson.",
                "Mình không biết mình đã lái thử Tucson chưa.",
                "Mình chưa có xe.",
                "Mình không biết mình có xe chưa.",
                "Mình chưa có thông tin là mình đã lái thử Tucson.",
                "Còn mình đã đi chơi chưa thì mình chưa có thông tin.",
                "Mình chưa có thông tin xác nhận rằng vợ mình thích Tucson.",
                "Mình chưa có thông tin xác nhận rằng cả nhà đã đi thử xe.",
                "Mình chưa có thông tin chính xác, chiều nay mình sẽ gọi lại cho bạn."):
            with self.subTest(text=text):
                result = validate_generated_reply(candidate(text, claims=[]), snapshot())
                self.assertEqual(result["status"], "REVIEW", result)

    def test_public_channel_cannot_ask_for_personal_contact_details(self):
        text = "Bạn có thể gửi số điện thoại qua Zalo."
        result = validate_generated_reply(candidate(text, claims=[]), snapshot(), channel="public")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "PUBLIC_CONTACT_SOLICITATION")

    def test_unclaimed_persona_statement_after_a_valid_claim_is_still_reviewed(self):
        text = "Mình đang đi Hyundai Creta. Mình làm kỹ sư cơ khí."
        result = validate_generated_reply(candidate(text, claims=[claim()]), snapshot())
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "UNSUPPORTED_PERSONAL_DETAIL")

    def test_unclaimed_opinion_sentence_after_a_valid_claim_is_reviewed(self):
        text = "Mình đang đi Hyundai Creta. Xe này khá rộng rãi."
        result = validate_generated_reply(candidate(text, claims=[claim()]), snapshot())
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "UNSUPPORTED_PERSONAL_DETAIL")

    def test_question_must_match_engine_permission_and_only_one_question_is_allowed(self):
        text = "Mình đang đi Hyundai Creta. Bạn thường đi phố hay đường dài hơn?"
        question = {"kind": "clarification", "field": "usage_need",
                    "text": "Bạn thường đi phố hay đường dài hơn?"}
        result = validate_generated_reply(candidate(text, claims=[claim()], asks_question=True,
                                                   question=question), snapshot(),
                                          allowed_question={"kind": "clarification", "field": "usage_need"})
        self.assertEqual(result["status"], "OK")

        blocked = validate_generated_reply(candidate(text, claims=[claim()], asks_question=True,
                                                     question=question), snapshot())
        self.assertEqual(blocked["status"], "REVIEW")
        self.assertEqual(blocked["reason"], "QUESTION_NOT_ALLOWED")

        no_mark = "Mình đang đi Hyundai Creta. Bạn thường đi phố à Bạn đang cân nhắc đổi xe à"
        q = {"kind": "clarification", "field": "usage_need", "text": "Bạn thường đi phố à"}
        result = validate_generated_reply(candidate(no_mark, claims=[claim()], asks_question=True,
                                                   question=q), snapshot(),
                                          allowed_question={"kind": "clarification", "field": "usage_need"})
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "REPLY_LIMIT_EXCEEDED")

        # Move the single mark onto a factual sentence: the question field
        # must identify the question that was actually asked.
        mismatched_text = "Mình đang đi Hyundai Creta? Bạn đang cân nhắc xe."
        mismatched_question = {"kind": "clarification", "field": "usage_need",
                               "text": "Bạn đang cân nhắc xe."}
        result = validate_generated_reply(
            candidate(mismatched_text, claims=[claim("Mình đang đi Hyundai Creta")], asks_question=True,
                      question=mismatched_question), snapshot(),
            allowed_question={"kind": "clarification", "field": "usage_need"})
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "QUESTION_MISMATCH")

        multi = "Mình đang đi Hyundai Creta. Bạn đi xe gì, bạn làm nghề gì?"
        multi_question = {"kind": "conversation", "field": None,
                          "text": "Bạn đi xe gì, bạn làm nghề gì?"}
        result = validate_generated_reply(
            candidate(multi, claims=[claim()], asks_question=True, question=multi_question),
            snapshot(), allowed_question={"kind": "conversation", "field": None})
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "REPLY_LIMIT_EXCEEDED")

    def test_draft_text_is_retained_for_operator_review_but_status_stays_review(self):
        text = "Mình đang đi Hyundai Creta, vợ mình cũng thích xe này."
        result = validate_generated_reply(candidate(text, claims=[claim(text)]), snapshot())
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["text"], text)
        self.assertTrue(result["reason"])

    def test_customer_claim_requires_a_supplied_verified_message_and_closed_predicate(self):
        text = "Bạn đang đi Toyota Vios."
        customer_claim = claim(text, subject="customer", predicate="current_vehicle",
                               fact_ids=(), message_ids=("m1",))
        message = {"message_id": "m1", "actor_id": "customer-1", "role": "user",
                   "text": "Em đang đi Toyota Vios.", "page_id": "page-1", "post_id": "post-1",
                   "thread_root_id": "root-1"}
        result = validate_generated_reply(candidate(text, claims=[customer_claim], fact_ids=[]),
                                          snapshot(), messages=[message])
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["fact_ids"], [])
        self.assertEqual(result["manifest"]["messages"][0]["message_id"], "m1")

        bad_predicate = dict(customer_claim, predicate="identity.age_years")
        result = validate_generated_reply(candidate(text, claims=[bad_predicate], fact_ids=[]),
                                          snapshot(), messages=[message])
        self.assertEqual(result["status"], "REVIEW")

    def test_customer_claim_cannot_use_bot_or_uncited_message(self):
        text = "Bạn đang đi Toyota Vios."
        customer_claim = claim(text, subject="customer", predicate="current_vehicle",
                               fact_ids=(), message_ids=("m1",))
        bot_message = {"message_id": "m1", "actor_id": "bot", "role": "bot",
                       "text": "Bạn đang đi Toyota Vios."}
        result = validate_generated_reply(candidate(text, claims=[customer_claim], fact_ids=[]),
                                          snapshot(), messages=[bot_message])
        self.assertEqual(result["status"], "REVIEW")

        unsupported = "Bạn thích Toyota Vios."
        customer_claim = claim(unsupported, subject="customer", predicate="current_vehicle",
                               fact_ids=(), message_ids=("m1",))
        user_message = dict(bot_message, role="user", actor_id="customer-1",
                            text="Em đang đi Toyota Vios.")
        result = validate_generated_reply(candidate(unsupported, claims=[customer_claim], fact_ids=[]),
                                          snapshot(), messages=[user_message])
        self.assertEqual(result["status"], "REVIEW")

    def test_allowed_memory_requires_valid_checksum_and_linked_public_fact(self):
        travel = fact("f-travel", "travel.weekend.destination", "Nam Định")
        memory = {
            "memory_id": "m-story", "persona_uuid": "p1", "release_id": "r1",
            "topic": "travel_story", "occurred_at": "2026-09-01", "precision": "exact",
            "source_ref": "approved:story:1", "source_kind": "official_source",
            "approval": "approved", "privacy": "public", "tellable": "public",
            "consent_ref": "approved:consent:1",
            "eligible_for_generation": True,
            "fact_ids": ["f-travel"], "approved_summary": "Cuối tuần mình về Nam Định.",
        }
        memory["checksum"] = memory_checksum(memory)
        value = snapshot([travel], memories=[memory])
        text = "Cuối tuần mình về Nam Định."
        story_claim = claim(text, predicate="travel.weekend.destination", fact_ids=(),
                            memory_ids=("m-story",))
        result = validate_generated_reply(candidate(text, claims=[story_claim], fact_ids=[]), value,
                                          allowed_memory_ids=["m-story"])
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["memory_ids"], ["m-story"])
        self.assertEqual(result["manifest"]["fact_ids"], ["f-travel"])

        value["revoked_memory_ids"] = ["m-story"]
        result = validate_generated_reply(candidate(text, claims=[story_claim], fact_ids=[]), value,
                                         allowed_memory_ids=["m-story"])
        self.assertEqual(result["status"], "REVIEW")
        value.pop("revoked_memory_ids")

        value["memories"][0].pop("consent_ref")
        value["memories"][0]["checksum"] = memory_checksum(value["memories"][0])
        self.assertEqual(validate_generated_reply(candidate(text, claims=[story_claim], fact_ids=[]),
                                                  value, allowed_memory_ids=["m-story"])["status"],
                         "REVIEW")

        value["memories"][0]["approved_summary"] = "Cuối tuần mình đi Đà Lạt."
        result = validate_generated_reply(candidate(text, claims=[story_claim], fact_ids=[]), value,
                                          allowed_memory_ids=["m-story"])
        self.assertEqual(result["status"], "REVIEW")

    def test_rejects_unapproved_private_or_unselected_memory(self):
        travel = fact("f-travel", "travel.weekend.destination", "Nam Định")
        memory = {
            "memory_id": "m-story", "persona_uuid": "p1", "release_id": "r1",
            "topic": "travel_story", "occurred_at": None, "precision": "undated",
            "source_ref": "approved:story:1", "source_kind": "official_source",
            "approval": "draft", "privacy": "private", "tellable": "never",
            "fact_ids": ["f-travel"], "approved_summary": "Cuối tuần mình về Nam Định.",
        }
        memory["checksum"] = memory_checksum(memory)
        value = snapshot([travel], memories=[memory])
        text = "Cuối tuần mình về Nam Định."
        story_claim = claim(text, predicate="travel.weekend.destination", fact_ids=(),
                            memory_ids=("m-story",))
        result = validate_generated_reply(candidate(text, claims=[story_claim], fact_ids=[]), value,
                                          allowed_memory_ids=["m-story"])
        self.assertEqual(result["status"], "REVIEW")
        value["memories"][0]["approval"] = "approved"
        value["memories"][0]["privacy"] = "public"
        value["memories"][0]["tellable"] = "public"
        value["memories"][0]["checksum"] = memory_checksum(value["memories"][0])
        result = validate_generated_reply(candidate(text, claims=[story_claim], fact_ids=[]), value,
                                          allowed_memory_ids=[])
        self.assertEqual(result["status"], "REVIEW")

    def test_reply_uses_at_most_one_memory_even_when_engine_allows_two(self):
        first = "Cuối tuần mình về Nam Định."
        second = "Mình đi làm tuyến Cầu Giấy–Mỹ Đình."
        text = first + " " + second
        claims = [claim(first, predicate="travel.weekend.destination", fact_ids=(),
                        memory_ids=("m-story-1",)),
                  claim(second, predicate="travel.commute", fact_ids=(),
                        memory_ids=("m-story-2",))]
        result = validate_generated_reply(candidate(text, claims=claims, fact_ids=[]), snapshot(),
                                         allowed_memory_ids=["m-story-1", "m-story-2"])
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "MEMORY_LIMIT_EXCEEDED")

    def test_reads_the_real_binding_snapshot_shape_and_keeps_fixture_flag_separate(self):
        import sqlite3
        from app.persona_fixture import seed_nam_creta
        from app.persona_store import PersonaStore, migrate_sqlite

        conn = sqlite3.connect(":memory:")
        try:
            migrate_sqlite(conn)
            store = PersonaStore(conn)
            persona_id, release_id = seed_nam_creta(store)
            store.bind_post("page", "post", persona_id, release_id)
            binding = store.get_binding_snapshot("page", "post")
            rows = allowed_generation_facts(binding, allow_fictional_fixture=True)
            self.assertTrue(rows)
            vehicle = next(item for item in rows if item["key"] == "vehicle.current.model")
            text = "Mình đang đi Hyundai Creta."
            used = claim(text, fact_ids=(vehicle["fact_id"],))
            blocked = validate_generated_reply(candidate(text, claims=[used]), binding)
            self.assertEqual(blocked["status"], "REVIEW")
            accepted = validate_generated_reply(candidate(text, claims=[used]), binding,
                                                allow_fictional_fixture=True)
            self.assertEqual(accepted["status"], "OK")
            wrapped = {"snapshot": binding, "channel": "private"}
            accepted = validate_generated_reply(candidate(text, claims=[used]), wrapped,
                                                allow_fictional_fixture=True)
            self.assertEqual(accepted["status"], "OK")
        finally:
            conn.close()

    def test_grounded_customer_correction_with_polite_acknowledgement(self):
        text = 'À, bạn có xe rồi, không tìm mua. Mình hiểu rồi nhé.'
        span = 'À, bạn có xe rồi, không tìm mua.'
        messages = [dict(message_id='m1', actor_id='customer', role='user',
                         text='Tui có xế rồi nha, không tìm mua đâu')]
        result = validate_generated_reply(candidate(text, claims=[
            claim(span, subject='customer', predicate='ownership_status', fact_ids=(), message_ids=('m1',)),
            claim(span, subject='customer', predicate='purchase_intent', fact_ids=(), message_ids=('m1',))]),
            snapshot(), messages=messages)
        self.assertEqual(result['status'], 'OK', result)


if __name__ == "__main__":
    unittest.main()
