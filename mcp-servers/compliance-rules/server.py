"""Compliance Rules MCP Server - Validates against Aadhaar Act, DPDP Act, and other regulations."""
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
from typing import Optional, List, Literal
from datetime import datetime

# Create FastMCP server
mcp = FastMCP("compliance-rules")


@mcp.tool()
def check_aadhaar_act(
    consent_provided: bool,
    purpose: str,
    consent_timestamp: Optional[str] = None,
    data_retention_days: Optional[int] = None
) -> dict:
    """
    Validate Aadhaar document processing against Aadhaar Act requirements.
    
    Args:
        consent_provided: Whether user has provided explicit consent
        purpose: Purpose of document usage (identity verification, KYC, etc.)
        consent_timestamp: ISO timestamp when consent was provided
        data_retention_days: Number of days data will be retained
    
    Returns:
        Dict with compliance assessment and recommendations
    """
    result = {
        "compliant": True,
        "violations": [],
        "warnings": [],
        "recommendations": []
    }
    
    # Consent is mandatory
    if not consent_provided:
        result["compliant"] = False
        result["violations"].append("No explicit consent provided for Aadhaar data processing")
    
    # Valid purposes for Aadhaar usage
    valid_purposes = [
        "identity_verification",
        "kyc",
        "age_verification",
        "address_verification"
    ]
    
    purpose_lower = purpose.lower()
    if not any(vp in purpose_lower for vp in valid_purposes):
        result["warnings"].append(f"Unusual purpose specified: {purpose}")
    
    # Consent timestamp should be recent (within 30 days)
    if consent_timestamp:
        try:
            consent_date = datetime.fromisoformat(consent_timestamp)
            days_since_consent = (datetime.utcnow() - consent_date).days
            if days_since_consent > 30:
                result["warnings"].append(f"Consent is {days_since_consent} days old - may need renewal")
                result["recommendations"].append("Re-obtain user consent for Aadhaar processing")
        except ValueError:
            result["warnings"].append("Invalid consent timestamp format")
    
    # Data retention limit (Aadhaar data should not be retained longer than necessary)
    if data_retention_days:
        max_retention = 90  # 90 days as per best practice
        if data_retention_days > max_retention:
            result["violations"].append(f"Data retention ({data_retention_days} days) exceeds recommended limit ({max_retention} days)")
            result["compliant"] = False
    
    return result


@mcp.tool()
def check_dpdp(
    data_collected: List[str],
    purpose: str,
    data_minimization_met: bool,
    encryption_at_rest: bool,
    encryption_in_transit: bool
) -> dict:
    """
    Validate processing against Digital Personal Data Protection (DPDP) Act principles.
    
    Args:
        data_collected: List of data fields collected
        purpose: Purpose of data collection
        data_minimization_met: Whether only necessary data is collected
        encryption_at_rest: Whether data is encrypted at rest
        encryption_in_transit: Whether data is encrypted during transmission
    
    Returns:
        Dict with DPDP compliance assessment
    """
    result = {
        "compliant": True,
        "violations": [],
        "warnings": [],
        "recommendations": []
    }
    
    # Purpose limitation principle
    if not purpose or purpose.strip() == "":
        result["violations"].append("No valid purpose specified for data collection")
        result["compliant"] = False
    
    # Data minimization principle
    if not data_minimization_met:
        result["violations"].append("Data minimization principle not met - collecting unnecessary data")
        result["compliant"] = False
    else:
        # Check if sensitive data is being collected appropriately
        sensitive_fields = ["aadhaar_number", "pan_number", "bank_account"]
        collected_sensitive = [f for f in data_collected if any(sf in f.lower() for sf in sensitive_fields)]
        if collected_sensitive and len(collected_sensitive) > 1:
            result["warnings"].append(f"Collecting multiple sensitive fields: {collected_sensitive}")
    
    # Security principles
    if not encryption_at_rest:
        result["violations"].append("Data must be encrypted at rest (DPDP requirement)")
        result["compliant"] = False
    
    if not encryption_in_transit:
        result["violations"].append("Data must be encrypted in transit (DPDP requirement)")
        result["compliant"] = False
    
    # Storage limitation (not storing data longer than necessary)
    if len(data_collected) > 10:
        result["warnings"].append(f"Large number of data fields collected: {len(data_collected)}")
        result["recommendations"].append("Review data collection for necessity")
    
    return result


@mcp.tool()
def check_watchlist(
    name: str,
    pan_number: Optional[str] = None,
    aadhaar_number: Optional[str] = None
) -> dict:
    """
    Check if entity appears on any watchlists (mock implementation).
    
    Args:
        name: Full name to check
        pan_number: PAN number (optional)
        aadhaar_number: Aadhaar number (optional)
    
    Returns:
        Dict with watchlist check results
    """
    # Mock watchlist check - in production, this would query actual watchlist APIs
    result = {
        "is_on_watchlist": False,
        "matched_lists": [],
        "confidence": 1.0,
        "notes": []
    }
    
    # Note: This is a mock implementation
    result["notes"].append("Mock implementation - integrate with actual watchlist APIs in production")
    
    # Simulated check patterns (for demo purposes only)
    # In production, you would integrate with:
    # - SEBI watchlist for financial fraud
    # - FATCA/CRS lists for international compliance
    # - Internal fraud monitoring systems
    
    # Return clean result for demo
    result["notes"].append("No actual watchlist data available - using mock")
    
    return result


@mcp.tool()
def validate_document_completeness(
    document_type: str,
    extracted_fields: dict
) -> dict:
    """
    Validate that all required fields are present in the document.
    
    Args:
        document_type: Type of document (aadhaar, pan)
        extracted_fields: Dict of extracted fields
    
    Returns:
        Dict with completeness validation results
    """
    result = {
        "complete": True,
        "missing_fields": [],
        "warnings": []
    }
    
    if document_type == "aadhaar":
        required_fields = ["name", "aadhaar_number", "dob"]
        for field in required_fields:
            if field not in extracted_fields or not extracted_fields[field]:
                result["missing_fields"].append(field)
                result["complete"] = False
    
    elif document_type == "pan":
        required_fields = ["name", "pan_number", "dob"]
        for field in required_fields:
            if field not in extracted_fields or not extracted_fields[field]:
                result["missing_fields"].append(field)
                result["complete"] = False
    
    # Check for quality of extracted fields
    if "name" in extracted_fields:
        name = extracted_fields["name"]
        if len(name) < 3:
            result["warnings"].append("Extracted name is too short")
    
    return result


if __name__ == "__main__":
    # Run the server
    mcp.run()
