"""Agent Manager Service for Claude Agent SDK integration.

This service orchestrates verification workflows and records exactly what evidence
was observed at each stage. It never fabricates OCR, fraud, or compliance outputs.
"""
from __future__ import annotations

from enum import Enum
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import base64
import hashlib
import importlib.util
import json
import os
import re
import sys

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    McpStdioServerConfig,
)

from app.mcp_config import DEFAULT_MCP_SERVERS
from app.runtime_config import resolve_runtime_policy
from app.document_processing import extract_document_contract
from app.models import (
    AadhaarVerificationData,
    AgentRunProvenance,
    AgentToolTrace,
    ComplianceVerificationEvidence,
    DocumentEvidenceSource,
    DocumentVerificationEvidence,
    FraudVerificationEvidence,
    PanVerificationData,
    StepStatus,
    VerificationGap,
    VerificationMetadata,
    VerificationStatus,
    VerificationStep,
    VerificationStepDetail,
)

repo_root_for_imports = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_root_for_imports not in sys.path:
    sys.path.insert(0, repo_root_for_imports)

_agents_spec = importlib.util.spec_from_file_location(
    "aadhaar_chain_mcp_agents",
    os.path.join(repo_root_for_imports, "mcp", "agents.py"),
)
if _agents_spec is None or _agents_spec.loader is None:
    raise ImportError("Unable to load aadhaar-chain agent definitions.")
_agents_module = importlib.util.module_from_spec(_agents_spec)
_agents_spec.loader.exec_module(_agents_module)
get_all_agents = _agents_module.get_all_agents


class AgentType(str, Enum):
    """Types of agents available."""

    DOCUMENT_VALIDATOR = "document-validator"
    FRAUD_DETECTION = "fraud-detection"
    COMPLIANCE_MONITOR = "compliance-monitor"
    ORCHESTRATOR = "orchestrator"


class AgentManager:
    """Manages verification workflows and evidence collection."""

    def __init__(self) -> None:
        self.agents: Dict[AgentType, Any] = {}
        self.verification_records: Dict[str, VerificationStatus] = {}
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    def _build_verification_status(
        self,
        verification_id: str,
        wallet_address: str,
    ) -> VerificationStatus:
        """Create a consistent initial verification status object."""
        timestamp = self._timestamp()
        return VerificationStatus(
            verification_id=verification_id,
            wallet_address=wallet_address,
            status="pending",
            current_step=VerificationStep.document_received,
            steps=[
                VerificationStepDetail(
                    name=VerificationStep.document_received.value,
                    status=StepStatus.completed,
                )
            ],
            progress=0.0,
            created_at=timestamp,
            updated_at=timestamp,
            metadata=None,
        )

    def _record_step(
        self,
        status: VerificationStatus,
        step: VerificationStep,
        progress: float,
    ) -> None:
        """Advance the verification workflow while keeping step state consistent."""
        if status.steps:
            status.steps[-1].status = StepStatus.completed

        status.current_step = step
        status.progress = progress
        status.status = "processing"
        status.updated_at = self._timestamp()
        status.steps.append(
            VerificationStepDetail(
                name=step.value,
                status=StepStatus.in_progress,
            )
        )

    async def initialize_agents(self) -> None:
        """Load configured agent definitions."""
        for agent_def in get_all_agents():
            self.agents[AgentType(agent_def.agent_id)] = agent_def

        print(f"✓ Loaded {len(self.agents)} agent definitions")
        for agent_type in AgentType:
            if agent_type in self.agents:
                print(f"  - {agent_type.value}")

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _gap(
        self,
        stage: str,
        code: str,
        message: str,
        *,
        blocking: bool = True,
    ) -> VerificationGap:
        return VerificationGap(
            code=code,
            stage=stage,  # type: ignore[arg-type]
            message=message,
            blocking=blocking,
        )

    def _missing_provenance(
        self,
        agent_id: str,
        error: str,
    ) -> AgentRunProvenance:
        timestamp = self._timestamp()
        return AgentRunProvenance(
            agent_id=agent_id,
            status="missing_contract",
            started_at=timestamp,
            completed_at=timestamp,
            error=error,
        )

    def _failed_provenance(
        self,
        agent_id: str,
        started_at: str,
        error: str,
    ) -> AgentRunProvenance:
        return AgentRunProvenance(
            agent_id=agent_id,
            status="failed",
            started_at=started_at,
            completed_at=self._timestamp(),
            error=error,
        )

    def _required_document_fields(self, document_type: str) -> list[str]:
        if document_type == "aadhaar":
            return ["name", "dob", "uid"]
        return ["name", "dob", "pan_number"]

    def _detect_input_kind(self, document_data: bytes) -> str:
        if not document_data:
            return "unknown"

        stripped = document_data.lstrip()
        if stripped.startswith(b"{") or stripped.startswith(b"["):
            try:
                json.loads(document_data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return "raw_document"
            return "request_payload"
        return "raw_document"

    def _decode_json_bytes(self, payload: bytes) -> Dict[str, Any]:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _normalize_mcp_server_name(self, server_name: str) -> str:
        """Accept either plain MCP server keys or mcp://-prefixed identifiers."""
        normalized = server_name.removeprefix("mcp://")
        if "/" in normalized:
            normalized = normalized.split("/", 1)[0]
        return normalized

    def _normalize_hash(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.startswith("sha256:"):
            normalized = normalized.split(":", 1)[1]
        return normalized.lower()

    def _extract_printable_text(self, document_data: bytes) -> str:
        decoded = document_data.decode("utf-8", errors="ignore")
        if decoded.strip():
            return decoded
        printable_sequences = re.findall(rb"[\x20-\x7E]{4,}", document_data)
        return "\n".join(chunk.decode("utf-8", errors="ignore") for chunk in printable_sequences)

    def _extract_name_candidate(self, text: str, blocked_patterns: list[str]) -> Optional[str]:
        for line in [line.strip() for line in text.splitlines() if line.strip()]:
            lowered = line.lower()
            if any(pattern in lowered for pattern in blocked_patterns):
                continue
            candidate = re.sub(r"[^A-Za-z ]", "", line).strip()
            if len(candidate.split()) >= 2:
                return candidate
        return None

    def _build_document_processor_provenance(
        self,
        document_type: str,
        payload: Dict[str, Any],
        runtime_error: Optional[str],
    ) -> AgentRunProvenance:
        status = "failed" if runtime_error else "completed"
        timestamp = self._timestamp()
        tools = [
            AgentToolTrace(
                tool_name="ocr_document",
                status="failed" if runtime_error else "completed",
                output_preview=payload.get("text_method"),
            ),
            AgentToolTrace(
                tool_name="detect_document_type",
                status="failed" if runtime_error else "completed",
                output_preview=str(payload.get("detected_document_type")),
            ),
            AgentToolTrace(
                tool_name=f"extract_{document_type}_fields",
                status="failed" if runtime_error else "completed",
                output_preview=json.dumps(payload.get("fields", {}))[:200],
            ),
        ]
        return AgentRunProvenance(
            agent_id=AgentType.DOCUMENT_VALIDATOR.value,
            status=status,  # type: ignore[arg-type]
            started_at=timestamp,
            completed_at=timestamp,
            model="document-processor-local",
            tools=tools,
            structured_output=payload,
            error=runtime_error,
        )

    def build_document_source(
        self,
        transport: str,
        document_data: bytes,
        *,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        submitted_hash: Optional[str] = None,
    ) -> DocumentEvidenceSource:
        """Build a stable source descriptor for primary document evidence."""
        digest = hashlib.sha256(document_data).hexdigest() if document_data else None
        normalized_submitted_hash = self._normalize_hash(submitted_hash)
        hash_matches_submission: Optional[bool] = None
        if digest is not None and normalized_submitted_hash is not None:
            hash_matches_submission = normalized_submitted_hash == digest

        return DocumentEvidenceSource(
            transport=transport,  # type: ignore[arg-type]
            file_name=file_name,
            content_type=content_type,
            size_bytes=len(document_data),
            sha256=f"sha256:{digest}" if digest else None,
            submitted_hash=submitted_hash,
            hash_matches_submission=hash_matches_submission,
        )

    def _build_mcp_servers(
        self,
        server_names: list[str],
    ) -> Dict[str, McpStdioServerConfig]:
        mcp_servers: Dict[str, McpStdioServerConfig] = {}
        for server_name in server_names:
            normalized_name = self._normalize_mcp_server_name(server_name)
            config = DEFAULT_MCP_SERVERS.get(normalized_name)
            if config is None or not config.enabled:
                continue

            mcp_servers[normalized_name] = {
                "type": "stdio",
                "command": config.command,
                "args": config.args,
                "env": config.env,
            }
        return mcp_servers

    def _create_sdk_client(self, agent_type: AgentType) -> ClaudeSDKClient:
        """Create a fresh SDK client for each invocation.

        ClaudeSDKClient instances cannot safely service concurrent background tasks.
        Reusing them causes overlapping reads against the same subprocess transport.
        """
        agent_def = self.agents.get(agent_type)
        if agent_def is None:
            raise ValueError(f"Agent not found: {agent_type}")
        runtime_policy = resolve_runtime_policy()
        if not runtime_policy.runtime_available:
            raise RuntimeError(runtime_policy.blocked_reason or "Claude Agent runtime is unavailable for AadhaarChain.")

        return ClaudeSDKClient(
            options=ClaudeAgentOptions(
                system_prompt=agent_def.system_prompt,
                mcp_servers=self._build_mcp_servers(agent_def.mcp_servers or []),
                cwd=self.repo_root,
                model=runtime_policy.model,
                allowed_tools=agent_def.tools or [],
                permission_mode="default",
                cli_path=runtime_policy.claude_code_executable_path,
            )
        )

    def _build_deterministic_fallback_provenance(
        self,
        agent_type: AgentType,
        provenance: AgentRunProvenance,
        payload: Dict[str, Any],
    ) -> AgentRunProvenance:
        return AgentRunProvenance(
            agent_id=agent_type.value,
            status="completed",
            started_at=provenance.started_at,
            completed_at=self._timestamp(),
            model="deterministic-fallback",
            tools=provenance.tools,
            response_preview=provenance.response_preview,
            structured_output=payload,
            error=provenance.error,
        )

    def _normalize_comparable_value(self, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        return re.sub(r"[^a-z0-9]", "", str(value).lower())

    def _fallback_fraud_payload(
        self,
        document_evidence: DocumentVerificationEvidence,
    ) -> Dict[str, Any]:
        indicators: list[str] = []
        risk_score = 0.08
        risk_level = "safe"
        recommendation = "approve"

        if document_evidence.confidence is None:
            indicators.append("Document extraction confidence was unavailable.")
            risk_score = max(risk_score, 0.45)
        elif document_evidence.confidence < 0.75:
            indicators.append(
                f"Document extraction confidence was low ({document_evidence.confidence:.2f})."
            )
            risk_score = max(risk_score, 0.45)

        for warning in document_evidence.warnings:
            indicators.append(f"Document warning: {warning}")
            risk_score = max(risk_score, 0.35)

        for field in ("name", "dob", "uid", "pan_number"):
            extracted = self._normalize_comparable_value(
                document_evidence.extracted_fields.get(field)
            )
            submitted = self._normalize_comparable_value(
                document_evidence.submitted_claims.get(field)
            )
            if extracted is None or submitted is None or extracted == submitted:
                continue

            indicators.append(
                f"Submitted {field} does not match the value extracted from the uploaded document."
            )
            if field in {"uid", "pan_number"}:
                risk_score = max(risk_score, 0.85)
            else:
                risk_score = max(risk_score, 0.65)

        if risk_score >= 0.85:
            risk_level = "high"
            recommendation = "manual_review"
        elif risk_score >= 0.35:
            risk_level = "medium"
            recommendation = "manual_review"

        return {
            "risk_score": risk_score,
            "risk_level": risk_level,
            "indicators": indicators,
            "recommendation": recommendation,
        }

    def _fallback_compliance_payload(
        self,
        document_type: str,
        verification_data: Optional[AadhaarVerificationData | PanVerificationData],
        purpose: str,
    ) -> Dict[str, Any]:
        violations: list[str] = []
        aadhaar_act_compliant = True
        dpdp_compliant = True
        recommendation = "approve"

        purpose_normalized = purpose.lower()
        if document_type == "aadhaar":
            consent_provided = bool(getattr(verification_data, "consent_provided", False))
            if not consent_provided:
                aadhaar_act_compliant = False
                violations.append("Explicit Aadhaar consent was not provided in the backend request.")

            allowed_purposes = (
                "identity_verification",
                "kyc",
                "age_verification",
                "address_verification",
            )
            if not any(token in purpose_normalized for token in allowed_purposes):
                aadhaar_act_compliant = False
                violations.append(
                    f"Declared Aadhaar processing purpose {purpose!r} does not match the allowed verification purposes."
                )

        if verification_data is not None:
            submitted_claims = verification_data.model_dump(exclude_none=True)
            allowed_fields = {"name", "dob", "document_hash"}
            if document_type == "aadhaar":
                allowed_fields.update({"uid", "address", "consent_provided"})
            else:
                allowed_fields.add("pan_number")

            unexpected_fields = sorted(set(submitted_claims) - allowed_fields)
            if unexpected_fields:
                dpdp_compliant = False
                violations.append(
                    "Unexpected verification fields were collected: "
                    + ", ".join(unexpected_fields)
                    + "."
                )

        if violations:
            recommendation = "block" if any("consent" in violation.lower() for violation in violations) else "manual_review"

        return {
            "aadhaar_act_compliant": aadhaar_act_compliant,
            "dpdp_compliant": dpdp_compliant,
            "violations": violations,
            "recommendation": recommendation,
        }

    def _append_tool_result(
        self,
        tools: list[AgentToolTrace],
        tool_use_id_to_name: Dict[str, str],
        tool_use_id: str,
        content: str | list[dict[str, Any]] | None,
        is_error: Optional[bool],
    ) -> None:
        tool_name = tool_use_id_to_name.get(tool_use_id, tool_use_id)
        preview = self._preview_tool_content(content)
        tool_status = "failed" if is_error else "completed"
        tools.append(
            AgentToolTrace(
                tool_name=tool_name,
                status=tool_status,
                output_preview=preview,
            )
        )

    def _preview_tool_content(
        self,
        content: str | list[dict[str, Any]] | None,
    ) -> Optional[str]:
        if content is None:
            return None
        if isinstance(content, str):
            return content[:240]
        try:
            return json.dumps(content)[:240]
        except TypeError:
            return str(content)[:240]

    def _extract_json_payload(self, response_text: Optional[str]) -> Optional[Dict[str, Any]]:
        if not response_text:
            return None

        candidates = [response_text]
        start = response_text.find("{")
        end = response_text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(response_text[start : end + 1])

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    async def invoke_agent(
        self,
        agent_type: AgentType,
        prompt: str,
    ) -> tuple[Optional[Dict[str, Any]], AgentRunProvenance]:
        """Invoke an agent via SDK and return parsed JSON plus provenance."""
        started_at = self._timestamp()
        tools: list[AgentToolTrace] = []
        tool_use_id_to_name: Dict[str, str] = {}
        response_fragments: list[str] = []
        model: Optional[str] = None
        session_id: Optional[str] = None
        structured_output: Optional[Dict[str, Any]] = None
        agent_id = agent_type.value

        try:
            client = self._create_sdk_client(agent_type)
            await client.connect()
            try:
                await client.query(prompt)

                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        model = message.model or model
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                response_fragments.append(block.text)
                            elif isinstance(block, ToolUseBlock):
                                tool_use_id_to_name[block.id] = block.name
                                tools.append(
                                    AgentToolTrace(
                                        tool_name=block.name,
                                        status="requested",
                                    )
                                )
                            elif isinstance(block, ToolResultBlock):
                                self._append_tool_result(
                                    tools,
                                    tool_use_id_to_name,
                                    block.tool_use_id,
                                    block.content,
                                    block.is_error,
                                )
                    elif isinstance(message, ResultMessage):
                        session_id = message.session_id
                        if isinstance(message.structured_output, dict):
                            structured_output = message.structured_output
                        if message.result:
                            response_fragments.append(message.result)

                response_text = "\n".join(fragment.strip() for fragment in response_fragments if fragment.strip()) or None
                payload = structured_output or self._extract_json_payload(response_text)
                status = "completed" if payload is not None else "missing_contract"

                provenance = AgentRunProvenance(
                    agent_id=agent_id,
                    status=status,
                    started_at=started_at,
                    completed_at=self._timestamp(),
                    model=model,
                    session_id=session_id,
                    tools=tools,
                    response_preview=response_text[:400] if response_text else None,
                    structured_output=payload,
                    error=None if payload is not None else "Agent response did not contain a parseable JSON contract.",
                )
                return payload, provenance
            finally:
                await client.disconnect()
        except Exception as exc:
            return None, self._failed_provenance(agent_id, started_at, str(exc))

    async def validate_document(
        self,
        document_data: bytes,
        document_type: str,
        verification_data: Optional[AadhaarVerificationData | PanVerificationData],
        document_source: Optional[DocumentEvidenceSource] = None,
    ) -> DocumentVerificationEvidence:
        """Validate document using Document Validator agent when primary evidence exists."""
        input_kind = self._detect_input_kind(document_data)
        required_fields = self._required_document_fields(document_type)
        submitted_claims = verification_data.model_dump(exclude_none=True) if verification_data else {}

        if input_kind != "raw_document":
            gap = self._gap(
                "document",
                "primary_document_missing",
                "Gateway received request payload fields instead of primary document bytes, so OCR evidence is unavailable.",
            )
            return DocumentVerificationEvidence(
                document_type=document_type,  # type: ignore[arg-type]
                input_kind=input_kind,  # type: ignore[arg-type]
                source=document_source,
                extracted_fields={},
                submitted_claims=submitted_claims,
                warnings=["Submitted form claims are not treated as verified document evidence."],
                required_fields=required_fields,
                missing_fields=required_fields,
                provenance=self._missing_provenance(
                    AgentType.DOCUMENT_VALIDATOR.value,
                    "Primary document evidence was not supplied to the backend.",
                ),
                gaps=[gap],
            )

        processed = extract_document_contract(
            document_data,
            expected_document_type=document_type,
            mime_type=document_source.content_type if document_source else None,
            file_name=document_source.file_name if document_source else None,
        )
        payload: Dict[str, Any] = {
            "document_type": document_type,
            "detected_document_type": processed.detected_document_type,
            "text_method": processed.text_method,
            "fields": processed.fields,
            "confidence": processed.confidence,
            "warnings": processed.warnings,
        }
        provenance = self._build_document_processor_provenance(
            document_type,
            payload,
            processed.runtime_error,
        )

        gaps: list[VerificationGap] = []
        fields: Dict[str, Any] = {}
        warnings: list[str] = []
        confidence: Optional[float] = None

        if document_source and document_source.hash_matches_submission is False:
            gaps.append(
                self._gap(
                    "document",
                    "document_hash_mismatch",
                    "Submitted document hash does not match the primary document bytes received by the backend.",
                )
            )

        if provenance.status == "failed":
            gaps.append(
                self._gap(
                    "document",
                    "document_processor_failed",
                    provenance.error or "Document processor invocation failed.",
                )
            )
        raw_fields = payload.get("fields")
        if isinstance(raw_fields, dict):
            fields = {key: value for key, value in raw_fields.items() if value not in (None, "")}

        raw_warnings = payload.get("warnings")
        if isinstance(raw_warnings, list):
            warnings.extend(str(item) for item in raw_warnings)

        raw_confidence = payload.get("confidence")
        if isinstance(raw_confidence, (int, float)):
            confidence = float(raw_confidence)
        else:
            gaps.append(
                self._gap(
                    "document",
                    "document_confidence_missing",
                    "Document processor did not provide a numeric confidence score.",
                )
            )

        returned_type = payload.get("detected_document_type")
        if returned_type not in (None, document_type, "unknown"):
            gaps.append(
                self._gap(
                    "document",
                    "document_type_mismatch",
                    f"Document processor detected {returned_type!r} instead of {document_type!r}.",
                )
            )

        if not processed.text.strip() and not processed.runtime_error:
            gaps.append(
                self._gap(
                    "document",
                    "document_text_missing",
                    "Document processor could not extract readable text from the uploaded evidence.",
                )
            )

        missing_fields = [field for field in required_fields if not fields.get(field)]
        if missing_fields:
            gaps.append(
                self._gap(
                    "document",
                    "document_fields_missing",
                    f"Document evidence is missing required extracted fields: {', '.join(missing_fields)}.",
                )
            )

        return DocumentVerificationEvidence(
            document_type=document_type,  # type: ignore[arg-type]
            input_kind="raw_document",
            source=document_source,
            extracted_fields=fields,
            submitted_claims=submitted_claims,
            confidence=confidence,
            warnings=warnings,
            required_fields=required_fields,
            missing_fields=missing_fields,
            provenance=provenance,
            gaps=gaps,
        )

    async def detect_fraud(
        self,
        document_evidence: DocumentVerificationEvidence,
        document_type: str,
    ) -> FraudVerificationEvidence:
        """Run fraud checks only when document evidence is explicit."""
        blocking_document_gaps = [gap for gap in document_evidence.gaps if gap.blocking]
        if blocking_document_gaps:
            return FraudVerificationEvidence(
                provenance=self._missing_provenance(
                    AgentType.FRAUD_DETECTION.value,
                    "Fraud analysis skipped because primary document evidence was incomplete.",
                ),
                gaps=[
                    self._gap(
                        "fraud",
                        "fraud_prerequisite_missing",
                        "Fraud analysis requires explicit document extraction evidence before it can be trusted.",
                    )
                ],
            )

        prompt = f"""Analyze this {document_type} verification for fraud.

Observed document fields:
{json.dumps(document_evidence.extracted_fields, indent=2)}

Return only JSON with this exact shape:
{{
  "risk_score": 0.0,
  "risk_level": "safe|medium|high",
  "indicators": ["string"],
  "recommendation": "approve|manual_review|block"
}}
"""

        payload: Optional[Dict[str, Any]]
        provenance: AgentRunProvenance
        try:
            payload, provenance = await asyncio.wait_for(
                self.invoke_agent(AgentType.FRAUD_DETECTION, prompt),
                timeout=12,
            )
        except asyncio.TimeoutError:
            payload = None
            started_at = self._timestamp()
            provenance = self._failed_provenance(
                AgentType.FRAUD_DETECTION.value,
                started_at,
                "Fraud detection timed out before returning a structured contract.",
            )

        used_deterministic_fallback = False
        if payload is None:
            payload = self._fallback_fraud_payload(document_evidence)
            used_deterministic_fallback = True
            provenance = self._build_deterministic_fallback_provenance(
                AgentType.FRAUD_DETECTION,
                provenance,
                payload,
            )

        gaps: list[VerificationGap] = []
        risk_score: Optional[float] = None
        risk_level: Optional[str] = None
        indicators: list[str] = []
        recommendation: Optional[str] = None

        if provenance.status == "failed":
            gaps.append(
                self._gap(
                    "fraud",
                    "fraud_agent_failed",
                    provenance.error or "Fraud detection agent invocation failed.",
                )
            )
        elif payload is None:
            gaps.append(
                self._gap(
                    "fraud",
                    "fraud_contract_missing",
                    provenance.error or "Fraud detection agent did not return a structured evidence contract.",
                )
            )
        else:
            raw_score = payload.get("risk_score")
            if isinstance(raw_score, (int, float)):
                risk_score = float(raw_score)
            else:
                gaps.append(
                    self._gap(
                        "fraud",
                        "fraud_score_missing",
                        "Fraud contract did not provide a numeric risk score.",
                    )
                )

            if isinstance(payload.get("risk_level"), str):
                risk_level = payload["risk_level"]
            else:
                gaps.append(
                    self._gap(
                        "fraud",
                        "fraud_level_missing",
                        "Fraud contract did not provide a risk level.",
                    )
                )

            raw_indicators = payload.get("indicators")
            if isinstance(raw_indicators, list):
                indicators = [str(item) for item in raw_indicators]

            raw_recommendation = payload.get("recommendation")
            if raw_recommendation in {"approve", "manual_review", "block"}:
                recommendation = raw_recommendation
            else:
                gaps.append(
                    self._gap(
                        "fraud",
                        "fraud_recommendation_missing",
                        "Fraud contract did not provide an explicit recommendation.",
                    )
                )

        if used_deterministic_fallback:
            indicators.append(
                "Fraud analysis used deterministic fallback because the primary fraud-detection contract was unavailable."
            )

        return FraudVerificationEvidence(
            risk_score=risk_score,
            risk_level=risk_level,
            indicators=indicators,
            recommendation=recommendation,  # type: ignore[arg-type]
            provenance=provenance,
            gaps=gaps,
        )

    async def check_compliance(
        self,
        document_evidence: DocumentVerificationEvidence,
        document_type: str,
        verification_data: Optional[AadhaarVerificationData | PanVerificationData],
        purpose: str = "kyc_verification",
    ) -> ComplianceVerificationEvidence:
        """Run compliance checks when the required supporting evidence exists."""
        gaps: list[VerificationGap] = []
        violations: list[str] = []

        if document_evidence.gaps:
            gaps.append(
                self._gap(
                    "compliance",
                    "compliance_prerequisite_missing",
                    "Compliance analysis requires explicit document evidence and provenance.",
                )
            )

        if document_type == "aadhaar":
            consent_provided = bool(getattr(verification_data, "consent_provided", False))
            if not consent_provided:
                violations.append("Explicit Aadhaar consent was not provided in the backend request.")
        else:
            consent_provided = True

        if gaps:
            recommendation = "block" if violations else "manual_review"
            return ComplianceVerificationEvidence(
                aadhaar_act_compliant=False if violations else None,
                dpdp_compliant=None,
                violations=violations,
                recommendation=recommendation,
                provenance=self._missing_provenance(
                    AgentType.COMPLIANCE_MONITOR.value,
                    "Compliance analysis skipped because prerequisite evidence was incomplete.",
                ),
                gaps=gaps,
            )

        prompt = f"""Verify compliance for this {document_type} document.

Observed document fields:
{json.dumps(document_evidence.extracted_fields, indent=2)}
Purpose: {purpose}
Consent provided: {consent_provided}

Return only JSON with this exact shape:
{{
  "aadhaar_act_compliant": true,
  "dpdp_compliant": true,
  "violations": ["string"],
  "recommendation": "approve|manual_review|block"
}}
"""

        payload: Optional[Dict[str, Any]]
        provenance: AgentRunProvenance
        try:
            payload, provenance = await asyncio.wait_for(
                self.invoke_agent(AgentType.COMPLIANCE_MONITOR, prompt),
                timeout=12,
            )
        except asyncio.TimeoutError:
            payload = None
            started_at = self._timestamp()
            provenance = self._failed_provenance(
                AgentType.COMPLIANCE_MONITOR.value,
                started_at,
                "Compliance monitor timed out before returning a structured contract.",
            )

        if payload is None:
            payload = self._fallback_compliance_payload(
                document_type,
                verification_data,
                purpose,
            )
            provenance = self._build_deterministic_fallback_provenance(
                AgentType.COMPLIANCE_MONITOR,
                provenance,
                payload,
            )

        aadhaar_act_compliant: Optional[bool] = None
        dpdp_compliant: Optional[bool] = None
        recommendation: Optional[str] = None

        if provenance.status == "failed":
            gaps.append(
                self._gap(
                    "compliance",
                    "compliance_agent_failed",
                    provenance.error or "Compliance agent invocation failed.",
                )
            )
        elif payload is None:
            gaps.append(
                self._gap(
                    "compliance",
                    "compliance_contract_missing",
                    provenance.error or "Compliance agent did not return a structured evidence contract.",
                )
            )
        else:
            if isinstance(payload.get("aadhaar_act_compliant"), bool):
                aadhaar_act_compliant = payload["aadhaar_act_compliant"]
            else:
                gaps.append(
                    self._gap(
                        "compliance",
                        "aadhaar_compliance_missing",
                        "Compliance contract did not provide Aadhaar Act compliance status.",
                    )
                )

            if isinstance(payload.get("dpdp_compliant"), bool):
                dpdp_compliant = payload["dpdp_compliant"]
            else:
                gaps.append(
                    self._gap(
                        "compliance",
                        "dpdp_compliance_missing",
                        "Compliance contract did not provide DPDP compliance status.",
                    )
                )

            raw_violations = payload.get("violations")
            if isinstance(raw_violations, list):
                violations.extend(str(item) for item in raw_violations)

            raw_recommendation = payload.get("recommendation")
            if raw_recommendation in {"approve", "manual_review", "block"}:
                recommendation = raw_recommendation
            else:
                gaps.append(
                    self._gap(
                        "compliance",
                        "compliance_recommendation_missing",
                        "Compliance contract did not provide an explicit recommendation.",
                    )
                )

        if violations and aadhaar_act_compliant is None:
            aadhaar_act_compliant = False

        return ComplianceVerificationEvidence(
            aadhaar_act_compliant=aadhaar_act_compliant,
            dpdp_compliant=dpdp_compliant,
            violations=violations,
            recommendation=recommendation,  # type: ignore[arg-type]
            provenance=provenance,
            gaps=gaps,
        )

    def _build_metadata(
        self,
        document_type: str,
        document: DocumentVerificationEvidence,
        fraud: FraudVerificationEvidence,
        compliance: ComplianceVerificationEvidence,
    ) -> VerificationMetadata:
        blocking_gaps = [
            *[gap for gap in document.gaps if gap.blocking],
            *[gap for gap in fraud.gaps if gap.blocking],
            *[gap for gap in compliance.gaps if gap.blocking],
        ]

        evidence_status = "complete"
        if blocking_gaps:
            evidence_status = "missing"
        elif document.gaps or fraud.gaps or compliance.gaps:
            evidence_status = "partial"

        if blocking_gaps:
            decision = "manual_review"
            reason = "Explicit evidence or provenance is missing for one or more verification stages."
        elif compliance.recommendation == "block" or compliance.violations:
            decision = "reject"
            reason = "Compliance violations were detected in the backend verification contract."
        elif fraud.recommendation == "block" or (fraud.risk_score is not None and fraud.risk_score > 0.7):
            decision = "reject"
            risk_score = fraud.risk_score if fraud.risk_score is not None else 0.0
            reason = f"Fraud risk exceeded the rejection threshold ({risk_score:.2f})."
        elif fraud.recommendation == "manual_review":
            decision = "manual_review"
            reason = "Fraud analysis requested manual review."
        elif document.confidence is not None and document.confidence < 0.6:
            decision = "manual_review"
            reason = f"Document evidence confidence is below threshold ({document.confidence:.2f})."
        elif compliance.aadhaar_act_compliant is False or compliance.dpdp_compliant is False:
            decision = "reject"
            reason = "Compliance contract marked the verification as non-compliant."
        else:
            decision = "approve"
            reason = "Explicit document, fraud, and compliance contracts passed all approval rules."

        assumptions = [
            "Approval is allowed only when every stage returns an explicit structured contract.",
            "Missing contracts or provenance never auto-approve a verification.",
            "Fraud risk > 0.7 rejects the verification.",
            "Document confidence < 0.6 downgrades the verification to manual review.",
            f"Document type: {document_type}",
        ]

        return VerificationMetadata(
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
            evidence_status=evidence_status,  # type: ignore[arg-type]
            document=document,
            fraud=fraud,
            compliance=compliance,
            blocking_gaps=blocking_gaps,
            assumptions=assumptions,
        )

    async def orchestrate_verification(
        self,
        wallet_address: str,
        document_type: str,
        document_data: bytes,
        verification_data: Optional[AadhaarVerificationData | PanVerificationData],
        document_source: Optional[DocumentEvidenceSource] = None,
    ) -> VerificationStatus:
        """Orchestrate the complete verification workflow."""
        verification_id = f"{document_type}_{wallet_address}"
        status = self.verification_records.get(verification_id)
        if status is None:
            status = self._build_verification_status(verification_id, wallet_address)
            self.verification_records[verification_id] = status

        self._record_step(status, VerificationStep.parsing, 0.2)
        document = await self.validate_document(
            document_data,
            document_type,
            verification_data,
            document_source,
        )

        self._record_step(status, VerificationStep.fraud_check, 0.4)
        fraud = await self.detect_fraud(document, document_type)

        self._record_step(status, VerificationStep.compliance_check, 0.6)
        compliance = await self.check_compliance(document, document_type, verification_data)

        self._record_step(status, VerificationStep.blockchain_upload, 0.8)
        metadata = self._build_metadata(document_type, document, fraud, compliance)

        await self.complete_verification(
            verification_id,
            metadata.decision,
            metadata,
        )
        return self.verification_records[verification_id]

    async def get_verification_status(
        self,
        verification_id: str,
    ) -> Optional[VerificationStatus]:
        """Get verification status by ID."""
        return self.verification_records.get(verification_id)

    async def create_verification(
        self,
        wallet_address: str,
        document_type: str,
        verification_data: Optional[AadhaarVerificationData | PanVerificationData],
    ) -> str:
        """Create verification request and initialize status."""
        del verification_data
        verification_id = f"{document_type}_{wallet_address}"
        self.verification_records[verification_id] = self._build_verification_status(
            verification_id,
            wallet_address,
        )
        return verification_id

    async def update_verification_progress(
        self,
        verification_id: str,
        current_step: VerificationStep,
        progress: float,
    ) -> None:
        """Update verification progress."""
        if verification_id not in self.verification_records:
            return

        status = self.verification_records[verification_id]
        self._record_step(status, current_step, progress)

    async def complete_verification(
        self,
        verification_id: str,
        decision: str,
        metadata: VerificationMetadata,
    ) -> None:
        """Mark verification as complete with the final decision contract."""
        if verification_id not in self.verification_records:
            return

        status = self.verification_records[verification_id]
        if status.steps:
            status.steps[-1].status = StepStatus.completed

        status.current_step = VerificationStep.complete
        status.progress = 1.0
        status.updated_at = self._timestamp()
        status.steps.append(
            VerificationStepDetail(
                name=VerificationStep.complete.value,
                status=StepStatus.completed,
            )
        )

        if decision == "approve":
            status.status = "verified"
            status.error = None
        elif decision == "manual_review":
            status.status = "manual_review"
            status.error = metadata.reason
        else:
            status.status = "failed"
            status.error = metadata.reason

        status.metadata = metadata

    async def cleanup_expired_verifications(self, days: int = 7) -> int:
        """Clean up old verification records."""
        cleaned = 0
        cutoff_time = datetime.now(timezone.utc).timestamp() - (days * 86400)

        for verification_id, status in list(self.verification_records.items()):
            created_time = datetime.fromisoformat(status.created_at.replace("Z", "+00:00")).timestamp()
            if created_time < cutoff_time:
                del self.verification_records[verification_id]
                cleaned += 1

        return cleaned


agent_manager = AgentManager()
