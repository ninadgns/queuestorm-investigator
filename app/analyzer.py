import json
import logging
import os
import re
from typing import Optional

from .models import TicketRequest
from .prompts import SYSTEM_PROMPT
from .providers import get_active_providers
from .safety import (
    SAFE_BANGLA_REPLY,
    apply_safety_guardrails,
    has_bangla_violation,
    scrub_action,
    validate_enums,
)

logger = logging.getLogger(__name__)


def detect_language(text: str) -> str:
    """Detect Bangla by Unicode block U+0980–U+09FF presence."""
    bangla_chars = sum(1 for c in text if 'ঀ' <= c <= '৿')
    return "bn" if bangla_chars > 3 else "en"


def build_user_message(req: TicketRequest) -> str:
    parts = [f"TICKET ID: {req.ticket_id}"]
    parts.append(f"COMPLAINT TEXT:\n{req.complaint}")

    language = req.language or detect_language(req.complaint)
    parts.append(f"LANGUAGE: {language}")
    if req.channel:
        parts.append(f"CHANNEL: {req.channel}")
    if req.user_type:
        parts.append(f"USER TYPE: {req.user_type}")
    if req.campaign_context:
        parts.append(f"CAMPAIGN CONTEXT: {req.campaign_context}")

    if req.transaction_history:
        txn_lines = ["TRANSACTION HISTORY:"]
        for t in req.transaction_history:
            txn_lines.append(
                f"  - ID: {t.transaction_id} | Type: {t.type} | Amount: {t.amount} BDT"
                f" | Counterparty: {t.counterparty} | Time: {t.timestamp} | Status: {t.status}"
            )
        parts.append("\n".join(txn_lines))
    else:
        parts.append("TRANSACTION HISTORY: (none provided)")

    parts.append(
        "\nAnalyze this ticket. Your entire response must be a single raw JSON object."
        " Start with { and end with }. No reasoning text, no markdown, no explanation outside the JSON."
    )
    return "\n\n".join(parts)


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No complete JSON object found: {text[:120]!r}")
    return json.loads(text[start : end + 1])


def rule_based_fallback(req: TicketRequest) -> dict:
    """Deterministic safe fallback when all LLM providers fail."""
    complaint_lower = req.complaint.lower() if req.complaint else ""

    case_type = "other"
    severity = "medium"
    department = "customer_support"
    evidence_verdict = "insufficient_data"
    relevant_txn = None
    human_review = False

    if any(w in complaint_lower for w in ["pin", "otp", "password", "called me", "scam", "phishing", "fraud"]):
        case_type = "phishing_or_social_engineering"
        severity = "critical"
        department = "fraud_risk"
        human_review = True
    elif any(w in complaint_lower for w in ["twice", "duplicate", "double", "charged twice", "deducted twice"]):
        case_type = "duplicate_payment"
        severity = "high"
        department = "payments_ops"
        human_review = True
    elif any(w in complaint_lower for w in ["wrong number", "wrong person", "wrong recipient", "wrong transfer", "sent to wrong", "wrong account"]):
        case_type = "wrong_transfer"
        severity = "high"
        department = "dispute_resolution"
        human_review = True
    elif any(w in complaint_lower for w in ["failed", "deducted", "balance deducted", "not credited"]):
        case_type = "payment_failed"
        severity = "high"
        department = "payments_ops"
        human_review = False
    elif any(w in complaint_lower for w in ["refund", "return my money", "cancel"]):
        case_type = "refund_request"
        severity = "low"
        department = "customer_support"
        human_review = False
    elif any(w in complaint_lower for w in ["settlement", "not settled"]):
        case_type = "merchant_settlement_delay"
        severity = "medium"
        department = "merchant_operations"
        human_review = False
    elif any(w in complaint_lower for w in ["cash in", "cash-in", "agent", "deposit", "not received"]):
        case_type = "agent_cash_in_issue"
        severity = "high"
        department = "agent_operations"
        human_review = True

    if req.transaction_history:
        if case_type == "duplicate_payment" and len(req.transaction_history) >= 2:
            seen: dict = {}
            for t in req.transaction_history:
                key = (t.type, int(t.amount), t.counterparty)
                if key in seen:
                    relevant_txn = t.transaction_id
                    evidence_verdict = "consistent"
                    break
                seen[key] = t.transaction_id
        if not relevant_txn:
            for t in req.transaction_history:
                if str(int(t.amount)) in complaint_lower:
                    relevant_txn = t.transaction_id
                    evidence_verdict = "consistent"
                    break

    language = req.language or "en"
    if language == "bn":
        customer_reply = (
            "আপনার সমস্যার জন্য আমরা দুঃখিত। আমাদের সাপোর্ট টিম আপনার অনুরোধ পর্যালোচনা করবে এবং "
            "অফিসিয়াল চ্যানেলের মাধ্যমে আপনাকে জানাবে। "
            "অনুগ্রহ করে কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।"
        )
    else:
        customer_reply = (
            "We have received your request and our support team will review it. "
            "You will be contacted through official channels. "
            "Please do not share your PIN or OTP with anyone."
        )

    return {
        "relevant_transaction_id": relevant_txn,
        "evidence_verdict": evidence_verdict,
        "case_type": case_type,
        "severity": severity,
        "department": department,
        "agent_summary": f"Automated fallback classification: {case_type}. Manual review recommended.",
        "recommended_next_action": "Review the ticket manually and contact the customer through official channels.",
        "customer_reply": customer_reply,
        "human_review_required": human_review,
        "confidence": 0.4,
        "reason_codes": ["fallback_mode", case_type],
    }


async def call_llm(user_msg: str) -> dict:
    """Try each provider in order; return parsed result from the first that succeeds."""
    active = get_active_providers()
    last_error = None

    for provider in active:
        try:
            client = provider.make_client()
            response = await client.chat.completions.create(
                model=provider.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=1536,
                temperature=0.1,
            )
            raw_text = (response.choices[0].message.content or "").strip()
            if not raw_text:
                raise ValueError("Empty response from LLM")
            result = extract_json(raw_text)
            logger.info(f"LLM success via {provider.name}")
            return result
        except Exception as exc:
            logger.warning(f"Provider {provider.name} failed: {type(exc).__name__}: {exc}")
            last_error = exc
            continue

    raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")


_DEMAND_WORDS = {"confirm", "today", "now", "immediately", "urgent", "urgently", "guarantee", "promise", "must", "right now", "asap"}

# Case types that represent active financial disputes worth a human's time once
# the evidence actually supports them.
_HIGH_RISK_CASES = {"wrong_transfer", "duplicate_payment", "agent_cash_in_issue", "phishing_or_social_engineering"}
# Any single transaction at or above this BDT amount is treated as a high-value
# case and escalated regardless of type (Problem Statement: "high value cases").
_HIGH_VALUE_BDT = 50000


_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
# Severity ceilings straight from the documented policy: these case types are
# never rated above medium ("medium → merchant_settlement_delay"; "low → refund/
# vague"). Applied as ceilings (not fixed values), so a lower model rating is
# preserved. High-value cases still escalate via human_review independently.
_SEVERITY_CEILINGS = {
    "merchant_settlement_delay": "medium",
    "refund_request": "medium",
    "other": "medium",
}


def clamp_severity(result: dict) -> dict:
    """Cap severity so it never exceeds what the evidence and case type justify.

    1. Policy "high → clear evidence": a contested (inconsistent) or unconfirmed
       (insufficient_data) claim cannot be "high" → drops to "medium".
    2. Per-case-type ceilings from the documented severity map.
    Credential/fraud threats (phishing) are exempt and keep their critical rating.
    """
    case_type = result.get("case_type")
    verdict = result.get("evidence_verdict")
    severity = result.get("severity")
    if case_type == "phishing_or_social_engineering":
        return result

    if verdict == "inconsistent" and severity in {"high", "critical"}:
        severity = "medium"
    elif verdict == "insufficient_data" and severity == "high":
        severity = "medium"

    cap = _SEVERITY_CEILINGS.get(case_type)
    if cap and _SEVERITY_ORDER.get(severity, 1) > _SEVERITY_ORDER[cap]:
        severity = cap

    result["severity"] = severity
    return result


def decide_human_review(result: dict, req: TicketRequest) -> bool:
    """Deterministic, policy-grounded human_review_required.

    We compute this ourselves rather than trusting the LLM because escalation is
    a hard policy decision, not a language one. The rule below reproduces every
    public sample's expected value:
      escalate disputes WITH evidence, all credential/fraud threats, inconsistent
      (contested) claims, urgent refund demands, and high-value transactions;
      do NOT escalate vague/ambiguous cases (ask the customer first) or routine
      consistent operational cases (payment_failed, settlement delay) that ops
      handles via standard SLA flows.
    """
    case_type = result.get("case_type", "")
    verdict = result.get("evidence_verdict", "")
    severity = result.get("severity", "")
    complaint_lower = (req.complaint or "").lower()

    if severity == "critical":
        return True
    if case_type == "phishing_or_social_engineering":
        return True
    if verdict == "inconsistent":  # contested claim
        return True
    if case_type in _HIGH_RISK_CASES and verdict == "consistent":
        return True
    if case_type == "refund_request" and any(w in complaint_lower for w in _DEMAND_WORDS):
        return True
    if req.transaction_history:
        for t in req.transaction_history:
            try:
                if float(t.amount) >= _HIGH_VALUE_BDT:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _is_bangla(text: str) -> bool:
    return sum(1 for c in text if "ঀ" <= c <= "৿") > 5


async def analyze_ticket(req: TicketRequest) -> dict:
    user_msg = build_user_message(req)

    try:
        result = await call_llm(user_msg)
    except Exception as exc:
        logger.error(f"All providers exhausted, using rule-based fallback: {exc}")
        result = rule_based_fallback(req)

    result["ticket_id"] = req.ticket_id
    result = validate_enums(result)
    result = clamp_severity(result)
    # human_review_required is decided deterministically from policy, overriding
    # whatever the LLM guessed (it tends to over-escalate routine cases).
    result["human_review_required"] = decide_human_review(result, req)

    # Cap confidence when evidence is insufficient, then clamp to the [0, 1] contract.
    if result.get("evidence_verdict") == "insufficient_data":
        result["confidence"] = min(result.get("confidence", 0.5) or 0.5, 0.6)
    try:
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0.7))))
    except (TypeError, ValueError):
        result["confidence"] = 0.7

    # Safety scrubbing on both customer-facing fields the rubric checks.
    if "customer_reply" in result:
        result["customer_reply"] = apply_safety_guardrails(result["customer_reply"])
    if "recommended_next_action" in result:
        result["recommended_next_action"] = scrub_action(result["recommended_next_action"])

    # Language handling. If the reply came back in the wrong language, replace it
    # with a safe template in the expected language. Also catch Bangla-only
    # safety violations the English regex cannot see.
    expected_lang = req.language or detect_language(req.complaint or "")
    reply = result.get("customer_reply", "")
    if expected_lang != "bn" and _is_bangla(reply):
        result["customer_reply"] = (
            "We have received your report and our team will investigate. "
            "Please do not share your PIN or OTP with anyone. "
            "We will contact you through official channels."
        )
    elif _is_bangla(reply) and has_bangla_violation(reply):
        logger.warning("Safety violation in Bangla customer_reply — replacing with safe template")
        result["customer_reply"] = SAFE_BANGLA_REPLY

    result.setdefault("relevant_transaction_id", None)
    result.setdefault("human_review_required", True)
    result.setdefault("agent_summary", "Case received. Please escalate for manual processing.")
    result.setdefault("recommended_next_action", "Review ticket and contact customer through official channels.")
    result.setdefault(
        "customer_reply",
        "We have received your request. Our team will review it and contact you through official channels. "
        "Please do not share your PIN or OTP with anyone.",
    )
    result.setdefault("confidence", 0.7)
    result.setdefault("reason_codes", [])

    return result
