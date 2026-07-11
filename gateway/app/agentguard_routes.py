"""HTTP routes for AgentGuard control plane."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import agentguard
from app.models import ApiResponse

router = APIRouter(prefix="/api/agentguard", tags=["agentguard"])


class EnsureAgentRequest(BaseModel):
    wallet_address: str = Field(..., min_length=32, max_length=64)


class EvaluateRequest(BaseModel):
    wallet_address: str = Field(..., min_length=32, max_length=64)
    action: str = "refund"
    amount_inr: int = Field(..., ge=0)
    resource_id: str = Field(..., min_length=1)


class ConsumeApprovalRequest(BaseModel):
    wallet_address: str = Field(..., min_length=32, max_length=64)
    approval_id: str


class PauseRequest(BaseModel):
    wallet_address: str = Field(..., min_length=32, max_length=64)


@router.post("/agents/ensure", response_model=ApiResponse)
async def ensure_agent(body: EnsureAgentRequest) -> ApiResponse:
    agent, policy = agentguard.ensure_seller_ops_agent(body.wallet_address)
    return ApiResponse(
        success=True,
        message="Store Operations Assistant ready",
        data={"agent": agent.model_dump(), "policy": policy.model_dump()},
    )


@router.get("/wallets/{wallet_address}", response_model=ApiResponse)
async def get_wallet_agentguard(wallet_address: str) -> ApiResponse:
    agent = agentguard.get_agent_for_wallet(wallet_address)
    if not agent:
        agent, policy = agentguard.ensure_seller_ops_agent(wallet_address)
    else:
        policy = agentguard.get_policy(agent.policy_id or "")
    receipts = [
        r.model_dump() for r in agentguard.list_receipts_for_wallet(wallet_address)[:20]
    ]
    return ApiResponse(
        success=True,
        message="AgentGuard status",
        data={
            "agent": agent.model_dump() if agent else None,
            "policy": policy.model_dump() if policy else None,
            "receipts": receipts,
        },
    )


@router.post("/actions/evaluate", response_model=ApiResponse)
async def evaluate_action(body: EvaluateRequest) -> ApiResponse:
    result = agentguard.evaluate_action(
        wallet_address=body.wallet_address,
        action=body.action,
        amount_inr=body.amount_inr,
        resource_id=body.resource_id,
    )
    return ApiResponse(
        success=True,
        message=result.get("reason") or "Evaluated",
        data=result,
    )


@router.post("/approvals/consume", response_model=ApiResponse)
async def consume_approval(body: ConsumeApprovalRequest) -> ApiResponse:
    try:
        result = agentguard.consume_approval(
            approval_id=body.approval_id,
            wallet_address=body.wallet_address,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except agentguard.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(
        success=True,
        message="Approval consumed",
        data=result,
    )


@router.post("/agents/{agent_id}/pause", response_model=ApiResponse)
async def pause_agent(agent_id: str, body: PauseRequest) -> ApiResponse:
    agent = agentguard.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Unknown agent")
    if agent.wallet_address != body.wallet_address:
        raise HTTPException(status_code=403, detail="Wallet mismatch")
    paused = agentguard.pause_agent(agent_id)
    return ApiResponse(
        success=True,
        message="Agent paused",
        data={"agent": paused.model_dump()},
    )


@router.post("/agents/{agent_id}/resume", response_model=ApiResponse)
async def resume_agent(agent_id: str, body: PauseRequest) -> ApiResponse:
    agent = agentguard.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Unknown agent")
    if agent.wallet_address != body.wallet_address:
        raise HTTPException(status_code=403, detail="Wallet mismatch")
    resumed = agentguard.resume_agent(agent_id)
    return ApiResponse(
        success=True,
        message="Agent resumed",
        data={"agent": resumed.model_dump()},
    )


@router.get("/receipts/{receipt_id}", response_model=ApiResponse)
async def get_receipt(receipt_id: str) -> ApiResponse:
    receipt = agentguard.get_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Unknown receipt")
    # PII-free by construction
    return ApiResponse(
        success=True,
        message="Receipt",
        data={"receipt": receipt.model_dump()},
    )
