"""Shared document OCR and field extraction helpers.

This module is the single source of truth for document text extraction used by
both the gateway and the document-processor MCP server.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import io
import os
import re
import shutil
from typing import Optional

try:
    import pytesseract
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - optional runtime dependency
    pytesseract = None
    Image = None
    ImageOps = None


_DIGIT_SUBSTITUTIONS = str.maketrans(
    {
        "O": "0",
        "Q": "0",
        "D": "0",
        "I": "1",
        "L": "1",
        "|": "1",
        "S": "5",
        "B": "8",
        "G": "6",
        "Z": "2",
    }
)

_PAN_ALPHA_SUBSTITUTIONS = {
    "0": "O",
    "1": "I",
    "2": "Z",
    "5": "S",
    "6": "G",
    "8": "B",
}

_PAN_DIGIT_SUBSTITUTIONS = {
    "O": "0",
    "Q": "0",
    "D": "0",
    "I": "1",
    "L": "1",
    "S": "5",
    "B": "8",
    "Z": "2",
}


@dataclass
class TextExtractionResult:
    text: str
    confidence: float
    warnings: list[str] = field(default_factory=list)
    method: str = "unavailable"
    runtime_error: Optional[str] = None


@dataclass
class StructuredDocumentResult:
    document_type: str
    detected_document_type: str
    text: str
    text_confidence: float
    fields: dict[str, Optional[str]]
    confidence: float
    warnings: list[str] = field(default_factory=list)
    text_method: str = "unavailable"
    indicators: list[str] = field(default_factory=list)
    runtime_error: Optional[str] = None


def _extract_printable_text(document_bytes: bytes) -> str:
    if b"\x00" not in document_bytes:
        decoded = document_bytes.decode("utf-8", errors="ignore")
        printable_chars = sum(1 for char in decoded if char.isprintable() or char.isspace())
        printable_ratio = printable_chars / max(len(decoded), 1)
        alpha_chunks = re.findall(r"[A-Za-z]{3,}", decoded)
        if decoded.strip() and printable_ratio > 0.9 and len(alpha_chunks) >= 2:
            return decoded

    decoded = document_bytes.decode("utf-8", errors="ignore")
    if decoded.strip() and len(re.findall(r"[A-Za-z]{3,}", decoded)) >= 2 and not document_bytes.startswith(b"\x89PNG"):
        return decoded

    chunks = re.findall(rb"[\x20-\x7E]{4,}", document_bytes)
    return "\n".join(chunk.decode("utf-8", errors="ignore") for chunk in chunks)


def _score_text(text: str) -> int:
    if not text.strip():
        return 0
    alnum = len(re.findall(r"[A-Za-z0-9]", text))
    line_bonus = len([line for line in text.splitlines() if line.strip()]) * 8
    keyword_bonus = 0
    lowered = text.lower()
    for keyword in ("uidai", "aadhaar", "permanent account number", "income tax", "dob"):
        if keyword in lowered:
            keyword_bonus += 40
    return alnum + line_bonus + keyword_bonus


def _ocr_image(document_bytes: bytes) -> TextExtractionResult:
    if Image is None or ImageOps is None or pytesseract is None:
        return TextExtractionResult(
            text="",
            confidence=0.0,
            warnings=["OCR dependencies are unavailable in the gateway runtime."],
            method="ocr_unavailable",
            runtime_error="OCR dependencies are unavailable in the gateway runtime.",
        )

    if shutil.which("tesseract") is None:
        return TextExtractionResult(
            text="",
            confidence=0.0,
            warnings=["Tesseract binary is unavailable in the gateway runtime."],
            method="ocr_unavailable",
            runtime_error="Tesseract binary is unavailable in the gateway runtime.",
        )

    try:
        with Image.open(io.BytesIO(document_bytes)) as image:
            grayscale = ImageOps.grayscale(image)
            autocontrast = ImageOps.autocontrast(grayscale)
            threshold = autocontrast.point(lambda px: 0 if px < 180 else 255, mode="1")
            variants = [
                ("grayscale", grayscale),
                ("autocontrast", autocontrast),
                ("threshold", threshold),
            ]

            best_text = ""
            best_method = "ocr_unavailable"
            best_score = -1
            for method, variant in variants:
                text = pytesseract.image_to_string(variant, config="--psm 6")
                score = _score_text(text)
                if score > best_score:
                    best_score = score
                    best_text = text
                    best_method = method

            warnings: list[str] = []
            confidence = 0.85 if best_text.strip() else 0.15
            if not best_text.strip():
                warnings.append("Image OCR did not produce readable text.")

            return TextExtractionResult(
                text=best_text.strip(),
                confidence=confidence,
                warnings=warnings,
                method=f"tesseract:{best_method}",
            )
    except Exception as exc:  # pragma: no cover - defensive runtime handling
        return TextExtractionResult(
            text="",
            confidence=0.0,
            warnings=[f"Image OCR failed: {exc}"],
            method="ocr_error",
            runtime_error=f"Image OCR failed: {exc}",
        )


def _infer_file_type(
    document_bytes: bytes,
    mime_type: Optional[str],
    file_name: Optional[str],
) -> str:
    lowered_mime = (mime_type or "").lower()
    lowered_name = (file_name or "").lower()
    if lowered_mime.startswith("image/") or lowered_name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "image"
    if lowered_mime == "application/pdf" or lowered_name.endswith(".pdf") or document_bytes.startswith(b"%PDF"):
        return "pdf"
    if lowered_mime.startswith("text/") or lowered_name.endswith((".txt", ".json")):
        return "text"
    return "binary"


def extract_document_text(
    document_bytes: bytes,
    *,
    mime_type: Optional[str] = None,
    file_name: Optional[str] = None,
) -> TextExtractionResult:
    file_type = _infer_file_type(document_bytes, mime_type, file_name)

    if file_type == "image":
        ocr_result = _ocr_image(document_bytes)
        warnings = list(ocr_result.warnings)
        if not ocr_result.text.strip():
            warnings.append("No readable text could be extracted from the supplied document bytes.")
        return TextExtractionResult(
            text=ocr_result.text,
            confidence=ocr_result.confidence,
            warnings=warnings,
            method=ocr_result.method,
            runtime_error=ocr_result.runtime_error,
        )

    warnings: list[str] = []
    printable_text = _extract_printable_text(document_bytes).strip()

    if file_type == "pdf" and not printable_text:
        return TextExtractionResult(
            text="",
            confidence=0.0,
            warnings=[
                "No embedded text could be extracted from the PDF bytes.",
                "Scanned PDFs require an OCR backend beyond printable-text extraction.",
            ],
            method="pdf_embedded_text",
        )

    confidence = 0.8 if printable_text else 0.0
    if file_type == "binary" and printable_text:
        confidence = 0.5
    if not printable_text:
        warnings.append("No readable text could be extracted from the supplied document bytes.")

    return TextExtractionResult(
        text=printable_text,
        confidence=confidence,
        warnings=warnings,
        method="embedded_text",
    )


def detect_document_type(ocr_text: str) -> tuple[str, float, list[str]]:
    lowered = ocr_text.lower()
    indicators: list[str] = []

    if "uidai" in lowered or "aadhaar" in lowered or re.search(r"\b\d{4}[ ]?\d{4}[ ]?\d{4}\b", ocr_text):
        indicators.append("aadhaar-pattern")
        return "aadhaar", 0.8, indicators

    if re.search(r"\b[A-Z0-9]{5}[0-9OILSBZ]{4}[A-Z0-9]\b", ocr_text.upper()):
        indicators.append("pan-pattern")
        return "pan", 0.8, indicators

    return "unknown", 0.2, indicators


def _normalize_digit_like(value: str) -> str:
    return value.upper().translate(_DIGIT_SUBSTITUTIONS)


def _extract_uid(ocr_text: str) -> Optional[str]:
    for line in _candidate_lines(ocr_text):
        normalized_line = _normalize_digit_like(line.upper())
        match = re.search(r"\b(\d{4})[ -]?(\d{4})[ -]?(\d{4})\b", normalized_line)
        if match:
            return "".join(match.groups())

    normalized = _normalize_digit_like(ocr_text.upper())
    match = re.search(r"\b(\d{4})[ -]?(\d{4})[ -]?(\d{4})\b", normalized)
    if match:
        return "".join(match.groups())
    return None


def _extract_date(ocr_text: str) -> Optional[str]:
    for match in re.finditer(r"[0-9OQDILSBGZ]{2}[/-][0-9OQDILSBGZ]{2}[/-][0-9OQDILSBGZ]{4}", ocr_text.upper()):
        normalized = _normalize_digit_like(match.group(0))
        if re.fullmatch(r"\d{2}[/-]\d{2}[/-]\d{4}", normalized):
            return normalized
    return None


def _normalize_pan_candidate(token: str) -> Optional[str]:
    token = re.sub(r"[^A-Z0-9]", "", token.upper())
    if len(token) != 10:
        return None

    normalized: list[str] = []
    for index, char in enumerate(token):
        if index in {0, 1, 2, 3, 4, 9}:
            if char.isalpha():
                normalized.append(char)
            elif char in _PAN_ALPHA_SUBSTITUTIONS:
                normalized.append(_PAN_ALPHA_SUBSTITUTIONS[char])
            else:
                return None
        else:
            if char.isdigit():
                normalized.append(char)
            elif char in _PAN_DIGIT_SUBSTITUTIONS:
                normalized.append(_PAN_DIGIT_SUBSTITUTIONS[char])
            else:
                return None

    candidate = "".join(normalized)
    if re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", candidate):
        return candidate
    return None


def _extract_pan_number(ocr_text: str) -> Optional[str]:
    tokens = re.findall(r"[A-Z0-9]{10,}", ocr_text.upper())
    for token in tokens:
        normalized = _normalize_pan_candidate(token[:10])
        if normalized:
            return normalized
    compact = re.sub(r"[^A-Z0-9]", "", ocr_text.upper())
    for index in range(0, max(len(compact) - 9, 0)):
        normalized = _normalize_pan_candidate(compact[index:index + 10])
        if normalized:
            return normalized
    return None


def _candidate_lines(ocr_text: str) -> list[str]:
    return [line.strip() for line in ocr_text.splitlines() if line.strip()]


def _looks_like_name(candidate: str, blocked_patterns: list[re.Pattern[str]]) -> bool:
    if any(pattern.search(candidate) for pattern in blocked_patterns):
        return False
    letters_only = re.sub(r"[^A-Za-z ]", "", candidate).strip()
    words = [word for word in letters_only.split() if word]
    if len(words) < 2 or len(words) > 5:
        return False
    if any(len(word) == 1 for word in words):
        return False
    return bool(letters_only)


def _clean_name(candidate: str) -> Optional[str]:
    letters_only = re.sub(r"[^A-Za-z ]", "", candidate).strip()
    words = [word.capitalize() for word in letters_only.split() if word]
    if len(words) < 2:
        return None
    return " ".join(words)


def _extract_name_from_label(lines: list[str]) -> Optional[str]:
    for index, line in enumerate(lines):
        lowered = line.lower()
        if "name" in lowered:
            parts = re.split(r"name[:\s-]+", line, flags=re.IGNORECASE, maxsplit=1)
            if len(parts) == 2:
                cleaned = _clean_name(parts[1])
                if cleaned:
                    return cleaned
            if index + 1 < len(lines):
                cleaned = _clean_name(lines[index + 1])
                if cleaned:
                    return cleaned
    return None


def _extract_name(ocr_text: str, document_type: str) -> Optional[str]:
    lines = _candidate_lines(ocr_text)
    labelled = _extract_name_from_label(lines)
    if labelled:
        return labelled

    blocked_patterns = [
        re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),
        re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
        re.compile(r"\d"),
        re.compile(r"uidai|aadhaar|government|india|dob|address|year of birth|male|female", re.IGNORECASE),
        re.compile(r"permanent account number|income tax|signature", re.IGNORECASE),
    ]

    date = _extract_date(ocr_text)
    if date:
        for index, line in enumerate(lines):
            normalized_line = _normalize_digit_like(line.upper())
            if date in normalized_line:
                for lookback in range(1, 3):
                    if index - lookback >= 0 and _looks_like_name(lines[index - lookback], blocked_patterns):
                        cleaned = _clean_name(lines[index - lookback])
                        if cleaned:
                            return cleaned

    best_candidate: Optional[str] = None
    best_score = -1
    for line in lines:
        if not _looks_like_name(line, blocked_patterns):
            continue
        cleaned = _clean_name(line)
        if not cleaned:
            continue
        score = len(cleaned.replace(" ", ""))
        if line.isupper():
            score += 6
        if document_type == "pan" and line.isupper():
            score += 4
        if score > best_score:
            best_candidate = cleaned
            best_score = score
    return best_candidate


def extract_aadhaar_fields(ocr_text: str) -> StructuredDocumentResult:
    detected_document_type, _, indicators = detect_document_type(ocr_text)
    fields = {
        "name": _extract_name(ocr_text, "aadhaar"),
        "dob": _extract_date(ocr_text),
        "uid": _extract_uid(ocr_text),
        "address": None,
    }

    address_match = re.search(r"(?:address|addr)[:\s-]+(.+)", ocr_text, re.IGNORECASE)
    if address_match:
        fields["address"] = address_match.group(1).strip()

    warnings = [f"Missing {field}" for field in ("name", "dob", "uid") if not fields.get(field)]
    completeness = 1 - (len(warnings) / 3)
    confidence = min(0.98, 0.45 + (completeness * 0.4))

    return StructuredDocumentResult(
        document_type="aadhaar",
        detected_document_type=detected_document_type,
        text=ocr_text,
        text_confidence=0.0,
        fields=fields,
        confidence=confidence,
        warnings=warnings,
        indicators=indicators,
    )


def extract_pan_fields(ocr_text: str) -> StructuredDocumentResult:
    detected_document_type, _, indicators = detect_document_type(ocr_text)
    fields = {
        "name": _extract_name(ocr_text, "pan"),
        "dob": _extract_date(ocr_text),
        "pan_number": _extract_pan_number(ocr_text),
    }

    warnings = [f"Missing {field}" for field in ("name", "dob", "pan_number") if not fields.get(field)]
    completeness = 1 - (len(warnings) / 3)
    confidence = min(0.98, 0.45 + (completeness * 0.4))

    return StructuredDocumentResult(
        document_type="pan",
        detected_document_type=detected_document_type,
        text=ocr_text,
        text_confidence=0.0,
        fields=fields,
        confidence=confidence,
        warnings=warnings,
        indicators=indicators,
    )


def extract_document_contract(
    document_bytes: bytes,
    *,
    expected_document_type: str,
    mime_type: Optional[str] = None,
    file_name: Optional[str] = None,
) -> StructuredDocumentResult:
    text_result = extract_document_text(
        document_bytes,
        mime_type=mime_type,
        file_name=file_name,
    )

    if expected_document_type == "aadhaar":
        result = extract_aadhaar_fields(text_result.text)
    else:
        result = extract_pan_fields(text_result.text)

    result.text = text_result.text
    result.text_confidence = text_result.confidence
    result.text_method = text_result.method
    result.runtime_error = text_result.runtime_error
    result.warnings = text_result.warnings + result.warnings
    result.confidence = min(0.99, round((text_result.confidence * 0.5) + (result.confidence * 0.5), 2))
    return result
