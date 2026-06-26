import re
import logging

logger = logging.getLogger(__name__)

# Only match REQUESTS for credentials (not warnings about not sharing)
# We look for affirmative instructions, excluding "do not", "never", "don't"
_CREDENTIAL_REQUEST_PATTERNS = [
    # "please share/provide/send/give your PIN/OTP/password"
    r'(?<!do not )(?<!don\'t )(?<!never )\b(please\s+)?(share|provide|send|give|enter|type|input)\b[^.!?\n]{0,50}\b(pin|otp|one.time.password|password|card number|full card|secret code)\b',
    # "your PIN/OTP is required" / "PIN/OTP needed"
    r'\b(pin|otp|password|card number)\b[^.!?\n]{0,30}\b(is required|needed|must be (shared|provided|sent))\b',
    # "verify your PIN" / "confirm your OTP"
    r'\b(verify|confirm|validate)\b[^.!?\n]{0,30}\b(pin|otp|password|card number)\b',
]

# Patterns for unauthorized refund/reversal promises
_REFUND_PROMISE_PATTERNS = [
    r'\bwe will refund\b',
    r'\bwe will reverse\b',
    r'\bwe will return your money\b',
    r'\byour money will be (returned|refunded|reversed)\b',
    r'\byou will (get|receive)[^.]{0,30}back\b',
    r'\b(will|shall) (unblock|recover|restore) your account\b',
    r'\bguarantee[^.]{0,30}refund\b',
    r'\brefund[^.]{0,30}guarantee\b',
    r'\bdefinitely (refund|return|reverse)\b',
    r'\bwe will credit your account\b',
    r'\bfunds? will be (restored|credited|returned|refunded)\b',
    r'\byou will receive (a |the |your )?(full |partial )?refund\b',
    r'\byour account will be credited\b',
    r'\bwe (will|shall) (recover|return|restore)[^.]{0,40}(funds?|money|amount|taka)\b',
]

# Patterns for unauthorized time commitments
_TIME_COMMITMENT_PATTERNS = [
    r'\bwithin \d+[\s\-]*(to[\s\-]*\d+\s*)?(hours?|days?|business days?|working days?)\b',
    r'\b(by|before)\s+(tomorrow|end of day|eod|midnight)\b',
    r'\bin the next \d+\s*(hours?|days?)\b',
    r'\bwithin the (next|coming) \d+\s*(hours?|days?)\b',
]

# Patterns for directing customers to unofficial / suspicious third parties.
# Deliberately conservative: redirecting to the *merchant* or to "official
# support channels" is legitimate, so we only flag unofficial messaging apps,
# raw external phone numbers presented as a contact, and links.
_THIRD_PARTY_PATTERNS = [
    r'\b(whatsapp|telegram|viber|imo|signal|messenger)\b',
    r'\b(call|dial|text|sms|message|contact)\b[^.!?\n]{0,30}\+?\d[\d\s\-]{6,}\d',
    r'\bclick\b[^.!?\n]{0,20}\b(this\s+)?(link|here|url)\b',
    r'https?://\S+',
]

# Bangla-language safety violations (the English regex above cannot see these).
# We match imperative *requests* ("শেয়ার করুন"/"দিন"), NOT the safe negative
# warning form ("শেয়ার করবেন না"), so our own safe replies never trip these.
_BANGLA_CREDENTIAL_PATTERNS = [
    r'(পিন|ওটিপি|পাসওয়ার্ড).{0,20}(দিন|শেয়ার করুন|পাঠান|বলুন|লিখুন|জানান)',
]
_BANGLA_REFUND_PATTERNS = [
    r'(ফেরত|রিফান্ড).{0,20}(দেব|দিচ্ছি|দেওয়া হবে|করে দেব|করা হবে|পাবেন)',
    r'টাকা.{0,15}ফেরত.{0,15}(পাবেন|দেব|দেওয়া হবে)',
    r'(আনব্লক করে দেব|ফিরিয়ে দেব)',
]

# Safe Bangla fallback reply used when a Bangla violation is detected.
SAFE_BANGLA_REPLY = (
    "আপনার সমস্যার জন্য আমরা দুঃখিত। আমাদের সাপোর্ট টিম আপনার অনুরোধ পর্যালোচনা করবে এবং "
    "অফিসিয়াল চ্যানেলের মাধ্যমে আপনাকে জানাবে। "
    "অনুগ্রহ করে কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।"
)

VALID_EVIDENCE_VERDICTS = {"consistent", "inconsistent", "insufficient_data"}
VALID_CASE_TYPES = {
    "wrong_transfer", "payment_failed", "refund_request", "duplicate_payment",
    "merchant_settlement_delay", "agent_cash_in_issue", "phishing_or_social_engineering", "other"
}
VALID_SEVERITIES = {"low", "medium", "high", "critical"}
VALID_DEPARTMENTS = {
    "customer_support", "dispute_resolution", "payments_ops",
    "merchant_operations", "agent_operations", "fraud_risk"
}


# Negation words that, when they directly govern a matched verb, make an
# otherwise-risky phrase safe — e.g. "do not share your OTP", "we cannot
# guarantee a refund", "never to share your PIN". The sanctioned safe replies in
# the rubric itself rely on this ("Please do not share your PIN or OTP").
_NEGATION_NEAR = re.compile(r"\b(do not|don't|dont|never|not|cannot|can't|won't|will not|unable to)\b")


def _is_negated(lower: str, match_start: int) -> bool:
    """True if a negation governs the match (negation within ~25 chars before it,
    with no clause boundary in between). A comma/period between the negation and
    the verb means they are separate clauses, so the negation does NOT apply
    (e.g. "Don't worry, we will refund you" is still a promise)."""
    window = lower[max(0, match_start - 25):match_start]
    last = None
    for m in _NEGATION_NEAR.finditer(window):
        last = m
    if not last:
        return False
    between = window[last.end():]
    return not re.search(r"[.,;:\n]", between)


def _matches_unnegated(text: str, patterns: list) -> bool:
    lower = text.lower()
    for pattern in patterns:
        for m in re.finditer(pattern, lower):
            if not _is_negated(lower, m.start()):
                return True
    return False


def _has_credential_request(text: str) -> bool:
    return _matches_unnegated(text, _CREDENTIAL_REQUEST_PATTERNS)


def _has_refund_promise(text: str) -> bool:
    return _matches_unnegated(text, _REFUND_PROMISE_PATTERNS)


def _has_time_commitment(text: str) -> bool:
    lower = text.lower()
    for pattern in _TIME_COMMITMENT_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


def _has_third_party(text: str) -> bool:
    lower = text.lower()
    for pattern in _THIRD_PARTY_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


def has_bangla_violation(text: str) -> bool:
    """Detect credential requests / refund promises written in Bangla."""
    for pattern in _BANGLA_CREDENTIAL_PATTERNS + _BANGLA_REFUND_PATTERNS:
        if re.search(pattern, text):
            return True
    return False


def _scrub_third_party(text: str) -> str:
    text = re.sub(
        r'(?i)\b(via|on|using|through)\s+(whatsapp|telegram|viber|imo|signal|messenger)\b',
        'through our official support channels', text,
    )
    text = re.sub(
        r'(?i)\b(whatsapp|telegram|viber|imo|signal|messenger)\b',
        'our official support channels', text,
    )
    text = re.sub(r'https?://\S+', 'our official app or website', text)
    text = re.sub(
        r'(?i)\b(call|dial|text|sms|message|contact)\b([^.!?\n]{0,30})\+?\d[\d\s\-]{6,}\d',
        r'\1 our official helpline', text,
    )
    return text


def apply_safety_guardrails(customer_reply: str) -> str:
    if _has_credential_request(customer_reply):
        logger.warning("Safety violation: credential request in customer_reply — scrubbing")
        customer_reply = re.sub(
            r'(?i)(?<!do not )(?<!don\'t )(?<!never )(please\s+)?(share|provide|send|give|enter|type|input)[^.!?]{0,60}(pin|otp|password|card number|full card)[^.!?]*[.!?]?',
            "Please do not share your PIN, OTP, or password with anyone.",
            customer_reply,
        )

    if _has_refund_promise(customer_reply):
        logger.warning("Safety violation: unauthorized refund promise in customer_reply — scrubbing")
        customer_reply = re.sub(r'(?i)we will refund( you)?', 'any eligible amount will be returned through official channels', customer_reply)
        customer_reply = re.sub(r'(?i)we will reverse the transaction', 'our team will review the transaction', customer_reply)
        customer_reply = re.sub(r'(?i)your money will be (refunded|returned)', 'any eligible amount will be returned through official channels', customer_reply)
        customer_reply = re.sub(r'(?i)you will (get|receive)[^.]{0,30}back', 'any eligible amount will be returned through official channels', customer_reply)
        customer_reply = re.sub(r'(?i)we will credit your account', 'our team will process your case through official channels', customer_reply)
        customer_reply = re.sub(r'(?i)funds? will be (restored|credited|returned|refunded)', 'any eligible amount will be returned through official channels', customer_reply)
        customer_reply = re.sub(r'(?i)you will receive (a |the |your )?(full |partial )?refund', 'any eligible amount will be returned through official channels', customer_reply)
        customer_reply = re.sub(r'(?i)your account will be credited', 'our team will process your case through official channels', customer_reply)

    if _has_time_commitment(customer_reply):
        logger.warning("Safety: time commitment in customer_reply — scrubbing")
        customer_reply = re.sub(
            r'(?i)\bwithin \d+[\s\-]*(to[\s\-]*\d+\s*)?(hours?|days?|business days?|working days?)\b',
            'as soon as possible',
            customer_reply,
        )
        customer_reply = re.sub(
            r'(?i)\b(by|before)\s+(tomorrow|end of day|eod|midnight)\b',
            'through official channels',
            customer_reply,
        )
        customer_reply = re.sub(
            r'(?i)\bin the next \d+\s*(hours?|days?)\b',
            'as soon as possible',
            customer_reply,
        )

    if _has_third_party(customer_reply):
        logger.warning("Safety violation: third-party redirection in customer_reply — scrubbing")
        customer_reply = _scrub_third_party(customer_reply)

    return customer_reply


def scrub_action(action: str) -> str:
    """Scrub recommended_next_action.

    Per Problem Statement Section 8, the refund/reversal-confirmation rule is
    checked on BOTH customer_reply AND recommended_next_action. Internal ops
    language such as 'initiate the reversal flow per policy' is legitimate and is
    NOT matched by the customer-facing promise patterns; only explicit promises
    like 'we will refund the customer' are scrubbed.
    """
    if not action:
        return action
    if _has_refund_promise(action):
        logger.warning("Safety violation: refund promise in recommended_next_action — scrubbing")
        action = re.sub(r'(?i)we will refund( you| the customer)?', 'recommend reviewing the case for any eligible reversal through official channels', action)
        action = re.sub(r'(?i)we will reverse the transaction', 'review the transaction for an eligible reversal', action)
        action = re.sub(r'(?i)your money will be (refunded|returned)', 'any eligible amount will be returned through official channels', action)
        action = re.sub(r'(?i)we will credit your account', 'process the case for any eligible credit through official channels', action)
        action = re.sub(r'(?i)funds? will be (restored|credited|returned|refunded)', 'any eligible amount will be returned through official channels', action)
    return action


def has_safety_violation(customer_reply: str) -> bool:
    return (
        _has_credential_request(customer_reply)
        or _has_refund_promise(customer_reply)
        or _has_third_party(customer_reply)
        or has_bangla_violation(customer_reply)
    )


def validate_enums(result: dict) -> dict:
    if result.get("evidence_verdict") not in VALID_EVIDENCE_VERDICTS:
        logger.warning(f"Invalid evidence_verdict: {result.get('evidence_verdict')!r}, defaulting to insufficient_data")
        result["evidence_verdict"] = "insufficient_data"
    if result.get("case_type") not in VALID_CASE_TYPES:
        logger.warning(f"Invalid case_type: {result.get('case_type')!r}, defaulting to other")
        result["case_type"] = "other"
    if result.get("severity") not in VALID_SEVERITIES:
        logger.warning(f"Invalid severity: {result.get('severity')!r}, defaulting to medium")
        result["severity"] = "medium"
    if result.get("department") not in VALID_DEPARTMENTS:
        logger.warning(f"Invalid department: {result.get('department')!r}, defaulting to customer_support")
        result["department"] = "customer_support"
    return result
