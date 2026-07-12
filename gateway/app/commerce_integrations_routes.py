"""PSP / logistics / IGM scaffolds under AgentGuard (prod readiness P4).

Real UPI/PSP and ONDC logistics/IGM wire after staging Beckn works.
These routes define the contract AgentGuard checkout/refund will call.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/commerce-integrations", tags=["commerce-integrations"])


class PaymentIntentBody(BaseModel):
    amount_inr: float = Field(..., gt=0)
    currency: str = "INR"
    order_id: str
    principal_id: Optional[str] = None
    agentguard_receipt_id: Optional[str] = None
    provider: str = Field(default="upi_psp_stub", description="PSP adapter id")


class LogisticsTransitionBody(BaseModel):
    order_id: str
    to_state: str
    tracking_id: Optional[str] = None
    note: Optional[str] = None


class IssueBody(BaseModel):
    order_id: str
    category: str = "ORDER"
    description: str
    principal_id: Optional[str] = None


@router.get("/status")
async def integrations_status() -> JSONResponse:
    return JSONResponse(
        {
            "success": True,
            "data": {
                "payments": {
                    "provider": "upi_psp_stub",
                    "live": False,
                    "note": "Choose regulated PSP; map AgentGuard checkout_commit → create intent.",
                },
                "logistics": {
                    "provider": "stub",
                    "live": False,
                    "note": "Seller transitions call this after AG allow.",
                },
                "igm": {
                    "provider": "stub",
                    "live": False,
                    "note": "Issue/grievance (ONDC IGM) after network orders exist.",
                },
            },
        }
    )


@router.post("/payments/intents")
async def create_payment_intent(body: PaymentIntentBody) -> JSONResponse:
    if not body.agentguard_receipt_id:
        raise HTTPException(
            status_code=400,
            detail="agentguard_receipt_id required — AgentGuard must authorize payment first.",
        )
    intent_id = f"pi_{uuid.uuid4().hex[:16]}"
    return JSONResponse(
        {
            "success": True,
            "data": {
                "intent_id": intent_id,
                "status": "requires_action",
                "amount_inr": body.amount_inr,
                "currency": body.currency,
                "order_id": body.order_id,
                "provider": body.provider,
                "agentguard_receipt_id": body.agentguard_receipt_id,
                "created_at": int(time.time()),
                "live": False,
                "note": "Stub intent — wire real UPI/PSP SDK here for production.",
            },
        }
    )


@router.post("/logistics/transitions")
async def logistics_transition(body: LogisticsTransitionBody) -> JSONResponse:
    return JSONResponse(
        {
            "success": True,
            "data": {
                "order_id": body.order_id,
                "to_state": body.to_state,
                "tracking_id": body.tracking_id or f"trk_{uuid.uuid4().hex[:10]}",
                "live": False,
                "note": "Stub logistics transition.",
            },
        }
    )


@router.post("/igm/issues")
async def create_issue(body: IssueBody) -> JSONResponse:
    issue_id = f"iss_{uuid.uuid4().hex[:12]}"
    return JSONResponse(
        {
            "success": True,
            "data": {
                "issue_id": issue_id,
                "order_id": body.order_id,
                "category": body.category,
                "status": "OPEN",
                "live": False,
                "note": "Stub IGM issue — wire ONDC issue protocol after network orders.",
            },
        }
    )
