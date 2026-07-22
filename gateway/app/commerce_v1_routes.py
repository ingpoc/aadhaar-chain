"""Authenticated HTTP boundary for durable single-seller commerce."""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.commerce_v1 import (
    CommerceConflict,
    CommerceNotFound,
    CommerceV1,
    CommerceValidation,
    IdempotencyConflict,
)
from app.models import ApiResponse
from app.persistence import ConnectionPool
from app.session_auth import SESSION_COOKIE_NAME, parse_session_token

router = APIRouter(prefix="/api/commerce/v1", tags=["commerce-v1"])

IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", min_length=1)]
CorrelationHeader = Annotated[
    str | None, Header(alias="X-Correlation-ID", min_length=1)
]


class CreateCartRequest(BaseModel):
    seller_id: str = Field(min_length=1)


class SetCartLineRequest(BaseModel):
    quantity: int = Field(ge=0)
    expected_version: int = Field(ge=1)


class CheckoutPreviewRequest(BaseModel):
    expected_version: int = Field(ge=1)
    landed_total_paise: int | None = Field(default=None, ge=0)
    ttl_seconds: int = Field(default=300, gt=0, le=1800)


def _principal(request: Request) -> str:
    session = parse_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))
    if not session or not session.get("principal_id"):
        raise HTTPException(status_code=401, detail="Authenticated principal required.")
    if session.get("aud") != "ondcbuyer":
        raise HTTPException(status_code=403, detail="Buyer session required.")
    return str(session["principal_id"])


def _service(request: Request) -> CommerceV1:
    pool = getattr(request.app.state, "persistence_pool", None)
    if not isinstance(pool, ConnectionPool) or not pool.is_open:
        raise HTTPException(status_code=503, detail="Durable commerce is unavailable.")
    return CommerceV1(pool)


def _correlate(response: Response, supplied: str | None) -> str:
    correlation_id = supplied or f"correlation_{uuid4().hex}"
    response.headers["X-Correlation-ID"] = correlation_id
    return correlation_id


def _raise_domain_error(error: Exception) -> None:
    if isinstance(error, CommerceNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, (CommerceConflict, IdempotencyConflict)):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, (CommerceValidation, ValueError)):
        raise HTTPException(status_code=422, detail=str(error)) from error
    raise error


@router.post("/carts", response_model=ApiResponse, status_code=201)
async def create_cart(
    request: Request,
    response: Response,
    body: CreateCartRequest,
    idempotency_key: IdempotencyHeader,
    correlation_id: CorrelationHeader = None,
) -> ApiResponse:
    correlation = _correlate(response, correlation_id)
    try:
        cart = await _service(request).create_cart(
            principal_id=_principal(request),
            seller_id=body.seller_id,
            idempotency_key=idempotency_key,
        )
    except Exception as error:
        _raise_domain_error(error)
    return ApiResponse(
        success=True,
        message="Cart ready",
        data={"cart": cart, "correlation_id": correlation},
    )


@router.get("/carts/{cart_id}", response_model=ApiResponse)
async def get_cart(cart_id: str, request: Request) -> ApiResponse:
    try:
        cart = await _service(request).get_cart(
            principal_id=_principal(request), cart_id=cart_id
        )
    except Exception as error:
        _raise_domain_error(error)
    return ApiResponse(success=True, message="Cart", data={"cart": cart})


@router.put("/carts/{cart_id}/lines/{sku}", response_model=ApiResponse)
async def set_cart_line(
    cart_id: str,
    sku: str,
    request: Request,
    response: Response,
    body: SetCartLineRequest,
    idempotency_key: IdempotencyHeader,
    correlation_id: CorrelationHeader = None,
) -> ApiResponse:
    correlation = _correlate(response, correlation_id)
    try:
        cart = await _service(request).set_cart_line(
            principal_id=_principal(request),
            cart_id=cart_id,
            sku=sku,
            quantity=body.quantity,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
    except Exception as error:
        _raise_domain_error(error)
    return ApiResponse(
        success=True,
        message="Cart updated",
        data={"cart": cart, "correlation_id": correlation},
    )


@router.post("/carts/{cart_id}/checkout-preview", response_model=ApiResponse)
async def checkout_preview(
    cart_id: str,
    request: Request,
    response: Response,
    body: CheckoutPreviewRequest,
    idempotency_key: IdempotencyHeader,
    correlation_id: CorrelationHeader = None,
) -> ApiResponse:
    correlation = _correlate(response, correlation_id)
    try:
        quote = await _service(request).preview_checkout(
            principal_id=_principal(request),
            cart_id=cart_id,
            expected_version=body.expected_version,
            landed_total_paise=body.landed_total_paise,
            ttl_seconds=body.ttl_seconds,
            idempotency_key=idempotency_key,
        )
    except Exception as error:
        _raise_domain_error(error)
    return ApiResponse(
        success=True,
        message="Checkout preview ready",
        data={"quote": quote, "correlation_id": correlation},
    )


@router.get("/orders/{order_id}", response_model=ApiResponse)
async def get_order(order_id: str, request: Request) -> ApiResponse:
    try:
        order = await _service(request).get_order(
            principal_id=_principal(request), order_id=order_id
        )
    except Exception as error:
        _raise_domain_error(error)
    return ApiResponse(success=True, message="Order", data={"order": order})
