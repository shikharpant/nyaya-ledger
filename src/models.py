"""
Core Pydantic models for the Legal AST.

This module defines the data structures for:
- Legal nodes (Rule, SubRule, Proviso, Explanation)
- Form nodes (Form, FormSection with JSON Schema payload)
- Mutation operations (the "compiler output")
- Semantic edges (cross-references, mutatis mutandis, etc.)
"""

from datetime import datetime
from typing import Optional, Literal, Any
from pydantic import BaseModel, Field
from enum import Enum


# =============================================================================
# ENUMS
# =============================================================================

class NodeType(str, Enum):
    """Types of nodes in the Legal AST."""
    STATUTE = "statute"
    CHAPTER = "chapter"
    RULE = "rule"
    SUBRULE = "subrule"
    SECTION = "section"
    SUBSECTION = "subsection"
    CLAUSE = "clause"
    PROVISO = "proviso"
    EXPLANATION = "explanation"
    FORM = "form"
    FORM_SECTION = "form_section"


class OperationType(str, Enum):
    """Types of mutation operations (the "compiler" instructions)."""
    INSERT_CHILD = "INSERT_CHILD"       # Add a new child node
    INSERT_SIBLING = "INSERT_SIBLING"   # Add sibling after/before another
    SPLICE = "SPLICE"                   # Insert text at anchor position
    SUBSTITUTE = "SUBSTITUTE"           # Replace text pattern
    DELETE = "DELETE"                   # Remove a node
    OMIT = "OMIT"                       # Legal "omission" (different from delete)
    REPEAL = "REPEAL"                   # Full repeal of a provision
    RENAME = "RENAME"                   # Change label/heading


class EdgeType(str, Enum):
    """Types of semantic edges between nodes."""
    REQUIRES_FORM = "REQUIRES_FORM"           # Rule requires a form
    MUTATIS_MUTANDIS = "MUTATIS_MUTANDIS"     # Inherit logic with modifications
    SUBJECT_TO = "SUBJECT_TO"                 # This provision is subject to another
    NOTWITHSTANDING = "NOTWITHSTANDING"       # This overrides another
    READ_WITH = "READ_WITH"                   # Must be read together
    REFERS_TO = "REFERS_TO"                   # Simple cross-reference
    SUPERSEDES = "SUPERSEDES"                 # Explicitly replaces another
    DEFINED_BY = "DEFINED_BY"                 # Form is defined by a rule


class FormPurpose(str, Enum):
    """Purpose for which a form is required."""
    REGISTRATION = "REGISTRATION"
    AMENDMENT = "AMENDMENT"
    CANCELLATION = "CANCELLATION"
    RETURN_FILING = "RETURN_FILING"
    REFUND = "REFUND"
    APPEAL = "APPEAL"
    DECLARATION = "DECLARATION"
    INTIMATION = "INTIMATION"


class SectionType(str, Enum):
    """Type of form section."""
    FIELDS = "fields"              # Regular form fields
    TABLE = "table"                # Tabular data with columns
    INSTRUCTIONS = "instructions"  # Instructional text
    DECLARATION = "declaration"    # Declaration/undertaking
    ATTACHMENT = "attachment"      # Document upload section


class FormTableType(str, Enum):
    """Type of table in a form."""
    STATIC = "static"              # Fixed number of rows
    DYNAMIC = "dynamic"            # User can add/remove rows
    COMPUTED = "computed"          # Values calculated from other fields
    REFERENCE = "reference"        # Reference table (e.g., HSN codes)


# =============================================================================
# BASE NODE MODELS
# =============================================================================

class BaseNode(BaseModel):
    """Base class for all Legal AST nodes."""
    id: str = Field(..., description="Unique identifier, e.g., 'CGST_Rules/Rule_8'")
    node_type: NodeType
    version: str = Field(default="v1", description="Version identifier")
    valid_from: datetime = Field(..., description="When this version became effective")
    valid_to: Optional[datetime] = Field(None, description="When superseded (null = current)")
    content_hash: Optional[str] = Field(None, description="SHA256 of canonical content")
    source_mutation: Optional[str] = Field(None, description="Mutation that created this version")


class RuleNode(BaseNode):
    """A Rule in the Legal AST (e.g., Rule 8 of CGST Rules)."""
    node_type: Literal[NodeType.RULE] = NodeType.RULE
    label: str = Field(..., description="Rule number, e.g., '8' or '14A'")
    heading: str = Field(..., description="Rule title")
    content: str = Field(..., description="Full text of the rule (without sub-rules)")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": "CGST_Rules/Rule_8",
                "label": "8",
                "heading": "Application for registration",
                "content": "Every person... shall declare...",
                "version": "v1",
                "valid_from": "2017-07-01T00:00:00Z"
            }
        }


class SubRuleNode(BaseNode):
    """A Sub-rule within a Rule."""
    node_type: Literal[NodeType.SUBRULE] = NodeType.SUBRULE
    label: str = Field(..., description="Sub-rule label, e.g., '(1)' or '(4A)'")
    content: str = Field(..., description="Full text of the sub-rule")
    parent_rule: str = Field(..., description="ID of parent rule")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": "CGST_Rules/Rule_8/SubRule_1",
                "label": "(1)",
                "content": "Every person... in Part A of FORM GST REG-01...",
                "parent_rule": "CGST_Rules/Rule_8"
            }
        }


class SectionNode(BaseNode):
    """A Section in an Act (e.g., Section 25 of CGST Act)."""
    node_type: Literal[NodeType.SECTION] = NodeType.SECTION
    label: str = Field(..., description="Section number, e.g., '25' or '164'")
    heading: str = Field(..., description="Section title")
    content: str = Field(..., description="Full text of the section")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": "CGST_Act_2017/Section_25",
                "label": "25",
                "heading": "Procedure for registration",
                "content": "Every person...",
                "version": "v1",
                "valid_from": "2017-07-01T00:00:00Z"
            }
        }


class SubSectionNode(BaseNode):
    """A Sub-section within a Section."""
    node_type: Literal[NodeType.SUBSECTION] = NodeType.SUBSECTION
    label: str = Field(..., description="Sub-section label, e.g., '(1)'")
    content: str = Field(..., description="Full text of the sub-section")
    parent_section: str = Field(..., description="ID of parent section")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": "CGST_Act_2017/Section_25/SubSection_1",
                "label": "(1)",
                "content": "Every person...",
                "parent_section": "CGST_Act_2017/Section_25"
            }
        }


class ProvisoNode(BaseNode):
    """A Proviso attached to a Rule or Sub-rule."""
    node_type: Literal[NodeType.PROVISO] = NodeType.PROVISO
    label: str = Field(..., description="e.g., 'First Proviso', 'Second Proviso'")
    content: str = Field(..., description="Full text starting with 'Provided that...'")
    parent_node: str = Field(..., description="ID of parent (rule or sub-rule)")
    ordinal: int = Field(..., description="1 for first proviso, 2 for second, etc.")


class ExplanationNode(BaseNode):
    """An Explanation attached to a provision."""
    node_type: Literal[NodeType.EXPLANATION] = NodeType.EXPLANATION
    label: str = Field(default="Explanation", description="e.g., 'Explanation 1'")
    content: str
    parent_node: str


# =============================================================================
# FORM MODELS (Hybrid: Graph Node + JSON Schema Payload)
# =============================================================================

class FormNode(BaseNode):
    """A Form (e.g., FORM GST REG-01)."""
    node_type: Literal[NodeType.FORM] = NodeType.FORM
    form_number: str = Field(..., description="Official form number, e.g., 'GST REG-01'")
    title: str = Field(..., description="Form title")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": "FORM_GST_REG_01",
                "form_number": "GST REG-01",
                "title": "Application for Registration",
                "version": "v1",
                "valid_from": "2017-07-01T00:00:00Z"
            }
        }


class FormSectionNode(BaseNode):
    """
    A Section within a Form (e.g., Part A, Part B, Table 4A).
    
    HYBRID DESIGN:
    - Fields are stored as JSON Schema in `schema_payload`
    - Tables have explicit column definitions for amendment tracking
    - Supports complex GST forms with multi-column tables
    """
    node_type: Literal[NodeType.FORM_SECTION] = NodeType.FORM_SECTION
    section_label: str = Field(..., description="e.g., 'Part A', 'Table 4A'")
    heading: Optional[str] = Field(None, description="Section heading")
    parent_form: str = Field(..., description="ID of parent form")
    
    # Section type classification
    section_type: SectionType = Field(
        default=SectionType.FIELDS,
        description="Type of section: fields, table, instructions, etc."
    )
    
    # Table-specific fields (only used when section_type == TABLE)
    table_type: Optional[FormTableType] = Field(
        None, 
        description="Type of table: static, dynamic, computed"
    )
    table_columns: Optional[list[str]] = Field(
        None,
        description="Column headers for table sections, e.g., ['S.No.', 'Invoice No.', 'Value']"
    )
    table_column_widths: Optional[list[str]] = Field(
        None,
        description="Optional column widths, e.g., ['5%', '30%', '15%']"
    )
    has_serial_column: bool = Field(
        default=False,
        description="Whether table has auto-increment S.No. column"
    )
    has_total_row: bool = Field(
        default=False,
        description="Whether table has a totals row"
    )
    
    # The actual schema definition
    schema_payload: dict = Field(
        ..., 
        description="JSON Schema defining fields/rows in this section"
    )
    
    # Optional instructions or notes for this section
    instructions: Optional[str] = Field(
        None,
        description="Instructions or notes for filling this section"
    )
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": "FORM_GSTR_1/Table_4A",
                "section_label": "Table 4A",
                "heading": "Supplies made to registered persons",
                "parent_form": "FORM_GSTR_1",
                "section_type": "table",
                "table_type": "dynamic",
                "table_columns": [
                    "GSTIN of recipient",
                    "Invoice No.",
                    "Invoice Date",
                    "Invoice Value",
                    "Place of Supply",
                    "Rate",
                    "Taxable Value",
                    "IGST",
                    "CGST",
                    "SGST/UTGST"
                ],
                "has_serial_column": True,
                "has_total_row": True,
                "schema_payload": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "gstin": {"type": "string", "pattern": "^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"},
                            "invoice_no": {"type": "string"},
                            "invoice_date": {"type": "string", "format": "date"},
                            "invoice_value": {"type": "number"},
                            "place_of_supply": {"type": "string"},
                            "rate": {"type": "number", "enum": [0, 0.25, 3, 5, 12, 18, 28]},
                            "taxable_value": {"type": "number"},
                            "igst": {"type": "number"},
                            "cgst": {"type": "number"},
                            "sgst": {"type": "number"}
                        }
                    }
                }
            }
        }


# =============================================================================
# SEMANTIC EDGES
# =============================================================================

class SemanticEdge(BaseModel):
    """
    A semantic relationship between nodes.
    These are stored as graph edges in Neo4j.
    """
    edge_type: EdgeType
    source_node: str = Field(..., description="Source node ID")
    target_node: str = Field(..., description="Target node ID")
    properties: dict = Field(default_factory=dict, description="Edge properties")
    valid_from: datetime
    valid_to: Optional[datetime] = None
    source_mutation: Optional[str] = Field(None, description="Mutation that created this edge")


class RequiresFormEdge(SemanticEdge):
    """Specialized edge for Rule → Form relationships."""
    edge_type: Literal[EdgeType.REQUIRES_FORM] = EdgeType.REQUIRES_FORM
    purpose: FormPurpose
    section_ref: Optional[str] = Field(None, description="Specific section, e.g., 'Part A'")
    mandatory: bool = Field(default=True)


class MutatisMutandisEdge(SemanticEdge):
    """
    Edge indicating that provisions of one rule apply to another 
    with 'necessary modifications'.
    
    This is the "Killer Feature" for RAG - automatic context expansion.
    """
    edge_type: Literal[EdgeType.MUTATIS_MUTANDIS] = EdgeType.MUTATIS_MUTANDIS
    source_provisions: list[str] = Field(
        ..., 
        description="Specific sub-rules that apply, e.g., ['(5)', '(6)']"
    )


# =============================================================================
# MUTATION OPERATIONS (The "Compiler Output")
# =============================================================================

class MutationTarget(BaseModel):
    """Specifies the target of a mutation operation."""
    node_path: str = Field(..., description="Full path, e.g., 'CGST_Rules/Rule_10/SubRule_1'")
    anchor: Optional[str] = Field(None, description="Text anchor for SPLICE operations")
    anchor_position: Optional[Literal["before", "after"]] = Field(None)


class MutationPayload(BaseModel):
    """The content to insert/replace."""
    label: Optional[str] = None
    heading: Optional[str] = None
    content: Optional[str] = None
    node_type: Optional[NodeType] = None
    schema_patch: Optional[dict] = Field(None, description="For form schema mutations")


class MutationOperation(BaseModel):
    """
    A single mutation operation - the "compiled" output from a notification.
    
    This is the core unit of change in the Legal AST. Each notification
    produces one or more MutationOperations.
    """
    mutation_id: str = Field(..., description="Unique ID, e.g., 'mut_18_2025_001'")
    source_notification: str = Field(..., description="Source document reference")
    notification_date: datetime = Field(..., description="When notification was published")
    effective_date: datetime = Field(..., description="When changes take effect")
    
    target: MutationTarget
    operation: OperationType
    payload: MutationPayload
    
    # For operations with position context
    position: Optional[Literal["before", "after", "first", "last"]] = None
    anchor_child_label: Optional[str] = Field(
        None, 
        description="For INSERT_CHILD, the sibling to position relative to"
    )
    
    # Metadata
    original_text: Optional[str] = Field(
        None, 
        description="Original amendment text from notification"
    )
    
    class Config:
        json_schema_extra = {
            "example": {
                "mutation_id": "mut_18_2025_001",
                "source_notification": "Notification 18/2025-CT",
                "notification_date": "2025-10-31T00:00:00Z",
                "effective_date": "2025-11-01T00:00:00Z",
                "target": {
                    "node_path": "CGST_Rules/Rule_10/SubRule_1",
                    "anchor": "under rule 9,",
                    "anchor_position": "after"
                },
                "operation": "SPLICE",
                "payload": {
                    "content": "rule 9A and rule 14A,"
                }
            }
        }


class MutationBatch(BaseModel):
    """
    A batch of mutations from a single notification.
    
    A notification typically contains multiple amendments.
    This groups them for atomic processing.
    """
    batch_id: str
    source_notification: str
    notification_date: datetime
    effective_date: datetime
    mutations: list[MutationOperation]
    
    # Validation flags (populated by the compiler)
    all_anchors_resolved: bool = Field(default=False)
    all_targets_exist: bool = Field(default=False)
    no_temporal_conflicts: bool = Field(default=False)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def schema_to_natural_language(schema_payload: dict) -> str:
    """
    Convert JSON Schema to natural language for LLM consumption.
    
    This is critical for RAG - LLMs understand NL better than regex patterns.
    
    Example:
        Input: {"pan": {"type": "string", "pattern": "[A-Z]{5}[0-9]{4}[A-Z]{1}"}}
        Output: "Field 'PAN' accepts a text string in standard PAN format (5 letters, 4 digits, 1 letter)."
    """
    if not schema_payload.get("properties"):
        return "No fields defined."
    
    descriptions = []
    properties = schema_payload.get("properties", {})
    required = set(schema_payload.get("required", []))
    
    for field_name, field_def in properties.items():
        field_type = field_def.get("type", "text")
        title = field_def.get("title", field_name)
        
        # Build description
        parts = [f"**{title}**"]
        
        if field_name in required:
            parts.append("(mandatory)")
        else:
            parts.append("(optional)")
            
        # Type-specific descriptions
        if field_type == "string":
            if "pattern" in field_def:
                pattern = field_def["pattern"]
                if "PAN" in title.upper() or "[A-Z]{5}" in pattern:
                    parts.append("- accepts standard PAN format")
                elif "email" in field_def.get("format", ""):
                    parts.append("- accepts email address")
                else:
                    parts.append(f"- text with pattern validation")
            elif "enum" in field_def:
                options = ", ".join(field_def["enum"][:5])
                if len(field_def["enum"]) > 5:
                    options += "..."
                parts.append(f"- select from: {options}")
            else:
                parts.append("- text input")
        elif field_type == "boolean":
            parts.append("- Yes/No checkbox")
        elif field_type == "number" or field_type == "integer":
            parts.append("- numeric value")
        elif field_type == "array":
            parts.append("- multiple values allowed")
            
        descriptions.append(" ".join(parts))
    
    return "\n".join(descriptions)
