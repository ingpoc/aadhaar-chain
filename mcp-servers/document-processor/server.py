"""Document Processor MCP Server."""
from __future__ import annotations

import base64
import os
import sys

from mcp.server.fastmcp import FastMCP

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
gateway_root = os.path.join(repo_root, "gateway")
if gateway_root not in sys.path:
    sys.path.insert(0, gateway_root)

from app.document_processing import (  # noqa: E402
    detect_document_type as shared_detect_document_type,
    extract_aadhaar_fields as shared_extract_aadhaar_fields,
    extract_document_contract,
    extract_document_text,
    extract_pan_fields as shared_extract_pan_fields,
)


mcp = FastMCP("document-processor")

def _decode_document(document_data: str) -> bytes:
    return base64.b64decode(document_data)


@mcp.tool()
def ocr_document(document_data: str, file_type: str = "image", mime_type: Optional[str] = None) -> dict:
    """Extract raw text from uploaded document bytes."""
    try:
        document_bytes = _decode_document(document_data)
    except Exception as exc:
        return {"success": False, "error": f"Failed to decode base64 document data: {exc}"}

    result = extract_document_text(document_bytes, mime_type=mime_type, file_name=None)

    return {
        "success": True,
        "text": result.text,
        "confidence": result.confidence,
        "warnings": result.warnings,
        "method": result.method,
    }


@mcp.tool()
def detect_document_type(ocr_text: str) -> dict:
    """Detect document type from OCR text."""
    document_type, confidence, indicators = shared_detect_document_type(ocr_text)
    return {
        "success": True,
        "document_type": document_type,
        "confidence": confidence,
        "indicators": indicators,
    }


@mcp.tool()
def extract_aadhaar_fields(ocr_text: str, document_type: str = "aadhaar") -> dict:
    """Extract Aadhaar fields from OCR text."""
    result = shared_extract_aadhaar_fields(ocr_text)

    return {
        "success": True,
        "document_type": document_type,
        "fields": result.fields,
        "confidence": result.confidence,
        "warnings": result.warnings,
    }


@mcp.tool()
def extract_pan_fields(ocr_text: str, document_type: str = "pan") -> dict:
    """Extract PAN fields from OCR text."""
    result = shared_extract_pan_fields(ocr_text)

    return {
        "success": True,
        "document_type": document_type,
        "fields": result.fields,
        "confidence": result.confidence,
        "warnings": result.warnings,
    }


@mcp.tool()
def extract_document_contract_tool(
    document_data: str,
    expected_document_type: str,
    mime_type: str | None = None,
    file_name: str | None = None,
) -> dict:
    """Extract a full structured document contract directly from uploaded bytes."""
    try:
        document_bytes = _decode_document(document_data)
    except Exception as exc:
        return {"success": False, "error": f"Failed to decode base64 document data: {exc}"}

    result = extract_document_contract(
        document_bytes,
        expected_document_type=expected_document_type,
        mime_type=mime_type,
        file_name=file_name,
    )
    return {
        "success": True,
        "document_type": result.document_type,
        "detected_document_type": result.detected_document_type,
        "text": result.text,
        "text_method": result.text_method,
        "fields": result.fields,
        "confidence": result.confidence,
        "warnings": result.warnings,
        "indicators": result.indicators,
    }


if __name__ == "__main__":
    mcp.run()
