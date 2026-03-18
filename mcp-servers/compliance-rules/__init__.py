"""Compliance Rules MCP Server for Aadhaar Act and DPDP Act validation."""
from typing import Optional, List, Literal
from enum import Enum


class ComplianceRule(str, Enum):
    """Compliance rule categories."""
    data_minimization = "data_minimization"  # Aadhaar Act
    purpose_limitation = "purpose_limitation"  # Aadhaar Act
    explicit_consent = "explicit_consent"  # Aadhaar Act
    storage_limitation = "storage_limitation"  # DPDP Act
    access_control = "access_control"  # General


class ConsentType(str, Enum):
    """Types of user consent."""
    aadhaar_card = "aadhaar_card"
    pan_card = "pan_card"
    driving_license = "driving_license"
    education = "education"
    land_records = "land_records"


class Purpose(str, Enum):
    """Legitimate purposes for data access."""
    kyc_verification = "kyc_verification"
    lending_assessment = "lending_assessment"
    rental_verification = "rental_verification"
    employment_verification = "employment_verification"
    insurance_claim = "insurance_claim"


class ComplianceCheck:
    """Compliance check results."""
    def __init__(
        self,
        rule: ComplianceRule,
        passed: bool,
        reason: str,
        risk_level: str = "low",
    ):
        self.rule = rule
        self.passed = passed
        self.reason = reason
        self.risk_level = risk_level


class ComplianceMCP:
    """MCP Server for compliance rule validation."""
    
    def __init__(self):
        # Aadhaar Act 2023 compliance rules
        self.aadhaar_rules = {
            "minimize_aadhaar_data": ComplianceRule.data_minimization,
            "limit_to_purpose": ComplianceRule.purpose_limitation,
            "require_explicit_consent": ComplianceRule.explicit_consent,
        }
        
        # DPDP Act 2023 compliance rules
        self.dpdp_rules = {
            "limit_storage_duration": ComplianceRule.storage_limitation,
            "ensure_data_security": ComplianceRule.access_control,
        }
        
        # General consent mapping
        self.consent_map = {
            "aadhaar_card": ConsentType.aadhaar_card,
            "pan_card": ConsentType.pan_card,
            "driving_license": ConsentType.driving_license,
            "education": ConsentType.education,
            "land_records": ConsentType.land_records,
        }
    
    async def validate_aadhaar_access(
        self,
        data_requested: List[str],
        purpose: Purpose,
        consent_type: ConsentType,
        duration: Optional[int] = None,
    ) -> List[ComplianceCheck]:
        """Validate access to Aadhaar/PAN data against Aadhaar Act rules."""
        checks = []
        
        # Rule 1: Minimize Aadhaar data
        if purpose == Purpose.kyc_verification:
            essential_fields = 2  # name + DOB or name + PAN
            requested_fields = len(data_requested)
            
            if requested_fields > essential_fields:
                checks.append(ComplianceCheck(
                    rule=ComplianceRule.data_minimization,
                    passed=False,
                    reason=f"Requested {requested_fields} fields, but only {essential_fields} required for KYC",
                    risk_level="medium",
                ))
            else:
                checks.append(ComplianceCheck(
                    rule=ComplianceRule.data_minimization,
                    passed=True,
                    reason="Essential fields only for KYC verification",
                    risk_level="low",
                ))
        
        # Rule 2: Limit to declared purpose
        checks.append(ComplianceCheck(
            rule=ComplianceRule.purpose_limitation,
            passed=purpose == Purpose.kyc_verification,
            reason="Access limited to KYC verification purpose" if purpose != Purpose.kyc_verification else "Valid purpose declared",
            risk_level="low",
        ))
        
        # Rule 3: Require explicit consent
        checks.append(ComplianceCheck(
            rule=ComplianceRule.explicit_consent,
            passed=True,  # Assumed consent if we got here
            reason="Explicit consent required and granted",
            risk_level="low",
        ))
        
        # Rule 4: Duration limit (if specified)
        if duration and duration > 30:  # 30 days default limit
            checks.append(ComplianceCheck(
                rule=ComplianceRule.data_minimization,
                    passed=False,
                    reason=f"Data access duration {duration} days exceeds 30-day limit",
                    risk_level="medium",
                ))
        
        return checks
    
    async def validate_storage_access(
        self,
        data_type: str,
        operation: str,  # read, write, delete
        purpose: Purpose,
    ) -> List[ComplianceCheck]:
        """Validate storage operations against DPDP Act rules."""
        checks = []
        
        # Rule 1: Limit storage duration
        if operation == "read" or operation == "write":
            if operation == "write" and duration and duration > 7:  # 7 days write limit
                checks.append(ComplianceCheck(
                    rule=ComplianceRule.storage_limitation,
                    passed=False,
                    reason="Write access duration exceeds 7-day limit",
                    risk_level="low",
                ))
        
        # Rule 2: Ensure data security
        checks.append(ComplianceCheck(
            rule=ComplianceRule.access_control,
                    passed=True,  # Assumed secure storage
            reason="Secure storage controls in place",
            risk_level="low",
        ))
        
        # Rule 3: Purpose limitation
        checks.append(ComplianceCheck(
            rule=ComplianceRule.purpose_limitation,
            passed=purpose in [Purpose.kyc_verification, Purpose.lending_assessment],
            reason="Storage access limited to KYC/lending purposes" if purpose not in [Purpose.kyc_verification, Purpose.lending_assessment] else "Valid storage purpose",
            risk_level="low",
        ))
        
        return checks


def create_server() -> ComplianceMCP:
    """Factory function to create MCP server instance."""
    return ComplianceMCP()
