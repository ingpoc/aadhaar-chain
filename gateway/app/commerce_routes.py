"""HTTP routes for the local AgentGuard commerce demo."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app import commerce_demo
from app.models import ApiResponse
from config import get_runtime_mode

router = APIRouter(prefix="/api/demo-commerce", tags=["demo-commerce"])


class CommerceBody(BaseModel):
    idempotency_key: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)


class ItemBody(BaseModel):
    idempotency_key: Optional[str] = None
    title: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    price_inr: int = Field(0, ge=0)
    inventory: int = Field(0, ge=0)
    seller_id: Optional[str] = None


class OrderBody(BaseModel):
    idempotency_key: Optional[str] = None
    item_id: str
    quantity: int = Field(1, ge=1)
    buyer_id: Optional[str] = None
    payment_mode: str = "success"


class TransitionBody(BaseModel):
    idempotency_key: Optional[str] = None
    status: str


class IssueBody(BaseModel):
    idempotency_key: Optional[str] = None
    reason: Optional[str] = None
    description: Optional[str] = None


def _idem(body_key: Optional[str], header_key: Optional[str]) -> Optional[str]:
    return body_key or header_key


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise exc


@router.post("/test-fixtures/cleanup", response_model=ApiResponse)
async def cleanup_test_fixtures(body: CommerceBody = CommerceBody()) -> ApiResponse:
    if get_runtime_mode() != "demo":
        raise HTTPException(status_code=404, detail="Not found")
    requested = body.data.get("order_ids") or []
    explicit_order_ids = {
        str(order_id) for order_id in requested if isinstance(order_id, str) and order_id.strip()
    }
    result = commerce_demo.cleanup_test_artifacts(explicit_order_ids=explicit_order_ids)
    return ApiResponse(success=True, message="Test fixtures removed", data=result)


@router.post("/seller/items", response_model=ApiResponse)
async def create_seller_item(
    body: ItemBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    try:
        result = commerce_demo.create_item(
            body.model_dump(exclude_none=True),
            idempotency_key=_idem(body.idempotency_key, idempotency_key),
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Item created", data=result)


@router.patch("/seller/items/{item_id}", response_model=ApiResponse)
async def update_seller_item(item_id: str, body: dict[str, Any]) -> ApiResponse:
    try:
        result = commerce_demo.update_item(item_id, body)
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Item updated", data=result)


@router.post("/seller/items/{item_id}/publish", response_model=ApiResponse)
async def publish_seller_item(
    item_id: str,
    body: CommerceBody = CommerceBody(),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    try:
        result = commerce_demo.publish_item(
            item_id,
            idempotency_key=_idem(body.idempotency_key, idempotency_key),
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Item published", data=result)


@router.get("/buyer/search", response_model=ApiResponse)
async def search_items(q: Optional[str] = None) -> ApiResponse:
    return ApiResponse(success=True, message="Items", data=commerce_demo.search_items(q))


@router.get("/buyer/items/{item_id}", response_model=ApiResponse)
async def get_item(item_id: str) -> ApiResponse:
    try:
        result = commerce_demo.get_item(item_id)
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Item", data=result)


@router.post("/buyer/orders", response_model=ApiResponse)
async def create_buyer_order(
    body: OrderBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    try:
        result = commerce_demo.create_order(
            body.model_dump(exclude_none=True),
            idempotency_key=_idem(body.idempotency_key, idempotency_key),
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Order created", data=result)


@router.get("/buyer/orders/{order_id}", response_model=ApiResponse)
async def get_buyer_order(order_id: str) -> ApiResponse:
    try:
        result = commerce_demo.get_order(order_id)
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Order", data=result)


@router.get("/seller/orders", response_model=ApiResponse)
async def list_seller_orders(seller_id: Optional[str] = None) -> ApiResponse:
    return ApiResponse(success=True, message="Orders", data=commerce_demo.list_seller_orders(seller_id))


@router.post("/seller/orders/{order_id}/transition", response_model=ApiResponse)
async def transition_order(
    order_id: str,
    body: TransitionBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    try:
        result = commerce_demo.transition_order(
            order_id,
            body.status,
            idempotency_key=_idem(body.idempotency_key, idempotency_key),
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Order transitioned", data=result)


@router.post("/buyer/orders/{order_id}/issues", response_model=ApiResponse)
async def create_issue(
    order_id: str,
    body: IssueBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    try:
        result = commerce_demo.create_issue(
            order_id,
            body.model_dump(exclude_none=True),
            idempotency_key=_idem(body.idempotency_key, idempotency_key),
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Issue created", data=result)


@router.get("/seller/issues", response_model=ApiResponse)
async def list_seller_issues() -> ApiResponse:
    return ApiResponse(success=True, message="Issues", data=commerce_demo.list_seller_issues())


@router.post("/seller/issues/{issue_id}/respond", response_model=ApiResponse)
async def respond_issue(issue_id: str, body: dict[str, Any]) -> ApiResponse:
    try:
        result = commerce_demo.respond_issue(issue_id, body)
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Issue responded", data=result)


@router.post("/seller/issues/{issue_id}/remedy", response_model=ApiResponse)
async def remedy_issue(
    issue_id: str,
    body: CommerceBody,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse:
    payload = {**body.data, "issue_id": issue_id}
    try:
        result = commerce_demo.propose_remedy(
            issue_id,
            payload,
            idempotency_key=_idem(body.idempotency_key, idempotency_key),
        )
    except Exception as exc:
        _handle_error(exc)
    return ApiResponse(success=True, message="Remedy promised", data=result)
