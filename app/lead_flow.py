"""Peer-conversation helpers: address terms, ownership/interest cues, lead answers.

Pure functions only. Nothing here touches the database or dispatches a lead.
"""
import re
from .normalize import norm, strip_diacritics, VEHICLES, COLORS, LOCATIONS

FIELDS = ('interested_vehicle', 'purchase_timeline', 'finance_intent', 'budget', 'usage_need', 'current_vehicle')

_TERMS = ('bác', 'chú', 'cô', 'chị', 'anh', 'bạn')
# Without diacritics "co" is ambiguous ("có" vs "cô"), so it is never a term there.
_PLAIN_TERMS = {'bac': 'bác', 'chu': 'chú', 'chi': 'chị', 'anh': 'anh', 'ban': 'bạn'}
_LEADERS = ('chào', 'hi', 'hello', 'ơi', 'alo', 'giống', 'như', 'hỏi', 'thưa', 'cảm ơn', 'ok', 'à')
_ACTIONS = ('đi', 'chạy', 'mua', 'dùng', 'xài', 'lái', 'có', 'ở', 'sống', 'làm', 'thích', 'nghĩ',
            'tính', 'định', 'đang', 'cũng', 'đã', 'từng', 'trả', 'bảo', 'muốn', 'thấy', 'biết', 'ơi', 'à', 'nhỉ')
_THIRD = re.compile(r'(?:trai|gái|em|tôi|mình|tui|của|ấy|kia)(?!\w)')
_BEFORE_OTHER = re.compile(r'(?:^|\s)(?:cho|của|với|hỏi hộ|giúp)$')


def _starts_with(body, words):
    return any(re.match(re.escape(w) + r'(?!\w)', body) for w in words)


def address_explicit(text):
    """Term the speaker uses for the addressee, or None. Never guesses age or gender."""
    raw = re.sub(r'\s+', ' ', (text or '').lower()).strip()
    plain = strip_diacritics(raw) == raw
    terms = _PLAIN_TERMS if plain else {t: t for t in _TERMS}
    leaders = tuple(strip_diacritics(w) for w in _LEADERS) if plain else _LEADERS
    actions = tuple(strip_diacritics(w) for w in _ACTIONS) if plain else _ACTIONS
    third = re.compile(strip_diacritics(_THIRD.pattern)) if plain else _THIRD
    for m in re.finditer(r'(?<!\w)(' + '|'.join(terms) + r')(?!\w)', raw):
        before = raw[:m.start()].strip(' ,.!?')
        after = raw[m.end():].strip(' ,.!?')
        separated = bool(re.match(r'\s*[,;:]', raw[m.end():]))
        if (third.match(after) and not separated) or _BEFORE_OTHER.search(strip_diacritics(before) if plain else before):
            continue
        if not before or any(before.endswith(w) for w in leaders) or _starts_with(after, actions) or not after or re.fullmatch(r'(?:ạ|nhé|nha|a|nhe)[.! ]*', after):
            return terms[m.group(1)]
    return None


def address(text, previous=None):
    """Explicit term in this message, else the one already used with this person, else 'bạn'."""
    return address_explicit(text) or (previous if previous in _TERMS else 'bạn')


_PERSON = r'(?:toi|tui|minh|em)'
# "anh tôi", "bạn tôi", "bố mình": the pronoun belongs to a relative, not to the speaker.
_OF_OTHER = re.compile(r'\b(?:anh|chi|em|ban|bo|me|ba|vo|chong|con|cau|duong|bac|chu|co|dong nghiep)\s+(?:trai\s+|gai\s+)?(?:toi|minh|tui)\b'
                       r'|\b(?:cho|giup|ho)\s+(?:anh|chi|em|ban|bo|me|ba|vo|chong|con|cau|duong|bac|chu|co)\b')
_BUY_FIRST = re.compile(r'(?:\b' + _PERSON + r'\s+|^)(?:cung\s+)?(?:dang\s+)?(?:tinh|dinh|muon|can)\s+(mua|doi|thay)\b')
_BUY_EXCLUDE = re.compile(r'\b(?:khong|chua|coc|chot|dat mua|tra tien|da mua|mua roi)\b|\bmua\s+(?:xe\s+)?cho\s+\w+')
_CAR = r'(?:xe|oto|o to|' + '|'.join(re.escape(k) for k in VEHICLES) + r')'
# Own a car: first person ("tôi có xe rồi"), or a sentence that simply starts with "có xe rồi".
_OWNS = re.compile(r'\b' + _PERSON + r'\s+(?:cung\s+)?(?:da\s+|dang\s+)?co\s+' + _CAR + r'\b'
                   r'|^(?:da\s+)?co\s+' + _CAR + r'\s+roi\b')
_NO_OWN = re.compile(r'\b(?:chua|khong|ko)\s+co\s+xe\b')
_SWITCH = re.compile(r'\b(?:doi|thay|mua them|nang cap|len doi)\s+(?:chiec\s+)?' + _CAR + r'\b')
_WANT = re.compile(r'\b(?:muon|dinh|tinh|can)\b')
_DECLINE = re.compile(r'\b(?:khong can|dung hoi|khong muon|khong co nhu cau)\b|\bthoi(?:\s+(?:nhe|nha|roi|a|vay))?\s*[.!]*$')
_TRANSACTION = re.compile(r'\b(?:coc|chot|dat mua|dat xe|dat truoc|tra tien|ky hop dong)\b')
_CORRECT = re.compile(r'\b(?:khong phai|hieu nham|nham roi|y toi la|y minh la)\b')


def shallow_interest(text):
    """First-person, non-transactional wish to buy. Third parties, negation and past tense are excluded."""
    t = norm(text or '')
    match = _BUY_FIRST.search(t)
    return bool(match and (match.group(1) == 'mua' or wants_switch(text))) and not _BUY_EXCLUDE.search(t) and not _OF_OTHER.search(t)


def states_ownership(text):
    """True only for the speaker's own car ('tôi có xe rồi'); False for negation or other people."""
    t = norm(text or '')
    return bool(_OWNS.search(t)) and not _NO_OWN.search(t) and not _OF_OTHER.search(t)


def wants_switch(text):
    """Owner who still wants another/new car: 'có xe rồi nhưng muốn đổi'."""
    t = norm(text or '')
    implicit = states_ownership(text) and re.search(r'\b(?:muon|dinh|tinh|can)\s+(?:doi|thay)[.! ]*$', t)
    return bool((_SWITCH.search(t) or implicit) and _WANT.search(t)) and not _OF_OTHER.search(t) and not _NO_OWN.search(t)


def is_transaction(text):
    """Deposit / closing / payment wording: these go to a human, never the peer chat."""
    return bool(_TRANSACTION.search(norm(text or '')))


def peer_cue(text):
    """The customer said something about their own car or buying that the peer flow handles."""
    return not is_transaction(text) and (states_ownership(text) or shallow_interest(text) or wants_switch(text))


def is_decline(text):
    value = norm(text or '')
    if ('chua co nhu cau' in value or re.fullmatch(
            r'(?:(?:toi|minh|em)\s+)?(?:khong mua nua(?: dau)?|thoi khong lay nua|chua mua(?: dau)?(?: ad)?|de sau mua)[.! ]*', value)):
        return True
    if re.search(r'\bchi\b.{0,60}\bthoi\s*[.!]*$', value) and not re.search(r'\b(?:dung hoi|khong can|khong muon|khong co nhu cau)\b', value):
        return False
    return bool(_DECLINE.search(value))


def social_reaction(text):
    """Recognize short reactions only; other statements stay unresolved."""
    value = norm(text or '').strip('!. ')
    if re.fullmatch(r'(?:xe\s+)?(?:dep|xin)(?: qua)?(?: ad oi)?', value):
        return 'praise'
    if re.fullmatch(r'(?:ha){2,}|(?:hi){2,}|hehe', value):
        return 'laughter'
    if re.fullmatch(r'(?:@\w+ xem xe nay ne|tag ban @\w+ xem ne)', value):
        return 'tag_friend'
    return None


def is_correction(text):
    return bool(_CORRECT.search(norm(text or '')))


def is_ai_feedback(text):
    return bool(re.fullmatch(r'(?:ban|bac|anh|chi)\s+(?:nhan|noi|tra loi)\s+(?:nhu|giong)\s+(?:ai|bot|robot)(?:\s+(?:a|qua|vay))?[.!? ]*', norm(text or '')))


def asks_usage_duration(text):
    """Question about how long Nam has been using the car ('bác chạy bao lâu rồi')."""
    t = norm(text or '')
    return bool(re.search(r'\b(?:bac|chu|co|chi|anh|ban)\s+(?:dang\s+)?(?:chay|di|dung|lai|xai)\b.{0,20}\b(?:bao lau|may nam|may thang|bao nhieu nam|bao nhieu thang)\b', t))


def next_field(profile):
    return next((f for f in FIELDS if f not in profile), None)


def _vehicle(t):
    for key, name in VEHICLES.items():
        if re.search(r'\b' + re.escape(key) + r'\b', t):
            return name
    return None


def extract_answer(field, text):
    """Narrow, field-bound answers only. No guessing from word counts or bare digits."""
    if not text or '?' in text:
        return None
    t = norm(text)
    words = t.split()
    if field in ('interested_vehicle', 'current_vehicle'):
        # "Creta giống bác" compares with Nam's car; it does not say whether they own or want one.
        if re.search(r'\b(?:giong|nhu)\b', t):
            return None
        return _vehicle(t) if len(words) <= 5 else None
    if len(words) > 8:
        return None
    if field == 'color':
        return next((name for key, name in COLORS.items() if re.search(r'\b' + key + r'\b', t)), None)
    if field == 'dealer':
        return next((name for key, name in LOCATIONS.items() if re.search(r'\b' + key + r'\b', t)), None)
    if field == 'purchase_timeline' and (re.search(r'\b(?:thang|nam|tuan|ngay|som|quy)\b', t) or 'chua ro' in t):
        return text.strip()
    if field == 'finance_intent' and re.search(r'\b(?:gop|vay|tra thang|tien mat)\b', t):
        return text.strip()
    if field == 'budget' and re.search(r'\d+(?:[.,]\d+)?\s*(?:trieu|ty|tr)\b', t):
        return text.strip()
    return None


def mentions_other_person(text):
    value = norm(text or '')
    return bool(_OF_OTHER.search(value) or re.search(r'\bhoi ho\b', value))


def purchase_subject_correction(text):
    value = norm(text or '')
    return bool(re.search(r'\bkhong phai (?:toi|minh|em) mua\b', value) and mentions_other_person(text))


def proxy_clarification(text):
    return bool(re.fullmatch(r'(?:toi|minh|em) chi hoi ho thoi[.! ]*', norm(text or '')))


def plan_peer_turn(text, state, profile, pending, comment_id, social=False, persona_question=False):
    """Decide what this turn means and whether a follow-up question is allowed.

    Pure: returns the next state, the reply topic and the lead/pending updates; the
    engine applies them. A follow-up question needs purchase_intent == 'exploring'
    (said by the customer); owning a car alone never starts the funnel; a question
    that was ignored, declined or corrected is not asked again.
    """
    state = dict(state or {})
    ns = dict(state)
    explicit = address_explicit(text)
    if explicit:
        ns['address_term'] = explicit
    plan = dict(call=address(text, state.get('address_term')), topic=None, hook_field=None, supersede=[],
                lead_updates=[], next_state=ns, reason='')
    if is_ai_feedback(text):
        plan['supersede'] = [(p, 'SUPERSEDED') for p in pending]
        plan.update(topic='ai_disclosure', reason='conversation_feedback')
        ns['follow_up'] = 'stopped'
        return plan
    if (social or persona_question) and not (states_ownership(text) or is_decline(text) or purchase_subject_correction(text)):
        if persona_question and shallow_interest(text):
            ns['purchase_intent'] = 'exploring'
            vehicle = _vehicle(norm(text))
            if vehicle:
                plan['lead_updates'].append(('interested_vehicle', vehicle, 'ANSWERED'))
            plan['supersede'] = [(p, 'SUPERSEDED') for p in pending]
        # A mixed turn gets the persona answer first, without repeating a lead question.
        plan['reason'] = 'social' if social else 'persona_question'
        return plan

    text = text or ''
    pend = pending[-1] if pending else None
    field = pend['expected_slot'] if pend else None
    owns, switch, interest = states_ownership(text), wants_switch(text), shallow_interest(text)
    after = dict(profile or {})
    vehicle = _vehicle(norm(text))
    answered_now = [f for f, v in after.items()
                    if v.get('source_message_id') == comment_id and v.get('status') == 'ANSWERED']

    def close_pending(status):
        plan['supersede'].extend((p, status) for p in pending)

    def answer(f, value):
        plan['lead_updates'].append((f, value, 'ANSWERED'))
        after[f] = dict(status='ANSWERED', value=value)

    def hook():
        # A lead question needs the customer's own buying intent, never more than 3 per
        # conversation, and none after they declined or ignored one.
        if (ns.get('purchase_intent') == 'exploring' and ns.get('follow_up') != 'stopped'
                and ns.get('asked_total', 0) < 3):
            plan['hook_field'] = next_field(after)

    if is_decline(text) and not interest:
        if pend:
            plan['lead_updates'].append((field, None, 'DECLINED'))
            close_pending('REJECTED')
        ns['follow_up'] = 'stopped'
        plan['topic'] = 'decline_ack'
        plan['reason'] = 'decline'
    elif owns and not switch:
        repair = bool(pend) or state.get('purchase_intent') == 'exploring'
        ns['ownership_status'] = 'owns_vehicle'
        if repair:
            ns['purchase_intent'] = 'unknown'
            ns['follow_up'] = 'stopped'
        close_pending('SUPERSEDED')
        plan['topic'] = 'ownership_repair' if repair else 'ownership_ack'
        plan['reason'] = 'ownership'
    elif switch or interest:
        if owns:
            ns['ownership_status'] = 'owns_vehicle'
        ns['purchase_intent'] = 'exploring'
        ns['follow_up'] = None
        if vehicle and 'interested_vehicle' not in after:
            answer('interested_vehicle', vehicle)
        plan['topic'] = 'switch' if switch else 'interest'
        plan['reason'] = 'purchase_interest'
        if pend:
            close_pending('SUPERSEDED')
        hook()
    elif is_correction(text):
        value = extract_answer(field, text) if pend else None
        if value is not None:
            answer(field, value)
        if pend:
            close_pending('ANSWERED' if value is not None else 'SUPERSEDED')
        if value is None:
            ns['purchase_intent'] = 'unknown'
            ns['follow_up'] = 'stopped'
            if mentions_other_person(text):
                for key, item in after.items():
                    if item.get('status') in ('ASKED', 'ANSWERED'):
                        plan['lead_updates'].append((key, None, 'DECLINED'))
        plan['topic'] = 'repair'
        plan['reason'] = 'correction'
    elif pend and field != 'purchase_timeline' and extract_answer('purchase_timeline', text) is not None:
        answer('purchase_timeline', extract_answer('purchase_timeline', text))
        close_pending('SUPERSEDED')
        ns['follow_up'] = 'stopped'
        plan['topic'] = 'ack_purchase_timeline'
        plan['reason'] = 'volunteered_timeline'
    elif answered_now:
        # The model already recorded the answer to the open question this turn.
        plan['topic'] = 'ack_' + answered_now[0]
        plan['reason'] = 'answered_pending'
        if answered_now[0] == 'interested_vehicle':
            hook()
    elif pend and extract_answer(field, text) is not None:
        answer(field, extract_answer(field, text))
        close_pending('ANSWERED')
        plan['topic'] = 'ack_' + field
        plan['reason'] = 'answered_pending'
        if field == 'interested_vehicle':
            hook()
    else:
        statement = '?' not in text
        if mentions_other_person(text):
            plan['topic'] = 'proxy_clarification' if proxy_clarification(text) else 'other_person'
            plan['reason'] = 'other_person'
        elif vehicle and statement:
            if pend:
                close_pending('SUPERSEDED')
            plan['topic'] = 'ack' if ns.get('ownership_status') == 'owns_vehicle' else 'clarify_own_or_buy'
            plan['reason'] = 'vehicle_mention'
        else:
            if pend:
                # Asked once and not answered: stop until the customer raises buying again.
                close_pending('SUPERSEDED')
                ns['follow_up'] = 'stopped'
                plan['reason'] = 'pending_ignored'
            # A statement we do not understand gets no canned "I understand": no topic -> review.
            plan['reason'] = plan['reason'] or 'not_understood'
    return plan

