"""Pattern Analyzer MCP Server - Detects document tampering and anomalies."""
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
from typing import Optional
import hashlib
import re

# Create FastMCP server
mcp = FastMCP("pattern-analyzer")


@mcp.tool()
def detect_tampering(image_hash: str, expected_hash: Optional[str] = None, metadata: Optional[dict] = None) -> dict:
    """
    Detect if a document has been tampered with by comparing hashes and checking metadata.
    
    Args:
        image_hash: SHA256 hash of the document image
        expected_hash: Expected hash for comparison (optional)
        metadata: Image metadata dict (optional) for additional checks
    
    Returns:
        Dict with tampering analysis results
    """
    result = {
        "is_tampered": False,
        "hash_match": None,
        "warnings": [],
        "confidence": 1.0
    }
    
    # Hash comparison if expected hash provided
    if expected_hash:
        if image_hash.lower() != expected_hash.lower():
            result["is_tampered"] = True
            result["hash_match"] = False
            result["warnings"].append("Document hash does not match expected value")
            result["confidence"] = 0.3
        else:
            result["hash_match"] = True
            result["warnings"].append("Hash matches expected value")
    
    # Basic metadata checks
    if metadata:
        # Check for suspicious metadata patterns
        if "modified" in metadata and metadata["modified"] is True:
            result["warnings"].append("Document modification flag detected")
            result["is_tampered"] = True
            result["confidence"] = max(0.2, result["confidence"] - 0.4)
    
    return result


@mcp.tool()
def check_image_quality(image_data: str, width: int, height: int, dpi: Optional[int] = None) -> dict:
    """
    Check if image quality meets minimum requirements for OCR processing.
    
    Args:
        image_data: Base64 encoded image data
        width: Image width in pixels
        height: Image height in pixels
        dpi: Image DPI (optional)
    
    Returns:
        Dict with quality assessment and recommendations
    """
    result = {
        "acceptable": True,
        "resolution_ok": True,
        "dpi_ok": True,
        "warnings": [],
        "recommendations": []
    }
    
    # Resolution check (minimum 300x300)
    if width < 300 or height < 300:
        result["resolution_ok"] = False
        result["acceptable"] = False
        result["warnings"].append(f"Resolution too low: {width}x{height} (minimum: 300x300)")
        result["recommendations"].append("Use a higher resolution scan")
    
    # DPI check if provided (minimum 300 DPI)
    if dpi and dpi < 300:
        result["dpi_ok"] = False
        result["acceptable"] = False
        result["warnings"].append(f"DPI too low: {dpi} (minimum: 300)")
        result["recommendations"].append("Scan at higher DPI setting")
    
    # Aspect ratio check (unusual ratios may indicate manipulation)
    aspect_ratio = width / height
    if aspect_ratio < 0.5 or aspect_ratio > 2.0:
        result["warnings"].append(f"Unusual aspect ratio: {aspect_ratio:.2f}")
    
    return result


@mcp.tool()
def analyze_text_patterns(text: str, document_type: str) -> dict:
    """
    Analyze extracted text for common patterns and anomalies.
    
    Args:
        text: Extracted text from OCR
        document_type: Type of document (aadhaar, pan, etc.)
    
    Returns:
        Dict with pattern analysis results
    """
    result = {
        "patterns_found": [],
        "anomalies": [],
        "confidence": 1.0
    }
    
    if document_type == "aadhaar":
        # Aadhaar UID pattern (12 digits)
        uid_pattern = re.compile(r'\b\d{12}\b')
        uids = uid_pattern.findall(text)
        if uids:
            result["patterns_found"].append(f"Found {len(uids)} potential Aadhaar UIDs")
        
        # DOB pattern (DD/MM/YYYY or DD-MM-YYYY)
        dob_pattern = re.compile(r'\b(\d{2}[-/]\d{2}[-/]\d{4})\b')
        dobs = dob_pattern.findall(text)
        if dobs:
            result["patterns_found"].append(f"Found {len(dobs)} potential DOBs")
    
    elif document_type == "pan":
        # PAN pattern (5 letters + 4 digits + 1 letter)
        pan_pattern = re.compile(r'\b[A-Z]{5}\d{4}[A-Z]\b')
        pans = pan_pattern.findall(text)
        if pans:
            result["patterns_found"].append(f"Found {len(pans)} potential PAN numbers")
        
        # Check for multiple PANs (suspicious)
        if len(pans) > 1:
            result["anomalies"].append(f"Multiple PAN patterns detected: {len(pans)}")
            result["confidence"] = 0.5
    
    # Check for text density (too low may indicate poor scan)
    words = text.split()
    if len(words) < 20:
        result["anomalies"].append(f"Very low word count: {len(words)}")
        result["confidence"] = max(0.3, result["confidence"] - 0.3)
    
    return result


if __name__ == "__main__":
    # Run the server
    mcp.run()
