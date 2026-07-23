"""HTTP routes for AgentGuard control plane."""

from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import agentguard
from app.agentguard_contract import principal_id_from_wallet
from app.checkout_orchestrator import CheckoutOrchestrator
from app.models import ApiResponse
from app.persistence import ConnectionPool
from app.persistence.agentguard_repository import (
    AgentGuardConflict,
    AgentGuardNotFound,
    AgentGuardPermissionDenied,
)
from app.seller_agentguard_orchestrator import SellerAgentGuardOrchestrator
from app.session_auth import SESSION_COOKIE_NAME, parse_session_token

router = APIRouter(prefix="/api/agentguard", tags=["agentguard"])

Role = Literal["buyer", "seller"]


class EnsureAgentRequest(BaseModel):
    wallet_address: Optional[str] = Field(None, min_length=32, max_length=64)
    role: Role = "seller"


class AgentCreateRequest(BaseModel):
    wallet_address: Optional[str] = Field(None, min_length=32, max_length=64)
    role: Role
    name: Optional[str] = None


class EvaluateRequest(BaseModel):
    wallet_address: Optional[str] = Field(None, min_length=32, max_length=64)
    agent_id: Optional[str] = None
    action: str = "refund"
    amount_inr: int = Field(..., ge=0)
    resource_id: str = Field(..., min_length=1)
    counterparty_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ConsumeApprovalRequest(BaseModel):
    wallet_address: Optional[str] = Field(None, min_length=32, max_length=64)
    approval_id: Optional[str] = None
    action: Optional[str] = None
    amount_inr: Optional[int] = None
    resource_id: Optional[str] = None
    request_hash: Optional[str] = None


class PauseRequest(BaseModel):
    wallet_address: Optional[str] = Field(None, min_length=32, max_length=64)


class CompileMandateRequest(BaseModel):
    wallet_address: Optional[str] = Field(None, min_length=32, max_length=64)
    role: Role
    template: Optional[str] = None
    limits: dict[str, Any] = Field(default_factory=dict)
    allowed_actions: Optional[list[str]] = None
    agent_id: Optional[str] = None


class ExecuteRequest(BaseModel):
    wallet_address: Optional[str] = Field(None, min_length=32, max_length=64)
    agent_id: Optional[str] = None
    approval_id: Optional[str] = None
    decision_id: Optional[str] = None
    action: str
    amount_inr: int = Field(0, ge=0)
    resource_id: str = Field(..., min_length=1)
    idempotency_key: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ReceiptVerifyRequest(BaseModel):
    receipt_id: Optional[str] = None
    receipt: Optional[dict[str, Any]] = None


def _principal(
    request: Request,
    *,
    wallet_address: Optional[str],
    role: Optional[Role] = None,
) -> tuple[str, Optional[str]]:
    """Resolve authorization principal from session first; body wallet is legacy only."""
    del role  # reserved for future audience checks
    session = parse_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))
    if session:
        session_wallet = session.get("wallet_address")
        principal_id = session.get("principal_id")
        if not principal_id and session_wallet:
            principal_id = principal_id_from_wallet(session_wallet)
        if not principal_id:
            raise HTTPException(
                status_code=401, detail="AgentGuard principal required."
            )
        if wallet_address and session_wallet and wallet_address != session_wallet:
            raise HTTPException(
                status_code=403, detail="Wallet does not match session principal."
            )
        if wallet_address and not session_wallet:
            # Social/demo session: callers must not select another principal via body wallet.
            raise HTTPException(
                status_code=403, detail="Body wallet cannot override session principal."
            )
        return str(principal_id), session_wallet if isinstance(
            session_wallet, str
        ) else None
    if wallet_address:
        # Legacy pytest / fixture path without cookie — maps to wallet:* principal.
        return principal_id_from_wallet(wallet_address), wallet_address
    raise HTTPException(status_code=401, detail="AgentGuard principal required.")


def _assert_agent_principal(agent_id: str, principal_id: str) -> agentguard.AgentRecord:
    agent = agentguard.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Unknown agent")
    if agent.principal_id != principal_id:
        raise HTTPException(status_code=403, detail="Principal mismatch")
    return agent


def _persistence_pool(request: Request) -> ConnectionPool | None:
    pool = getattr(request.app.state, "persistence_pool", None)
    return pool if isinstance(pool, ConnectionPool) and pool.is_open else None


def _raise_persistent_error(error: Exception) -> None:
    if isinstance(error, AgentGuardNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, AgentGuardPermissionDenied):
        raise HTTPException(status_code=403, detail=str(error)) from error
    if isinstance(error, AgentGuardConflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, ValueError):
        raise HTTPException(status_code=422, detail=str(error)) from error
    raise error


@router.post("/agents/ensure", response_model=ApiResponse)
async def ensure_agent(request: Request, body: EnsureAgentRequest) -> ApiResponse:
    principal_id, wallet_address = _principal(
        request, wallet_address=body.wallet_address, role=body.role
    )
    pool = _persistence_pool(request)
    if pool is not None:
        try:
            orchestrator = (
                CheckoutOrchestrator(pool)
                if body.role == "buyer"
                else SellerAgentGuardOrchestrator(pool)
            )
            result = await orchestrator.ensure_agent(principal_id=principal_id)
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(success=True, message="AgentGuard agent ready", data=result)
    if body.role == "seller" and wallet_address:
        agent, policy = agentguard.ensure_seller_ops_agent(wallet_address)
        mandate = agentguard.get_mandate(agent.mandate_id or "")
    else:
        agent, mandate, policy = agentguard.ensure_agent(
            principal_id=principal_id,
            role=body.role,
            wallet_address=wallet_address,
        )
    return ApiResponse(
        success=True,
        message="AgentGuard agent ready",
        data={
            "agent": agent.model_dump(),
            "policy": policy.model_dump(),
            "mandate": mandate.model_dump() if mandate else None,
        },
    )


@router.post("/agents", response_model=ApiResponse)
async def create_agent(request: Request, body: AgentCreateRequest) -> ApiResponse:
    principal_id, wallet_address = _principal(
        request, wallet_address=body.wallet_address, role=body.role
    )
    pool = _persistence_pool(request)
    if pool is not None:
        orchestrator = (
            CheckoutOrchestrator(pool)
            if body.role == "buyer"
            else SellerAgentGuardOrchestrator(pool)
        )
        result = await orchestrator.ensure_agent(principal_id=principal_id)
        return ApiResponse(success=True, message="AgentGuard agent ready", data=result)
    agent, mandate, policy = agentguard.ensure_agent(
        principal_id=principal_id,
        role=body.role,
        wallet_address=wallet_address,
        name=body.name,
    )
    return ApiResponse(
        success=True,
        message="AgentGuard agent ready",
        data={
            "agent": agent.model_dump(),
            "mandate": mandate.model_dump(),
            "policy": policy.model_dump(),
        },
    )


@router.get("/agents/current", response_model=ApiResponse)
async def get_current_agent(
    request: Request, role: Role, wallet_address: Optional[str] = None
) -> ApiResponse:
    principal_id, wallet = _principal(request, wallet_address=wallet_address, role=role)
    pool = _persistence_pool(request)
    if pool is not None:
        try:
            orchestrator = (
                CheckoutOrchestrator(pool)
                if role == "buyer"
                else SellerAgentGuardOrchestrator(pool)
            )
            result = await orchestrator.ensure_agent(principal_id=principal_id)
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(
            success=True, message="Current AgentGuard agent", data=result
        )
    agent = agentguard.get_current_agent(principal_id, role)
    if not agent:
        agent, mandate, policy = agentguard.ensure_agent(
            principal_id=principal_id,
            role=role,
            wallet_address=wallet,
        )
    else:
        mandate = agentguard.get_mandate(agent.mandate_id or "")
        policy = agentguard.get_policy(agent.policy_id or "")
    receipts = [
        r.model_dump()
        for r in agentguard.list_receipts_for_principal(principal_id)[:20]
    ]
    return ApiResponse(
        success=True,
        message="Current AgentGuard agent",
        data={
            "agent": agent.model_dump() if agent else None,
            "mandate": mandate.model_dump() if mandate else None,
            "policy": policy.model_dump() if policy else None,
            "receipts": receipts,
        },
    )


@router.get("/wallets/{wallet_address}", response_model=ApiResponse)
async def get_wallet_agentguard(wallet_address: str, request: Request) -> ApiResponse:
    if _persistence_pool(request) is not None:
        raise HTTPException(
            status_code=409,
            detail="Wallet AgentGuard compatibility is unavailable in PostgreSQL mode.",
        )
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


@router.post("/mandates/compile", response_model=ApiResponse)
async def compile_mandate(request: Request, body: CompileMandateRequest) -> ApiResponse:
    principal_id, wallet_address = _principal(
        request, wallet_address=body.wallet_address, role=body.role
    )
    pool = _persistence_pool(request)
    if pool is not None:
        try:
            if body.role == "buyer":
                result = await CheckoutOrchestrator(pool).compile_mandate(
                    principal_id=principal_id,
                    limits=body.limits,
                    allowed_actions=body.allowed_actions,
                )
            else:
                result = await SellerAgentGuardOrchestrator(pool).compile_mandate(
                    principal_id=principal_id,
                    limits=body.limits,
                    allowed_actions=body.allowed_actions,
                )
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(
            success=True,
            message="Mandate compiled; confirmation required",
            data=result,
        )
    template = body.template or (
        "buyer_shop_v1" if body.role == "buyer" else "seller_ops_v1"
    )
    mandate = agentguard.compile_mandate(
        template=template,
        role=body.role,
        limits=body.limits,
        allowed_actions=body.allowed_actions,
        principal_id=principal_id,
        wallet_address=wallet_address,
        agent_id=body.agent_id,
    )
    return ApiResponse(
        success=True, message="Mandate compiled", data={"mandate": mandate.model_dump()}
    )


@router.post("/mandates/{mandate_id}/confirm", response_model=ApiResponse)
async def confirm_mandate(
    mandate_id: str, request: Request, body: PauseRequest
) -> ApiResponse:
    principal_id, _wallet = _principal(request, wallet_address=body.wallet_address)
    pool = _persistence_pool(request)
    if pool is not None:
        try:
            orchestrator = (
                SellerAgentGuardOrchestrator(pool)
                if mandate_id.startswith("mandate_seller_")
                else CheckoutOrchestrator(pool)
            )
            result = await orchestrator.confirm_mandate(
                principal_id=principal_id, mandate_id=mandate_id
            )
            mandate = result["mandate"]
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(
            success=True,
            message="Mandate confirmed",
            data={"mandate": mandate},
        )
    try:
        mandate = agentguard.confirm_mandate(mandate_id, principal_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ApiResponse(
        success=True,
        message="Mandate confirmed",
        data={"mandate": mandate.model_dump()},
    )


@router.get("/mandates/{mandate_id}", response_model=ApiResponse)
async def get_mandate(mandate_id: str, request: Request) -> ApiResponse:
    pool = _persistence_pool(request)
    if pool is not None:
        principal_id, _wallet = _principal(request, wallet_address=None)
        try:
            orchestrator = (
                SellerAgentGuardOrchestrator(pool)
                if mandate_id.startswith("mandate_seller_")
                else CheckoutOrchestrator(pool)
            )
            result = await orchestrator.ensure_agent(principal_id=principal_id)
        except Exception as error:
            _raise_persistent_error(error)
        mandate = result["mandate"]
        if mandate is None or mandate["mandate_id"] != mandate_id:
            raise HTTPException(status_code=404, detail="Unknown mandate")
        return ApiResponse(success=True, message="Mandate", data={"mandate": mandate})
    mandate = agentguard.get_mandate(mandate_id)
    if not mandate:
        raise HTTPException(status_code=404, detail="Unknown mandate")
    return ApiResponse(
        success=True, message="Mandate", data={"mandate": mandate.model_dump()}
    )


@router.post("/actions/evaluate", response_model=ApiResponse)
async def evaluate_action(
    request: Request,
    response: Response,
    body: EvaluateRequest,
    correlation_id: Optional[str] = Header(default=None, alias="X-Correlation-ID"),
) -> ApiResponse:
    principal_id, wallet_address = _principal(
        request, wallet_address=body.wallet_address
    )
    pool = _persistence_pool(request)
    effective_correlation_id = correlation_id or f"correlation_{uuid4().hex}"
    response.headers["X-Correlation-ID"] = effective_correlation_id
    if (
        pool is not None
        and agentguard.normalize_action(body.action) == "buyer.checkout.commit"
        and body.payload.get("quote_id")
    ):
        quote_id = str(body.payload["quote_id"])
        try:
            result = await CheckoutOrchestrator(pool).evaluate_checkout(
                principal_id=principal_id,
                quote_id=quote_id,
                delivery_context=body.payload.get("delivery_context"),
            )
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(success=True, message=result["human_reason"], data=result)
    if pool is not None:
        normalized = agentguard.normalize_action(body.action)
        if normalized is None:
            raise HTTPException(
                status_code=422,
                detail="Unsupported protected action.",
            )
        try:
            if normalized.startswith("seller."):
                result = await SellerAgentGuardOrchestrator(pool).evaluate(
                    principal_id=principal_id,
                    action=normalized,
                    amount_inr=body.amount_inr,
                    resource_id=body.resource_id,
                    counterparty_id=body.counterparty_id,
                    payload=body.payload,
                    correlation_id=effective_correlation_id,
                )
            else:
                result = await CheckoutOrchestrator(pool).evaluate_protected_action(
                    principal_id=principal_id,
                    action=normalized,
                    amount_inr=body.amount_inr,
                    resource_id=body.resource_id,
                    counterparty_id=body.counterparty_id,
                    payload=body.payload,
                    correlation_id=effective_correlation_id,
                )
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(success=True, message=result["human_reason"], data=result)
    try:
        result = agentguard.evaluate_action(
            principal_id=principal_id,
            wallet_address=wallet_address,
            agent_id=body.agent_id,
            action=body.action,
            amount_inr=body.amount_inr,
            resource_id=body.resource_id,
            counterparty_id=body.counterparty_id,
            payload=body.payload,
        )
    except (KeyError, PermissionError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ApiResponse(
        success=True,
        message=result.get("reason") or "Evaluated",
        data=result,
    )


@router.post("/approvals/consume", response_model=ApiResponse)
async def consume_approval(
    request: Request, body: ConsumeApprovalRequest
) -> ApiResponse:
    approval_id = body.approval_id
    if not approval_id:
        raise HTTPException(status_code=422, detail="approval_id required")
    principal_id, wallet_address = _principal(
        request, wallet_address=body.wallet_address
    )
    if _persistence_pool(request) is not None:
        raise HTTPException(
            status_code=409,
            detail="Approval is consumed atomically by the protected execute call.",
        )
    return _consume_response(
        approval_id=approval_id,
        principal_id=principal_id,
        wallet_address=wallet_address,
        body=body,
        message="Approval consumed",
    )


@router.post("/approvals/{approval_id}/approve", response_model=ApiResponse)
async def approve_approval(
    approval_id: str, request: Request, body: ConsumeApprovalRequest
) -> ApiResponse:
    principal_id, wallet_address = _principal(
        request, wallet_address=body.wallet_address
    )
    if _persistence_pool(request) is not None:
        raise HTTPException(
            status_code=409,
            detail="Approval is consumed atomically by the protected execute call.",
        )
    return _consume_response(
        approval_id=approval_id,
        principal_id=principal_id,
        wallet_address=wallet_address,
        body=body,
        message="Approval approved",
    )


def _consume_response(
    *,
    approval_id: str,
    principal_id: str,
    wallet_address: Optional[str],
    body: ConsumeApprovalRequest,
    message: str,
) -> ApiResponse:
    try:
        result = agentguard.consume_approval(
            approval_id=approval_id,
            principal_id=principal_id,
            wallet_address=wallet_address,
            action=body.action,
            amount_inr=body.amount_inr,
            resource_id=body.resource_id,
            request_hash=body.request_hash,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except agentguard.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(success=True, message=message, data=result)


@router.post("/actions/execute", response_model=ApiResponse)
async def execute_action(
    request: Request,
    response: Response,
    body: ExecuteRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    correlation_id: Optional[str] = Header(default=None, alias="X-Correlation-ID"),
) -> ApiResponse:
    """Execute the tool-runner protected-write contract.

    Mandate and agent lifecycle routes are control-plane commands. Commerce
    mutations are protected writes and, in PostgreSQL mode, must carry both a
    stable idempotency key and a caller-owned correlation identifier.
    """
    if (
        idempotency_key
        and body.idempotency_key
        and idempotency_key != body.idempotency_key
    ):
        raise HTTPException(
            status_code=422, detail="Idempotency key header and body disagree."
        )
    effective_idempotency_key = (idempotency_key or body.idempotency_key or "").strip()
    if not effective_idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required.")
    principal_id, wallet_address = _principal(
        request, wallet_address=body.wallet_address
    )
    pool = _persistence_pool(request)
    if pool is not None and not (correlation_id or "").strip():
        raise HTTPException(
            status_code=422,
            detail="X-Correlation-ID is required for protected writes.",
        )
    effective_correlation_id = (correlation_id or f"correlation_{uuid4().hex}").strip()
    response.headers["X-Correlation-ID"] = effective_correlation_id
    if (
        pool is not None
        and agentguard.normalize_action(body.action) == "buyer.checkout.commit"
        and body.payload.get("quote_id")
    ):
        if not body.decision_id:
            raise HTTPException(status_code=422, detail="decision_id is required.")
        quote_id = str(body.payload["quote_id"])
        try:
            result = await CheckoutOrchestrator(pool).execute_checkout(
                principal_id=principal_id,
                quote_id=quote_id,
                decision_id=body.decision_id,
                approval_id=body.approval_id,
                idempotency_key=effective_idempotency_key,
                correlation_id=effective_correlation_id,
                payment_outcome=str(body.payload.get("payment_outcome", "succeeded")),
                delivery_context=body.payload.get("delivery_context"),
            )
        except Exception as error:
            _raise_persistent_error(error)
        if result["reason_code"] == "PAYMENT_STATUS_UNKNOWN":
            response.status_code = 202
        return ApiResponse(
            success=True,
            message=result["human_reason"],
            data={**result, "correlation_id": effective_correlation_id},
        )
    if pool is not None:
        normalized = agentguard.normalize_action(body.action)
        if normalized is None:
            raise HTTPException(
                status_code=422,
                detail="Unsupported protected action.",
            )
        try:
            if normalized.startswith("seller."):
                result = await SellerAgentGuardOrchestrator(pool).execute(
                    principal_id=principal_id,
                    decision_id=body.decision_id,
                    approval_id=body.approval_id,
                    action=normalized,
                    amount_inr=body.amount_inr,
                    resource_id=body.resource_id,
                    idempotency_key=effective_idempotency_key,
                    correlation_id=effective_correlation_id,
                    payload=body.payload,
                )
            else:
                result = await CheckoutOrchestrator(pool).execute_protected_action(
                    principal_id=principal_id,
                    decision_id=body.decision_id,
                    approval_id=body.approval_id,
                    action=normalized,
                    amount_inr=body.amount_inr,
                    resource_id=body.resource_id,
                    idempotency_key=effective_idempotency_key,
                    correlation_id=effective_correlation_id,
                    payload=body.payload,
                )
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(
            success=True,
            message=result["human_reason"],
            data={**result, "correlation_id": effective_correlation_id},
        )
    try:
        result = agentguard.execute_action(
            principal_id=principal_id,
            wallet_address=wallet_address,
            agent_id=body.agent_id,
            approval_id=body.approval_id,
            action=body.action,
            amount_inr=body.amount_inr,
            resource_id=body.resource_id,
            idempotency_key=effective_idempotency_key,
            payload={**body.payload, "correlation_id": effective_correlation_id},
        )
    except agentguard.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except agentguard.ExecutionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (KeyError, PermissionError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ApiResponse(
        success=True,
        message=result.get("reason") or "Executed",
        data={**result, "correlation_id": effective_correlation_id},
    )


@router.post("/agents/{agent_id}/pause", response_model=ApiResponse)
async def pause_agent(
    agent_id: str, request: Request, body: PauseRequest
) -> ApiResponse:
    principal_id, _wallet = _principal(request, wallet_address=body.wallet_address)
    pool = _persistence_pool(request)
    if pool is not None:
        try:
            paused = await CheckoutOrchestrator(pool).set_agent_status(
                principal_id=principal_id, agent_id=agent_id, status="paused"
            )
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(success=True, message="Agent paused", data={"agent": paused})
    _assert_agent_principal(agent_id, principal_id)
    paused = agentguard.pause_agent(agent_id)
    return ApiResponse(
        success=True, message="Agent paused", data={"agent": paused.model_dump()}
    )


@router.post("/agents/{agent_id}/resume", response_model=ApiResponse)
async def resume_agent(
    agent_id: str, request: Request, body: PauseRequest
) -> ApiResponse:
    principal_id, _wallet = _principal(request, wallet_address=body.wallet_address)
    pool = _persistence_pool(request)
    if pool is not None:
        try:
            resumed = await CheckoutOrchestrator(pool).set_agent_status(
                principal_id=principal_id, agent_id=agent_id, status="active"
            )
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(
            success=True, message="Agent resumed", data={"agent": resumed}
        )
    _assert_agent_principal(agent_id, principal_id)
    resumed = agentguard.resume_agent(agent_id)
    return ApiResponse(
        success=True, message="Agent resumed", data={"agent": resumed.model_dump()}
    )


@router.post("/agents/{agent_id}/revoke", response_model=ApiResponse)
async def revoke_agent(
    agent_id: str, request: Request, body: PauseRequest
) -> ApiResponse:
    principal_id, _wallet = _principal(request, wallet_address=body.wallet_address)
    pool = _persistence_pool(request)
    if pool is not None:
        try:
            revoked = await CheckoutOrchestrator(pool).set_agent_status(
                principal_id=principal_id, agent_id=agent_id, status="revoked"
            )
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(
            success=True, message="Agent revoked", data={"agent": revoked}
        )
    _assert_agent_principal(agent_id, principal_id)
    revoked = agentguard.revoke_agent(agent_id)
    return ApiResponse(
        success=True, message="Agent revoked", data={"agent": revoked.model_dump()}
    )


@router.post("/receipts/verify", response_model=ApiResponse)
async def verify_receipt(request: Request, body: ReceiptVerifyRequest) -> ApiResponse:
    pool = _persistence_pool(request)
    if pool is not None and body.receipt_id:
        principal_id, _wallet = _principal(request, wallet_address=None)
        try:
            stored = await CheckoutOrchestrator(pool).get_receipt(
                principal_id=principal_id, receipt_id=body.receipt_id
            )
        except Exception as error:
            _raise_persistent_error(error)
        result = agentguard.verify_receipt_payload(stored["payload"])
        return ApiResponse(success=True, message="Receipt verified", data=result)
    try:
        if body.receipt_id:
            result = agentguard.verify_receipt_by_id(body.receipt_id)
        elif body.receipt:
            result = agentguard.verify_receipt_payload(body.receipt)
        else:
            raise HTTPException(
                status_code=422, detail="receipt_id or receipt required"
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse(success=True, message="Receipt verified", data=result)


@router.get("/receipts/{receipt_id}", response_model=ApiResponse)
async def get_receipt(receipt_id: str, request: Request) -> ApiResponse:
    pool = _persistence_pool(request)
    if pool is not None:
        principal_id, _wallet = _principal(request, wallet_address=None)
        try:
            receipt = await CheckoutOrchestrator(pool).get_receipt(
                principal_id=principal_id, receipt_id=receipt_id
            )
        except Exception as error:
            _raise_persistent_error(error)
        return ApiResponse(success=True, message="Receipt", data={"receipt": receipt})
    receipt = agentguard.get_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Unknown receipt")
    return ApiResponse(
        success=True, message="Receipt", data={"receipt": receipt.model_dump()}
    )
