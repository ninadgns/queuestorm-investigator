import json
import logging
import os
import re
from typing import Optional

from .models import TicketRequest
from .prompts import SYSTEM_PROMPT
from .providers import PROVIDERS, get_active_providers
from .safety import apply_safety_guardrails, validate_enums

logger = logging.getLogger(__name__)


def build_user_message(req: TicketRequest) -> str:
    parts = [f"TICKET ID: {req.ticket_id}"]
    parts.append(f"COMPLAINT TEXT:\n{req.complaint}")

    if req.language:
        parts.append(f"LANGUAGE: {req.language}")
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


async def analyze_ticket(req: TicketRequest) -> dict:
    user_msg = build_user_message(req)

    try:
        result = await call_llm(user_msg)
    except Exception as exc:
        logger.error(f"All providers exhausted, using rule-based fallback: {exc}")
        result = rule_based_fallback(req)

    result["ticket_id"] = req.ticket_id
    result = validate_enums(result)

    if "customer_reply" in result:
        result["customer_reply"] = apply_safety_guardrails(result["customer_reply"])

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
