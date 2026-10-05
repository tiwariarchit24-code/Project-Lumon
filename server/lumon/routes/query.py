"""
Query routes: parse a sentence into a plan, and run a (possibly edited) plan.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from .. import audit, db
from ..query import engine, parser

router = APIRouter(prefix="/api/query")


class ParseRequest(BaseModel):
    text: str


class RunRequest(BaseModel):
    plan: dict


@router.post("/parse")
def parse(request: ParseRequest):
    """Sentence -> structured plan (deterministic, no language model)."""
    return parser.parse(request.text)


@router.post("/run")
def run(request: RunRequest):
    """Run a plan and log the query in the audit trail."""
    result = engine.run(request.plan)
    connection = db.connect()
    audit.record(connection, "analyst", "query", None, {"text": request.plan.get("text"), "intent": request.plan.get("intent"),
                                                          "results": result["count"], "supported": result["capability"]["supported"]})
    connection.close()
    return result
