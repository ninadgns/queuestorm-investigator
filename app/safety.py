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


def _has_credential_request(text: str) -> bool:
    lower = text.lower()
    for pattern in _CREDENTIAL_REQUEST_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


def _has_refund_promise(text: str) -> bool:
    lower = text.lower()
    for pattern in _REFUND_PROMISE_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


def _has_time_commitment(text: str) -> bool:
    lower = text.lower()
    for pattern in _TIME_COMMITMENT_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


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

    return customer_reply


def has_safety_violation(customer_reply: str) -> bool:
    return _has_credential_request(customer_reply) or _has_refund_promise(customer_reply)


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
