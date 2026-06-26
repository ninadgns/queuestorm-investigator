import logging
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

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


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}


@app.post("/analyze-ticket")
async def analyze_ticket_endpoint(request: Request):
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
