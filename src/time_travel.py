"""
Time-Travel Query Engine - Materialize legal state at any point in time.

This module implements the "time-travel" capability:
- get_rule(rule_id, as_of_date) → Returns rule state at that date
- get_form(form_id, as_of_date) → Returns form state at that date
- get_with_dependencies() → Follows semantic edges (MUTATIS_MUTANDIS, etc.)
"""

import json
from datetime import datetime
from typing import Optional, Any
from dataclasses import dataclass, field
from neo4j import GraphDatabase, Driver
from dotenv import load_dotenv
import os

load_dotenv()


@dataclass 
class RuleState:
    """Materialized state of a rule at a specific point in time."""
    id: str
    label: str
    heading: str
    content: str
    version: str
    valid_from: datetime
    valid_to: Optional[datetime]
    subrules: list[dict] = field(default_factory=list)
    provisos: list[dict] = field(default_factory=list)
    clauses: list[dict] = field(default_factory=list)
    linked_forms: list[dict] = field(default_factory=list)
    
    def to_text(self, include_heading: bool = True) -> str:
        """Render rule as readable text."""
        lines = []
        
        if include_heading:
            lines.append(f"Rule {self.label}. {self.heading}")
            lines.append("-" * 50)
        
        if self.content:
            lines.append(self.content)
        
        for sr in self.subrules:
            lines.append(f"\n{sr['label']} {sr['content']}")
            
            for proviso in sr.get('provisos', []):
                lines.append(f"\n  {proviso['content']}")
            
            for clause in sr.get('clauses', []):
                lines.append(f"\n  {clause['label']} {clause['content']}")
        
        return "\n".join(lines)


@dataclass
class FormState:
    """Materialized state of a form at a specific point in time."""
    id: str
    form_number: str
    title: str
    version: str
    valid_from: datetime
    sections: list[dict] = field(default_factory=list)
    
    def get_section_schema(self, section_label: str) -> Optional[dict]:
        """Get the JSON schema for a specific section."""
        for section in self.sections:
            if section['section_label'] == section_label:
                schema_str = section.get('schema_payload', '{}')
                return json.loads(schema_str) if isinstance(schema_str, str) else schema_str
        return None


class TimeTravelEngine:
    """Query engine for retrieving legal state at any point in time."""
    
    def __init__(self, uri: str = None, user: str = None, password: str = None):
        self.uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or os.getenv("NEO4J_USER", "neo4j")
        self.password = password or os.getenv("NEO4J_PASSWORD", "gitforlaw123")
        self.driver: Optional[Driver] = None
    
    def connect(self) -> None:
        """Establish connection to Neo4j."""
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
    
    def close(self) -> None:
        """Close the connection."""
        if self.driver:
            self.driver.close()
    
    def __enter__(self):
        self.connect()
        return self
    
    def __exit__(self, *args):
        self.close()
    
    def get_rule(self, rule_id: str, as_of: datetime) -> Optional[RuleState]:
        """
        Get the state of a rule at a specific date.
        
        Uses temporal query to find the version that was valid at `as_of`.
        
        Cypher Logic:
        - Find rule where valid_from <= as_of
        - AND (valid_to IS NULL OR valid_to > as_of)
        - Order by valid_from DESC to get most recent valid version
        """
        with self.driver.session() as session:
            # Query the rule with temporal filtering
            result = session.run("""
                MATCH (r:Rule)
                WHERE r.id = $rule_id
                  AND r.valid_from <= datetime($as_of)
                  AND (r.valid_to IS NULL OR r.valid_to > datetime($as_of))
                OPTIONAL MATCH (r)-[:HAS_SUBRULE]->(sr:SubRule)
                OPTIONAL MATCH (sr)-[:HAS_PROVISO]->(p:Proviso)
                OPTIONAL MATCH (sr)-[:HAS_CLAUSE]->(c:Clause)
                OPTIONAL MATCH (sr)-[:REQUIRES_FORM]->(f:Form)
                RETURN r, 
                       collect(DISTINCT {
                         id: sr.id, 
                         label: sr.label, 
                         content: sr.content,
                         provisos: collect(DISTINCT {id: p.id, label: p.label, ordinal: p.ordinal, content: p.content}),
                         clauses: collect(DISTINCT {id: c.id, label: c.label, content: c.content})
                       }) as subrules,
                       collect(DISTINCT {id: f.id, form_number: f.form_number}) as linked_forms
                ORDER BY r.valid_from DESC
                LIMIT 1
            """, rule_id=rule_id, as_of=as_of.isoformat())
            
            record = result.single()
            if not record:
                return None
            
            rule_data = record["r"]
            
            # Query subrules separately for cleaner data
            subrules_result = session.run("""
                MATCH (r:Rule {id: $rule_id})-[:HAS_SUBRULE]->(sr:SubRule)
                OPTIONAL MATCH (sr)-[:HAS_PROVISO]->(p:Proviso)
                OPTIONAL MATCH (sr)-[:HAS_CLAUSE]->(c:Clause)
                RETURN sr, 
                       collect(DISTINCT p) as provisos,
                       collect(DISTINCT c) as clauses
                ORDER BY sr.label
            """, rule_id=rule_id)
            
            subrules = []
            for sr_record in subrules_result:
                sr = sr_record["sr"]
                subrules.append({
                    "id": sr["id"],
                    "label": sr["label"],
                    "content": sr["content"],
                    "provisos": [
                        {"id": p["id"], "label": p["label"], "ordinal": p["ordinal"], "content": p["content"]}
                        for p in sr_record["provisos"] if p
                    ],
                    "clauses": [
                        {"id": c["id"], "label": c["label"], "content": c["content"]}
                        for c in sr_record["clauses"] if c
                    ]
                })
            
            # Query linked forms
            forms_result = session.run("""
                MATCH (r:Rule {id: $rule_id})-[:HAS_SUBRULE]->(sr)-[:REQUIRES_FORM]->(f:Form)
                RETURN DISTINCT f.id as id, f.form_number as form_number, f.title as title
            """, rule_id=rule_id)
            
            linked_forms = [dict(f) for f in forms_result]
            
            return RuleState(
                id=rule_data["id"],
                label=rule_data["label"],
                heading=rule_data["heading"],
                content=rule_data.get("content", ""),
                version=rule_data["version"],
                valid_from=rule_data["valid_from"].to_native() if hasattr(rule_data["valid_from"], 'to_native') else rule_data["valid_from"],
                valid_to=rule_data.get("valid_to"),
                subrules=subrules,
                linked_forms=linked_forms
            )
    
    def get_form(self, form_id: str, as_of: datetime) -> Optional[FormState]:
        """
        Get the state of a form at a specific date.
        """
        with self.driver.session() as session:
            result = session.run("""
                MATCH (f:Form)
                WHERE f.id = $form_id
                  AND f.valid_from <= datetime($as_of)
                  AND (f.valid_to IS NULL OR f.valid_to > datetime($as_of))
                OPTIONAL MATCH (f)-[:HAS_SECTION]->(s:FormSection)
                RETURN f, collect(s) as sections
                ORDER BY f.valid_from DESC
                LIMIT 1
            """, form_id=form_id, as_of=as_of.isoformat())
            
            record = result.single()
            if not record:
                return None
            
            form_data = record["f"]
            sections = [
                {
                    "id": s["id"],
                    "section_label": s["section_label"],
                    "heading": s.get("heading", ""),
                    "schema_payload": s.get("schema_payload", "{}")
                }
                for s in record["sections"] if s
            ]
            
            return FormState(
                id=form_data["id"],
                form_number=form_data["form_number"],
                title=form_data["title"],
                version=form_data["version"],
                valid_from=form_data["valid_from"].to_native() if hasattr(form_data["valid_from"], 'to_native') else form_data["valid_from"],
                sections=sections
            )
    
    def get_rule_with_dependencies(
        self, 
        rule_id: str, 
        as_of: datetime,
        follow_mutatis_mutandis: bool = True,
        follow_subject_to: bool = True,
        max_depth: int = 2
    ) -> dict:
        """
        Get a rule along with all its semantic dependencies.
        
        This is the "killer feature" for RAG - automatically expands context
        by following edges like MUTATIS_MUTANDIS.
        
        Returns:
            {
                "primary_rule": RuleState,
                "inherited_rules": [RuleState, ...],  # via MUTATIS_MUTANDIS
                "subject_to_rules": [RuleState, ...],  # via SUBJECT_TO
                "required_forms": [FormState, ...]
            }
        """
        result = {
            "primary_rule": self.get_rule(rule_id, as_of),
            "inherited_rules": [],
            "subject_to_rules": [],
            "required_forms": []
        }
        
        if not result["primary_rule"]:
            return result
        
        with self.driver.session() as session:
            # Find MUTATIS_MUTANDIS edges
            if follow_mutatis_mutandis:
                mm_result = session.run("""
                    MATCH (r:Rule {id: $rule_id})-[:HAS_SUBRULE]->(sr)-[:MUTATIS_MUTANDIS]->(target)
                    RETURN DISTINCT target.id as target_id, 
                           sr.id as source_subrule,
                           [(target)-[:HAS_SUBRULE]->(tsr) | tsr.label] as applicable_subrules
                """, rule_id=rule_id)
                
                for record in mm_result:
                    inherited_rule = self.get_rule(record["target_id"], as_of)
                    if inherited_rule:
                        # Filter to only the relevant sub-rules if specified
                        result["inherited_rules"].append({
                            "rule": inherited_rule,
                            "via_subrule": record["source_subrule"],
                            "applicable_subrules": record["applicable_subrules"]
                        })
            
            # Find SUBJECT_TO edges
            if follow_subject_to:
                st_result = session.run("""
                    MATCH (r:Rule {id: $rule_id})-[:SUBJECT_TO]->(target:Rule)
                    RETURN DISTINCT target.id as target_id
                """, rule_id=rule_id)
                
                for record in st_result:
                    subject_rule = self.get_rule(record["target_id"], as_of)
                    if subject_rule:
                        result["subject_to_rules"].append(subject_rule)
            
            # Find REQUIRES_FORM edges
            forms_result = session.run("""
                MATCH (r:Rule {id: $rule_id})-[:HAS_SUBRULE]->(sr)-[:REQUIRES_FORM]->(f:Form)
                RETURN DISTINCT f.id as form_id
            """, rule_id=rule_id)
            
            for record in forms_result:
                form = self.get_form(record["form_id"], as_of)
                if form:
                    result["required_forms"].append(form)
        
        return result
    
    def compare_versions(
        self, 
        rule_id: str, 
        date1: datetime, 
        date2: datetime
    ) -> dict:
        """
        Compare a rule at two different points in time.
        
        Useful for showing "what changed" between dates.
        """
        version1 = self.get_rule(rule_id, date1)
        version2 = self.get_rule(rule_id, date2)
        
        return {
            "rule_id": rule_id,
            "date1": date1.isoformat(),
            "date2": date2.isoformat(),
            "version1": version1,
            "version2": version2,
            "versions_differ": (
                version1 is not None and 
                version2 is not None and 
                version1.version != version2.version
            )
        }


# CLI interface for testing
def main():
    """Test the time-travel engine."""
    from datetime import datetime
    
    engine = TimeTravelEngine()
    
    try:
        engine.connect()
        print("✓ Connected to Neo4j")
        
        # Test: Get Rule 8 as of today
        today = datetime.now()
        rule8 = engine.get_rule("CGST_Rules/Rule_8", today)
        
        if rule8:
            print(f"\n{'='*60}")
            print(f"Rule 8 as of {today.date()}")
            print(f"{'='*60}")
            print(f"Version: {rule8.version}")
            print(f"Valid from: {rule8.valid_from}")
            print(f"Sub-rules: {len(rule8.subrules)}")
            print(f"Linked forms: {[f['form_number'] for f in rule8.linked_forms]}")
            print(f"\n{rule8.to_text()[:500]}...")
        else:
            print("⚠ Rule 8 not found")
        
        # Test: Get Form REG-01
        form = engine.get_form("FORM_GST_REG_01", today)
        if form:
            print(f"\n{'='*60}")
            print(f"Form {form.form_number} as of {today.date()}")
            print(f"{'='*60}")
            print(f"Title: {form.title}")
            print(f"Sections: {[s['section_label'] for s in form.sections]}")
            
            # Show Part A schema
            part_a = form.get_section_schema("Part A")
            if part_a:
                print(f"\nPart A fields: {list(part_a.get('properties', {}).keys())}")
        
        # Test: Get Rule with dependencies
        print(f"\n{'='*60}")
        print("Testing dependency resolution...")
        print(f"{'='*60}")
        deps = engine.get_rule_with_dependencies("CGST_Rules/Rule_8", today)
        print(f"Primary rule: {deps['primary_rule'].heading if deps['primary_rule'] else 'None'}")
        print(f"Inherited rules (mutatis mutandis): {len(deps['inherited_rules'])}")
        print(f"Subject to rules: {len(deps['subject_to_rules'])}")
        print(f"Required forms: {[f.form_number for f in deps['required_forms']]}")
        
    finally:
        engine.close()


if __name__ == "__main__":
    main()
