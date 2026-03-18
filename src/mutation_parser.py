"""
Mutation Parser - Extracts structured mutations from legal notifications using GPT-4o.

This module implements the "Legal Compiler" that converts notification text
into JSON-Patch style mutation operations.

Key design decisions per user feedback:
1. Use GPT-4o for parsing (highest reasoning capability)
2. Include one-shot examples in the prompt
3. Strict JSON schema enforcement
"""

import json
import re
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, asdict

# DeepSeek API Configuration
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# GPT-4o / DeepSeek System Prompt with One-Shot Examples
MUTATION_PARSER_PROMPT = """You are a Legal Mutation Compiler. Your task is to convert legal amendment notifications into structured JSON-Patch style operations.

## Core Principles
1. Parse EXACTLY what the notification says - do not infer or add information
2. Resolve relative references ("after rule 9") to explicit paths
3. Preserve the exact text to be inserted/substituted
4. Extract the effective date from Rule 1 of the notification

## Operation Types
- INSERT_CHILD: Add a new child node (rule, sub-rule, proviso)
- INSERT_SIBLING: Add a sibling after/before another node
- SPLICE: Insert text at a specific anchor position within existing text
- SUBSTITUTE: Replace one text pattern with another
- DELETE: Remove a node entirely
- OMIT: Legal omission (marks as omitted, different from delete)

## Output Schema
{
  "notification_id": "string",
  "notification_date": "ISO8601 date when published",
  "effective_date": "ISO8601 date when changes take effect",
  "target_statute": "string (e.g., 'CGST_Rules_2017')",
  "mutations": [
    {
      "mutation_id": "unique ID like 'mut_001'",
      "target": {
        "node_path": "Full path like 'CGST_Rules/Rule_10/SubRule_1'",
        "anchor": "Text anchor for SPLICE operations (optional)",
        "anchor_position": "before|after (optional)"
      },
      "operation": "INSERT_CHILD|INSERT_SIBLING|SPLICE|SUBSTITUTE|DELETE|OMIT",
      "position": "before|after|first|last (for INSERT operations)",
      "anchor_child_label": "Label of sibling to position relative to (e.g., '(1)')",
      "payload": {
        "node_type": "rule|subrule|proviso|clause (for INSERT)",
        "label": "Node label (e.g., '9A', '(1A)')",
        "heading": "Heading text (for rules)",
        "content": "Full text content"
      },
      "original_text": "The exact text from the notification describing this change"
    }
  ]
}

## Important Rules
1. For "after rule X, the following rule shall be inserted" → Use INSERT_SIBLING with position="after"
2. For "in sub-rule (1), after the words 'X', insert..." → Use SPLICE with anchor="X" and anchor_position="after"
3. For "shall be substituted" → Use SUBSTITUTE operation
4. For "shall be omitted" → Use OMIT operation
5. Always include the original notification text in "original_text" field

## Example 1: Inserting a new rule

INPUT:
"2. After rule 9 of the said rules, the following rule shall be inserted, namely:-
'9A. Grant of registration electronically.- The registration shall be granted electronically...'"

OUTPUT:
{
  "mutations": [
    {
      "mutation_id": "mut_001",
      "target": {
        "node_path": "CGST_Rules/Rule_9"
      },
      "operation": "INSERT_SIBLING",
      "position": "after",
      "payload": {
        "node_type": "rule",
        "label": "9A",
        "heading": "Grant of registration electronically",
        "content": "The registration shall be granted electronically..."
      },
      "original_text": "After rule 9 of the said rules, the following rule shall be inserted..."
    }
  ]
}

## Example 2: Splicing text into existing provision

INPUT:
"3. In rule 10, in sub-rule (1), after the words 'under rule 9,' the words 'rule 9A and rule 14A,' shall be inserted."

OUTPUT:
{
  "mutations": [
    {
      "mutation_id": "mut_002",
      "target": {
        "node_path": "CGST_Rules/Rule_10/SubRule_1",
        "anchor": "under rule 9,",
        "anchor_position": "after"
      },
      "operation": "SPLICE",
      "payload": {
        "content": "rule 9A and rule 14A,"
      },
      "original_text": "In rule 10, in sub-rule (1), after the words 'under rule 9,' the words 'rule 9A and rule 14A,' shall be inserted."
    }
  ]
}

## Example 3: Inserting a new sub-rule

INPUT:
"In rule 12, after sub-rule (1), the following sub-rule shall be inserted, namely:-
'(1A) A person applying for registration under this rule...'"

OUTPUT:
{
  "mutations": [
    {
      "mutation_id": "mut_003",
      "target": {
        "node_path": "CGST_Rules/Rule_12"
      },
      "operation": "INSERT_CHILD",
      "position": "after",
      "anchor_child_label": "(1)",
      "payload": {
        "node_type": "subrule",
        "label": "(1A)",
        "content": "A person applying for registration under this rule..."
      },
      "original_text": "In rule 12, after sub-rule (1), the following sub-rule shall be inserted..."
    }
  ]
}

Now parse the following notification text into mutation operations. Be precise and complete.
"""


@dataclass
class ParsedMutation:
    """A parsed mutation ready for application."""
    mutation_id: str
    target_node_path: str
    operation: str
    payload: dict
    anchor: Optional[str] = None
    anchor_position: Optional[str] = None
    position: Optional[str] = None
    anchor_child_label: Optional[str] = None
    original_text: Optional[str] = None


@dataclass
class ParsedNotification:
    """Complete parsed notification with all mutations."""
    notification_id: str
    notification_date: datetime
    effective_date: datetime
    target_statute: str
    mutations: list[ParsedMutation]
    raw_response: dict


def parse_notification_with_llm(
    notification_text: str,
    api_key: str,
    notification_id: str = "unknown",
    model: str = DEEPSEEK_MODEL
) -> ParsedNotification:
    """
    Parse a legal notification into structured mutations using LLM (DeepSeek/GPT).
    
    Args:
        notification_text: The full text of the notification
        api_key: OpenAI API key
        notification_id: ID to assign to this notification
        model: OpenAI model to use
    
    Returns:
        ParsedNotification with all extracted mutations
    """
    from openai import OpenAI
    
    client = OpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL
    )
    
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": MUTATION_PARSER_PROMPT},
            {"role": "user", "content": notification_text}
        ],
        response_format={"type": "json_object"},
        temperature=0  # Deterministic output
    )
    
    # Parse the response
    result = json.loads(response.choices[0].message.content)
    
    # Convert to dataclasses
    mutations = []
    for mut in result.get("mutations", []):
        target = mut.get("target", {})
        mutations.append(ParsedMutation(
            mutation_id=mut.get("mutation_id", f"mut_{len(mutations)+1:03d}"),
            target_node_path=target.get("node_path", ""),
            operation=mut.get("operation", ""),
            payload=mut.get("payload", {}),
            anchor=target.get("anchor"),
            anchor_position=target.get("anchor_position"),
            position=mut.get("position"),
            anchor_child_label=mut.get("anchor_child_label"),
            original_text=mut.get("original_text")
        ))
    
    return ParsedNotification(
        notification_id=result.get("notification_id", notification_id),
        notification_date=datetime.fromisoformat(
            result.get("notification_date", datetime.now().isoformat())
        ),
        effective_date=datetime.fromisoformat(
            result.get("effective_date", datetime.now().isoformat())
        ),
        target_statute=result.get("target_statute", "CGST_Rules_2017"),
        mutations=mutations,
        raw_response=result
    )


def parse_notification_offline(notification_text: str) -> ParsedNotification:
    """
    Parse a notification using rule-based extraction (no LLM).
    
    This is a fallback for when LLM is not available, or for simple cases.
    Uses regex patterns to identify common amendment structures.
    """
    mutations = []
    
    # Pattern: "After rule X, the following rule shall be inserted"
    insert_rule_pattern = r"After rule (\d+[A-Z]?)[,\s]+.*?the following rule shall be inserted.*?['\"](\d+[A-Z]?)\.\s*([^'\"]+)"
    
    for match in re.finditer(insert_rule_pattern, notification_text, re.IGNORECASE | re.DOTALL):
        after_rule, new_label, content = match.groups()
        
        # Try to extract heading
        heading_match = re.match(r"([^-\.]+)[.-]", content.strip())
        heading = heading_match.group(1).strip() if heading_match else ""
        
        mutations.append(ParsedMutation(
            mutation_id=f"mut_{len(mutations)+1:03d}",
            target_node_path=f"CGST_Rules/Rule_{after_rule}",
            operation="INSERT_SIBLING",
            position="after",
            payload={
                "node_type": "rule",
                "label": new_label,
                "heading": heading,
                "content": content.strip()
            },
            original_text=match.group(0)[:200]
        ))
    
    # Pattern: "in sub-rule (X), after the words 'Y', ... shall be inserted"
    splice_pattern = r"in rule (\d+[A-Z]?),?\s*in sub-rule \((\d+)\),?\s*after the words ['\"]([^'\"]+)['\"],?\s*(?:the words\s*)?['\"]([^'\"]+)['\"].*?shall be inserted"
    
    for match in re.finditer(splice_pattern, notification_text, re.IGNORECASE):
        rule, subrule, anchor, insert_text = match.groups()
        
        mutations.append(ParsedMutation(
            mutation_id=f"mut_{len(mutations)+1:03d}",
            target_node_path=f"CGST_Rules/Rule_{rule}/SubRule_{subrule}",
            operation="SPLICE",
            anchor=anchor,
            anchor_position="after",
            payload={"content": insert_text},
            original_text=match.group(0)
        ))
    
    return ParsedNotification(
        notification_id="offline_parse",
        notification_date=datetime.now(),
        effective_date=datetime.now(),
        target_statute="CGST_Rules_2017",
        mutations=mutations,
        raw_response={}
    )


def save_mutations_to_file(parsed: ParsedNotification, output_path: str) -> None:
    """Save parsed mutations to a JSON file for review/audit."""
    output = {
        "notification_id": parsed.notification_id,
        "notification_date": parsed.notification_date.isoformat(),
        "effective_date": parsed.effective_date.isoformat(),
        "target_statute": parsed.target_statute,
        "mutations": [asdict(m) for m in parsed.mutations],
        "raw_response": parsed.raw_response
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"✓ Saved {len(parsed.mutations)} mutations to {output_path}")


# Example usage
if __name__ == "__main__":
    # Example notification text (from the Notification 18/2025 discussed)
    sample_notification = """
    NOTIFICATION No. 18/2025 – Central Tax
    
    New Delhi, the 31st October, 2025
    
    G.S.R. ....(E).— In exercise of the powers conferred by section 164 of the Central Goods 
    and Services Tax Act, 2017 (12 of 2017), the Central Government hereby makes the following 
    rules further to amend the Central Goods and Services Tax Rules, 2017, namely:—
    
    1. (1) These rules may be called the Central Goods and Services Tax (Amendment) Rules, 2025.
       (2) They shall come into force on the 1st day of November, 2025.
    
    2. After rule 9 of the said rules, the following rule shall be inserted, namely:-
       "9A. Grant of registration electronically.- The registration shall be granted 
       electronically in FORM GST REG-06 after verification of the application and 
       documents furnished."
    
    3. In rule 10, in sub-rule (1), after the words "under rule 9," the words 
       "rule 9A and rule 14A," shall be inserted.
    
    4. After rule 14 of the said rules, the following rule shall be inserted, namely:-
       "14A. Specific procedure for registration.- (1) A person applying for registration 
       under this rule shall undergo OTP-based authentication..."
    """
    
    # Test offline parsing
    print("Testing offline parser...")
    result = parse_notification_offline(sample_notification)
    
    print(f"\nFound {len(result.mutations)} mutations:")
    for mut in result.mutations:
        print(f"  - {mut.operation}: {mut.target_node_path}")
        if mut.payload.get("label"):
            print(f"    New node: {mut.payload['label']}")
        if mut.anchor:
            print(f"    Anchor: '{mut.anchor}' ({mut.anchor_position})")
