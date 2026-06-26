import logging
import os
import time
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .models import TicketRequest, TicketResponse
from .analyzer import analyze_ticket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="QueueStorm Investigator",
    description="AI-powered support ticket investigator for digital finance platforms",
    version="1.0.0",
)

# Simple in-memory sliding-window rate limiter (30 req/min per IP)
_rate_store: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 30
_RATE_WINDOW = 60.0


def _check_rate(ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW
    times = [t for t in _rate_store[ip] if t > cutoff]
    if len(times) >= _RATE_LIMIT:
        _rate_store[ip] = times
        return False
    times.append(now)
    _rate_store[ip] = times
    return True


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}


@app.post("/analyze-ticket")
async def analyze_ticket_endpoint(request: Request):
    # Rate limit
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate(client_ip):
        return JSONResponse(
            status_code=429,
            content={"error": "Rate limit exceeded. Maximum 30 requests per minute."},
        )

    # Parse JSON body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON in request body"},
        )

    # Validate required fields
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "Request body must be a JSON object"})

    if "ticket_id" not in body or not body.get("ticket_id"):
        return JSONResponse(status_code=400, content={"error": "Missing required field: ticket_id"})

    if "complaint" not in body:
        return JSONResponse(status_code=400, content={"error": "Missing required field: complaint"})

    complaint = body.get("complaint", "")
    if not complaint.strip():
        return JSONResponse(status_code=422, content={"error": "Field 'complaint' must not be empty"})
    if len(complaint) > 5000:
        return JSONResponse(status_code=422, content={"error": "Field 'complaint' exceeds maximum length of 5000 characters"})

    # Parse and validate schema
    try:
        ticket_req = TicketRequest(**body)
    except ValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid request schema", "details": exc.errors()},
        )

    # Run analysis
    try:
        result = await analyze_ticket(ticket_req)
        validated = TicketResponse.model_validate(result)
        return JSONResponse(status_code=200, content=validated.model_dump())
    except Exception as exc:
        logger.exception("Unexpected error during ticket analysis")
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error. Please try again."},
        )
