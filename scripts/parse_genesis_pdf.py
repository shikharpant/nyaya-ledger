"""
PDF Genesis Parser - Parse Notification 3/2017-CT into genesis JSON files.

This script parses the founding CGST Rules notification and extracts:
1. All Rules with their hierarchy (Chapter → Rule → SubRule → Clause → Proviso)
2. All Forms with their structure (Form → Section → Fields/Tables)

Usage:
    python scripts/parse_genesis_pdf.py <input_pdf_or_txt> --output-dir data/genesis/
    
Requirements:
    pip install pdfplumber openai python-dotenv

The script uses DeepSeek API (OpenAI-compatible) for intelligent parsing of complex legal structures.
Set DEEPSEEK_API_KEY in your environment or .env file.
"""

import argparse
import json
import re
import os
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field, asdict
from dotenv import load_dotenv

load_dotenv()

# DeepSeek API Configuration
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"  # or "deepseek-reasoner" for R1

# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class ParsedProviso:
    id: str
    label: str
    ordinal: int
    content: str

@dataclass
class ParsedClause:
    id: str
    label: str
    content: str

@dataclass 
class ParsedSubRule:
    id: str
    label: str
    content: str
    provisos: list[ParsedProviso] = field(default_factory=list)
    clauses: list[ParsedClause] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)

@dataclass
class ParsedRule:
    id: str
    label: str
    heading: str
    content: str
    version: str
    valid_from: str
    subrules: list[ParsedSubRule] = field(default_factory=list)

@dataclass
class ParsedChapter:
    id: str
    number: str
    title: str
    rules: list[ParsedRule] = field(default_factory=list)

@dataclass
class ParsedFormField:
    name: str
    title: str
    field_type: str  # string, number, boolean, array, object
    required: bool = False
    pattern: Optional[str] = None
    enum: Optional[list] = None
    format: Optional[str] = None  # date, email, etc.

@dataclass
class ParsedFormSection:
    section_id: str
    section_label: str
    heading: str
    section_type: str = "fields"  # fields, table, instructions, declaration
    table_type: Optional[str] = None  # static, dynamic
    table_columns: Optional[list[str]] = None
    has_serial_column: bool = False
    has_total_row: bool = False
    description: str = ""
    schema_payload: dict = field(default_factory=dict)

@dataclass
class ParsedForm:
    form_id: str
    form_number: str
    title: str
    version: str
    valid_from: str
    defined_by_rule: Optional[str] = None
    sections: list[ParsedFormSection] = field(default_factory=list)


# =============================================================================
# TEXT EXTRACTION
# =============================================================================

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from PDF using pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("Please install pdfplumber: pip install pdfplumber")
    
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
    
    return "\n\n".join(text_parts)


def read_text_file(txt_path: str) -> str:
    """Read plain text file."""
    with open(txt_path, 'r', encoding='utf-8') as f:
        return f.read()


# =============================================================================
# GPT-4o PARSING PROMPTS
# =============================================================================

RULES_PARSER_PROMPT = """You are a legal document parser. Parse the following CGST Rules notification into a structured JSON format.

## Output Schema
{
    "statute_id": "CGST_Rules_2017",
    "title": "Central Goods and Services Tax Rules, 2017",
    "effective_date": "2017-07-01",
    "chapters": [
        {
            "number": "I",
            "title": "Preliminary",
            "rules": [
                {
                    "label": "1",
                    "heading": "Short title and commencement",
                    "content": "Main rule text without sub-rules",
                    "subrules": [
                        {
                            "label": "(1)",
                            "content": "Sub-rule text",
                            "provisos": [
                                {
                                    "label": "First Proviso",
                                    "ordinal": 1,
                                    "content": "Provided that..."
                                }
                            ],
                            "clauses": [
                                {
                                    "label": "(a)",
                                    "content": "Clause text"
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    ]
}

## Parsing Rules
1. Each rule has a number (e.g., "8", "9A", "14A") and a heading
2. Sub-rules are numbered like (1), (2), (3) or (1A), (2B)
3. Clauses within sub-rules are lettered like (a), (b), (c)
4. Provisos start with "Provided that" or "Provided further that"
5. Explanations start with "Explanation"
6. Preserve exact text, including punctuation

## Important
- Parse ALL rules you find in the text
- Maintain the exact hierarchy
- Mark references to forms (e.g., "FORM GST REG-01")
- Include chapter boundaries

Now parse this notification:
"""

FORMS_PARSER_PROMPT = """You are a legal document parser. Parse the following GST Form into a structured JSON format with FULL DETAIL including every field.

## Output Schema
{
    "form_id": "FORM_GST_REG_01",
    "form_number": "GST REG-01",
    "title": "Application for Registration",
    "sections": [
        {
            "section_label": "Part A",
            "heading": "Preliminary Information",
            "section_type": "fields",
            "fields": [
                {
                    "name": "pan",
                    "title": "Permanent Account Number (PAN)",
                    "type": "string",
                    "required": true,
                    "pattern": "[A-Z]{5}[0-9]{4}[A-Z]{1}",
                    "description": "10-character PAN"
                }
            ]
        },
        {
            "section_label": "Table 4A",
            "heading": "Details of outward supplies",
            "section_type": "table",
            "table_type": "dynamic",
            "has_serial_column": true,
            "has_total_row": true,
            "table_columns": ["S.No.", "GSTIN", "Invoice No.", "Value", "Tax"],
            "row_schema": {
                "serial": {"type": "integer", "auto": true},
                "gstin": {"type": "string", "pattern": "GSTIN_PATTERN"},
                "invoice_no": {"type": "string"},
                "value": {"type": "number"},
                "tax": {"type": "number"}
            }
        }
    ]
}

## Field Type Mappings
- Text input → "string"
- Number/Amount → "number"
- Date → "string" with format: "date"
- Dropdown/Select → "string" with "enum" list
- Checkbox → "boolean"
- Multi-select → "array"
- Address block → "object" with nested properties
- Repeating rows → "array" of "object"

## Important
- Extract EVERY field from the form
- Identify validation patterns (PAN, GSTIN, pincode, mobile, email)
- Mark mandatory fields
- For tables, identify column headers
- Include serial number (S.No.) columns
- Note if table has total/subtotal rows

Now parse this form:
"""


ACT_PARSER_PROMPT = """You are a legal document parser. Parse the following GST Act into a structured JSON format.

## Output Schema
{
    "statute_id": "CGST_Act_2017",
    "title": "Central Goods and Services Tax Act, 2017",
    "effective_date": "2017-07-01",
    "chapters": [
        {
            "number": "II",
            "title": "Administration",
            "sections": [
                {
                    "label": "3",
                    "heading": "Officers under this Act",
                    "content": "Main section text",
                    "subsections": [
                        {
                            "label": "(1)",
                            "content": "The Government shall..."
                        }
                    ]
                }
            ]
        }
    ]
}

## Parsing Rules
1. Sections are numbered (e.g., "1", "25", "174")
2. Sub-sections are numbered (1), (2), (3)
3. Clauses are lettered (a), (b), (c)
4. Preserve exact text and hierarchy
5. Include chapter boundaries

Now parse this Act text:
"""


# =============================================================================
# PARSING FUNCTIONS
# =============================================================================

def parse_act_with_llm(text: str, api_key: str, chunk_size: int = 30000) -> dict:
    """Parse Act text using DeepSeek API."""
    from openai import OpenAI
    
    client = OpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL
    )
    
    if len(text) > chunk_size:
        print(f"⚠ Text is {len(text)} chars, will process in chunks")
        text = text[:chunk_size]
    
    response = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": ACT_PARSER_PROMPT},
            {"role": "user", "content": text}
        ],
        response_format={"type": "json_object"},
        temperature=0
    )
    
    return json.loads(response.choices[0].message.content)

def parse_rules_with_llm(text: str, api_key: str, chunk_size: int = 30000) -> dict:
    """Parse rules text using DeepSeek API."""
    from openai import OpenAI
    
    client = OpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL
    )
    
    # For very long documents, we may need to chunk
    if len(text) > chunk_size:
        print(f"⚠ Text is {len(text)} chars, will process in chunks")
        # For now, just use first chunk and warn
        text = text[:chunk_size]
        print(f"  Using first {chunk_size} characters")
    
    response = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": RULES_PARSER_PROMPT},
            {"role": "user", "content": text}
        ],
        response_format={"type": "json_object"},
        temperature=0
    )
    
    return json.loads(response.choices[0].message.content)


def parse_form_with_llm(form_text: str, api_key: str) -> dict:
    """Parse a single form using DeepSeek API."""
    from openai import OpenAI
    
    client = OpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL
    )
    
    response = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": FORMS_PARSER_PROMPT},
            {"role": "user", "content": form_text}
        ],
        response_format={"type": "json_object"},
        temperature=0
    )
    
    return json.loads(response.choices[0].message.content)


def extract_forms_from_text(full_text: str) -> list[tuple[str, str]]:
    """
    Extract individual forms from the notification text.
    Returns list of (form_name, form_text) tuples.
    """
    # Pattern to find form headers
    form_pattern = r'FORM\s+GST\s+([A-Z]{2,4}[-\s]?\d{1,2}[A-Z]?)'
    
    matches = list(re.finditer(form_pattern, full_text, re.IGNORECASE))
    
    forms = []
    for i, match in enumerate(matches):
        form_name = f"FORM GST {match.group(1).replace(' ', '-')}"
        start = match.start()
        
        # End at next form or end of text
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(full_text)
        
        form_text = full_text[start:end]
        forms.append((form_name, form_text))
    
    return forms


# =============================================================================
# OUTPUT GENERATION
# =============================================================================

def generate_rule_id(statute_id: str, rule_label: str) -> str:
    """Generate rule ID like CGST_Rules/Rule_8"""
    return f"{statute_id.replace('_2017', '')}/Rule_{rule_label}"


def generate_subrule_id(rule_id: str, subrule_label: str) -> str:
    """Generate subrule ID like CGST_Rules/Rule_8/SubRule_1"""
    # Extract number from label like "(1)" -> "1"
    num = re.sub(r'[^\dA-Za-z]', '', subrule_label)
    return f"{rule_id}/SubRule_{num}"


def convert_to_genesis_json(parsed_data: dict, effective_date: str = "2017-07-01") -> dict:
    """Convert parsed rules data to genesis JSON format."""
    statute_id = parsed_data.get("statute_id", "CGST_Rules_2017")
    
    genesis = {
        "_metadata": {
            "description": f"Genesis block for {parsed_data.get('title', 'CGST Rules 2017')}",
            "source": "Notification 3/2017-Central Tax",
            "effective_date": effective_date,
            "version": "v1",
            "parsed_at": datetime.now().isoformat()
        },
        "statute_id": statute_id,
        "title": parsed_data.get("title", "Central Goods and Services Tax Rules, 2017"),
        "chapters": []
    }
    
    for chapter in parsed_data.get("chapters", []):
        chapter_data = {
            "id": f"{statute_id}/Chapter_{chapter['number']}",
            "number": chapter["number"],
            "title": chapter["title"],
            "rules": []
        }
        
        for rule in chapter.get("rules", []):
            rule_id = generate_rule_id(statute_id, rule["label"])
            
            rule_data = {
                "id": rule_id,
                "node_type": "rule",
                "label": rule["label"],
                "heading": rule["heading"],
                "content": rule.get("content", ""),
                "version": "v1",
                "valid_from": f"{effective_date}T00:00:00Z",
                "subrules": []
            }
            
            for subrule in rule.get("subrules", []):
                subrule_id = generate_subrule_id(rule_id, subrule["label"])
                
                subrule_data = {
                    "id": subrule_id,
                    "label": subrule["label"],
                    "content": subrule["content"],
                    "provisos": [],
                    "clauses": [],
                    "edges": []
                }
                
                # Add provisos
                for i, proviso in enumerate(subrule.get("provisos", [])):
                    subrule_data["provisos"].append({
                        "id": f"{subrule_id}/Proviso_{i+1}",
                        "label": proviso.get("label", f"Proviso {i+1}"),
                        "ordinal": proviso.get("ordinal", i+1),
                        "content": proviso["content"]
                    })
                
                # Add clauses
                for clause in subrule.get("clauses", []):
                    clause_label = clause["label"].strip("()")
                    subrule_data["clauses"].append({
                        "id": f"{subrule_id}/Clause_{clause_label}",
                        "label": clause["label"],
                        "content": clause["content"]
                    })
                
                # Detect form references and add edges
                form_refs = re.findall(r'FORM\s+GST\s+([A-Z]{2,4}[-\s]?\d{1,2}[A-Z]?)', subrule["content"], re.IGNORECASE)
                for form_ref in form_refs:
                    form_id = f"FORM_GST_{form_ref.replace('-', '_').replace(' ', '_').upper()}"
                    subrule_data["edges"].append({
                        "type": "REQUIRES_FORM",
                        "target": form_id,
                        "properties": {
                            "purpose": "REGISTRATION",  # Will need refinement
                            "mandatory": True
                        }
                    })
                
                rule_data["subrules"].append(subrule_data)
            
            chapter_data["rules"].append(rule_data)
        
        genesis["chapters"].append(chapter_data)
    
    return genesis


def convert_form_to_genesis_json(parsed_form: dict, effective_date: str = "2017-07-01") -> dict:
    """Convert parsed form data to genesis JSON format."""
    form_number = parsed_form.get("form_number", "GST REG-01")
    form_id = f"FORM_{form_number.replace(' ', '_').replace('-', '_').upper()}"
    
    genesis = {
        "_metadata": {
            "description": f"Genesis block for {form_number}",
            "source": "Notification 3/2017-Central Tax",
            "effective_date": effective_date,
            "version": "v1",
            "parsed_at": datetime.now().isoformat()
        },
        "form_id": form_id,
        "form_number": form_number,
        "title": parsed_form.get("title", ""),
        "version": "v1",
        "valid_from": f"{effective_date}T00:00:00Z",
        "sections": []
    }
    
    for section in parsed_form.get("sections", []):
        section_id = f"{form_id}/{section['section_label'].replace(' ', '_')}"
        
        section_data = {
            "section_id": section_id,
            "section_label": section["section_label"],
            "heading": section.get("heading", ""),
            "section_type": section.get("section_type", "fields"),
            "description": section.get("description", "")
        }
        
        # Handle table-specific fields
        if section.get("section_type") == "table":
            section_data["table_type"] = section.get("table_type", "dynamic")
            section_data["table_columns"] = section.get("table_columns", [])
            section_data["has_serial_column"] = section.get("has_serial_column", False)
            section_data["has_total_row"] = section.get("has_total_row", False)
            
            # Convert row_schema to array schema
            row_schema = section.get("row_schema", {})
            section_data["schema_payload"] = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": row_schema
                }
            }
        else:
            # Convert fields to JSON Schema
            fields = section.get("fields", [])
            properties = {}
            required = []
            
            for field in fields:
                field_schema = {
                    "type": field.get("type", "string"),
                    "title": field.get("title", field["name"])
                }
                
                if field.get("pattern"):
                    field_schema["pattern"] = field["pattern"]
                if field.get("enum"):
                    field_schema["enum"] = field["enum"]
                if field.get("format"):
                    field_schema["format"] = field["format"]
                if field.get("description"):
                    field_schema["description"] = field["description"]
                
                properties[field["name"]] = field_schema
                
                if field.get("required"):
                    required.append(field["name"])
            
            section_data["schema_payload"] = {
                "type": "object",
                "properties": properties,
                "required": required
            }
        
        genesis["sections"].append(section_data)
    
    return genesis


def convert_act_to_genesis_json(parsed_data: dict, effective_date: str = "2017-07-01") -> dict:
    """Convert parsed Act data to genesis JSON format."""
    statute_id = parsed_data.get("statute_id", "CGST_Act_2017")
    
    genesis = {
        "_metadata": {
            "description": f"Genesis block for {parsed_data.get('title')}",
            "source": "Official Gazette",
            "effective_date": effective_date,
            "version": "v1",
            "parsed_at": datetime.now().isoformat()
        },
        "statute_id": statute_id,
        "title": parsed_data.get("title", ""),
        "chapters": []
    }
    
    for chapter in parsed_data.get("chapters", []):
        chapter_data = {
            "id": f"{statute_id}/Chapter_{chapter['number']}",
            "number": chapter["number"],
            "title": chapter["title"],
            "sections": []
        }
        
        for section in chapter.get("sections", []):
            section_id = f"{statute_id}/Section_{section['label']}"
            
            section_data = {
                "id": section_id,
                "node_type": "section",
                "label": section["label"],
                "heading": section["heading"],
                "content": section.get("content", ""),
                "version": "v1",
                "valid_from": f"{effective_date}T00:00:00Z",
                "subsections": []
            }
            
            for subsection in section.get("subsections", []):
                subsection_id = f"{section_id}/SubSection_{subsection['label'].strip('()')}"
                
                subsection_data = {
                    "id": subsection_id,
                    "label": subsection["label"],
                    "content": subsection["content"]
                }
                section_data["subsections"].append(subsection_data)
            
            chapter_data["sections"].append(section_data)
        
        genesis["chapters"].append(chapter_data)
    
    return genesis


# =============================================================================
# MAIN CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Parse CGST Rules notification into genesis JSON files"
    )
    parser.add_argument("input", help="Input PDF or TXT file")
    parser.add_argument("--output-dir", "-o", default="data/genesis/",
                       help="Output directory for genesis files")
    parser.add_argument("--parse-act", action="store_true",
                       help="Parse Act/Statute")
    parser.add_argument("--parse-rules", action="store_true", 
                       help="Parse rules")
    parser.add_argument("--parse-forms", action="store_true",
                       help="Parse forms")
    parser.add_argument("--effective-date", default="2017-07-01",
                       help="Effective date for genesis")
    parser.add_argument("--offline", action="store_true",
                       help="Use offline regex parsing (less accurate)")
    
    args = parser.parse_args()
    
    # Check API key (prefer DeepSeek, fallback to OpenAI)
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key and not args.offline:
        print("⚠ DEEPSEEK_API_KEY not set. Use --offline for regex-based parsing.")
        print("  Set it in .env: DEEPSEEK_API_KEY=your_key")
        return
    
    # Extract text
    input_path = Path(args.input)
    if input_path.suffix.lower() == ".pdf":
        print(f"📄 Extracting text from PDF: {input_path}")
        text = extract_text_from_pdf(str(input_path))
    else:
        print(f"📄 Reading text file: {input_path}")
        text = read_text_file(str(input_path))
    
    print(f"   Extracted {len(text)} characters")
    
    # Create output directories
    output_dir = Path(args.output_dir)
    acts_dir = output_dir / "acts"
    rules_dir = output_dir / "rules"
    forms_dir = output_dir / "forms"
    
    acts_dir.mkdir(parents=True, exist_ok=True)
    rules_dir.mkdir(parents=True, exist_ok=True)
    forms_dir.mkdir(parents=True, exist_ok=True)
    
    # Parse based on flags (default: rules + forms if nothing specific)
    parse_act = args.parse_act
    parse_rules = args.parse_rules or (not args.parse_act and not args.parse_rules and not args.parse_forms)
    parse_forms = args.parse_forms or (not args.parse_act and not args.parse_rules and not args.parse_forms)
    
    if parse_act:
        print("\n🏛 Parsing Act...")
        parsed_act = parse_act_with_llm(text, api_key)
        genesis = convert_act_to_genesis_json(parsed_act, args.effective_date)
        
        output_file = acts_dir / f"{genesis['statute_id'].lower()}.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(genesis, f, indent=4, ensure_ascii=False)
        print(f"   ✓ Saved {output_file.name}")
    
    if parse_rules:
        print("\n📜 Parsing rules...")
        if args.offline:
            print("   Using offline parser (regex-based)")
            # TODO: Implement offline parser
            parsed_rules = {"chapters": []}
        else:
            print("   Using DeepSeek API parser")
            parsed_rules = parse_rules_with_llm(text, api_key)
        
        # Convert and save
        genesis = convert_to_genesis_json(parsed_rules, args.effective_date)
        
        # Save per-chapter files
        for chapter in genesis.get("chapters", []):
            chapter_num = chapter["number"].lower()
            output_file = rules_dir / f"cgst_rules_chapter{chapter_num}.json"
            
            chapter_genesis = {
                "_metadata": genesis["_metadata"],
                "statute_id": genesis["statute_id"],
                "title": genesis["title"],
                "chapter": {
                    "id": chapter["id"],
                    "number": chapter["number"],
                    "title": chapter["title"]
                },
                "rules": chapter["rules"]
            }
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(chapter_genesis, f, indent=4, ensure_ascii=False)
            
            print(f"   ✓ Saved {output_file.name} ({len(chapter['rules'])} rules)")
    
    if parse_forms:
        print("\n📋 Parsing forms...")
        forms = extract_forms_from_text(text)
        print(f"   Found {len(forms)} forms")
        
        for form_name, form_text in forms:
            print(f"   Processing: {form_name}")
            
            if args.offline:
                # TODO: Implement offline form parser
                parsed_form = {"form_number": form_name, "sections": []}
            else:
                try:
                    parsed_form = parse_form_with_llm(form_text[:15000], api_key)  # Limit size
                except Exception as e:
                    print(f"      ⚠ Error parsing {form_name}: {e}")
                    continue
            
            # Convert and save
            form_genesis = convert_form_to_genesis_json(parsed_form, args.effective_date)
            
            output_file = forms_dir / f"{form_genesis['form_id'].lower()}.json"
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(form_genesis, f, indent=4, ensure_ascii=False)
            
            print(f"      ✓ Saved {output_file.name}")
    
    print("\n✓ Genesis parsing complete!")
    print(f"  Output directory: {output_dir}")


if __name__ == "__main__":
    main()
