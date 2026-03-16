"""Document Processor MCP Server."""
from __future__ import annotations

import base64
import io
import re
from typing import Optional

from mcp.server.fastmcp import FastMCP

try:
    import pytesseract
    from PIL import Image
except ImportError:  # pragma: no cover - optional runtime dependency
    pytesseract = None
    Image = None


mcp = FastMCP("document-processor")


def _decode_document(document_data: str) -> bytes:
    return base64.b64decode(document_data)


def _extract_printable_text(document_bytes: bytes) -> str:
    decoded = document_bytes.decode("utf-8", errors="ignore")
    if decoded.strip():
        return decoded

    chunks = re.findall(rb"[\x20-\x7E]{4,}", document_bytes)
    return "\n".join(chunk.decode("utf-8", errors="ignore") for chunk in chunks)


def _ocr_image(document_bytes: bytes) -> str:
    if Image is None or pytesseract is None:
        return ""

    try:
        with Image.open(io.BytesIO(document_bytes)) as image:
            return pytesseract.image_to_string(image)
    except Exception:
        return ""


def _extract_document_text(document_bytes: bytes, mime_type: Optional[str], file_type: Optional[str]) -> tuple[str, list[str]]:
    warnings: list[str] = []
    text = ""

    if mime_type and mime_type.startswith("image/"):
        text = _ocr_image(document_bytes)
        if not text:
            warnings.append("Image OCR did not produce readable text.")
    else:
        text = _extract_printable_text(document_bytes)

    if not text.strip() and file_type == "image":
        text = _ocr_image(document_bytes)

    if not text.strip():
        warnings.append("No readable text could be extracted from the supplied document bytes.")

    return text.strip(), warnings


def _detect_type(ocr_text: str) -> tuple[str, float, list[str]]:
    lowered = ocr_text.lower()
    indicators: list[str] = []

    if "uidai" in lowered or "aadhaar" in lowered or re.search(r"\b\d{4}[ ]?\d{4}[ ]?\d{4}\b", ocr_text):
        indicators.append("aadhaar-pattern")
        return "aadhaar", 0.8, indicators

    if re.search(r"\b[A-Z]{5}\d{4}[A-Z]\b", ocr_text):
        indicators.append("pan-pattern")
        return "pan", 0.8, indicators

    return "unknown", 0.2, indicators


def _extract_name(ocr_text: str, excluded_patterns: list[re.Pattern[str]]) -> Optional[str]:
    for line in [line.strip() for line in ocr_text.splitlines() if line.strip()]:
        if any(pattern.search(line) for pattern in excluded_patterns):
            continue
        letters_only = re.sub(r"[^A-Za-z ]", "", line).strip()
        if len(letters_only.split()) >= 2:
            return letters_only
    return None


@mcp.tool()
def ocr_document(document_data: str, file_type: str = "image", mime_type: Optional[str] = None) -> dict:
    """Extract raw text from uploaded document bytes."""
    try:
        document_bytes = _decode_document(document_data)
    except Exception as exc:
        return {"success": False, "error": f"Failed to decode base64 document data: {exc}"}

    text, warnings = _extract_document_text(document_bytes, mime_type, file_type)
    confidence = 0.85 if text else 0.2
    if warnings and text:
        confidence = 0.55

    return {
        "success": True,
        "text": text,
        "confidence": confidence,
        "warnings": warnings,
    }


@mcp.tool()
def detect_document_type(ocr_text: str) -> dict:
    """Detect document type from OCR text."""
    document_type, confidence, indicators = _detect_type(ocr_text)
    return {
        "success": True,
        "document_type": document_type,
        "confidence": confidence,
        "indicators": indicators,
    }


@mcp.tool()
def extract_aadhaar_fields(ocr_text: str, document_type: str = "aadhaar") -> dict:
    """Extract Aadhaar fields from OCR text."""
    uid_match = re.search(r"\b(\d{4}[ ]?\d{4}[ ]?\d{4})\b", ocr_text)
    dob_match = re.search(r"\b(\d{2}[/-]\d{2}[/-]\d{4})\b", ocr_text)
    address_match = re.search(r"(?:address|addr)[:\s-]+(.+)", ocr_text, re.IGNORECASE)
    name = _extract_name(
        ocr_text,
        [
            re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),
            re.compile(r"aadhaar|uidai|government|dob|address", re.IGNORECASE),
        ],
    )

    fields = {
        "name": name,
        "dob": dob_match.group(1) if dob_match else None,
        "uid": uid_match.group(1).replace(" ", "") if uid_match else None,
        "address": address_match.group(1).strip() if address_match else None,
    }
    warnings = [key for key in ("name", "dob", "uid") if not fields.get(key)]

    return {
        "success": True,
        "document_type": document_type,
        "fields": fields,
        "confidence": 0.9 if not warnings else 0.45,
        "warnings": [f"Missing {item}" for item in warnings],
    }


@mcp.tool()
def extract_pan_fields(ocr_text: str, document_type: str = "pan") -> dict:
    """Extract PAN fields from OCR text."""
    pan_match = re.search(r"\b([A-Z]{5}\d{4}[A-Z])\b", ocr_text.upper())
    dob_match = re.search(r"\b(\d{2}[/-]\d{2}[/-]\d{4})\b", ocr_text)
    name = _extract_name(
        ocr_text,
        [
            re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
            re.compile(r"permanent account number|income tax|pan|dob", re.IGNORECASE),
        ],
    )

    fields = {
        "name": name,
        "dob": dob_match.group(1) if dob_match else None,
        "pan_number": pan_match.group(1) if pan_match else None,
    }
    warnings = [key for key in ("name", "dob", "pan_number") if not fields.get(key)]

    return {
        "success": True,
        "document_type": document_type,
        "fields": fields,
        "confidence": 0.9 if not warnings else 0.45,
        "warnings": [f"Missing {item}" for item in warnings],
    }


if __name__ == "__main__":
    mcp.run()
