"""
Graph Loader - Loads genesis JSON into Neo4j.

This module:
1. Connects to Neo4j
2. Creates constraints and indexes
3. Loads genesis block data (rules, forms, edges)
4. Validates temporal integrity (no overlapping versions)
"""

import json
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional, Any
from neo4j import GraphDatabase, Driver
from dotenv import load_dotenv
import os

load_dotenv()


class GraphLoader:
    """Loads Legal AST data into Neo4j."""
    
    def __init__(self, uri: str = None, user: str = None, password: str = None):
        self.uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or os.getenv("NEO4J_USER", "neo4j")
        self.password = password or os.getenv("NEO4J_PASSWORD", "gitforlaw123")
        self.driver: Optional[Driver] = None
    
    def connect(self) -> None:
        """Establish connection to Neo4j."""
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        # Verify connection
        with self.driver.session() as session:
            session.run("RETURN 1")
        print(f"✓ Connected to Neo4j at {self.uri}")
    
    def close(self) -> None:
        """Close the connection."""
        if self.driver:
            self.driver.close()
    
    def setup_schema(self) -> None:
        """Create constraints and indexes for the Legal AST."""
        with self.driver.session() as session:
            # Constraints for unique IDs
            constraints = [
                "CREATE CONSTRAINT rule_id IF NOT EXISTS FOR (r:Rule) REQUIRE r.id IS UNIQUE",
                "CREATE CONSTRAINT subrule_id IF NOT EXISTS FOR (s:SubRule) REQUIRE s.id IS UNIQUE",
                "CREATE CONSTRAINT proviso_id IF NOT EXISTS FOR (p:Proviso) REQUIRE p.id IS UNIQUE",
                "CREATE CONSTRAINT form_id IF NOT EXISTS FOR (f:Form) REQUIRE f.id IS UNIQUE",
                "CREATE CONSTRAINT section_id IF NOT EXISTS FOR (s:FormSection) REQUIRE s.id IS UNIQUE",
                "CREATE CONSTRAINT statute_id IF NOT EXISTS FOR (s:Statute) REQUIRE s.id IS UNIQUE",
                "CREATE CONSTRAINT chapter_id IF NOT EXISTS FOR (c:Chapter) REQUIRE c.id IS UNIQUE",
            ]
            
            # Indexes for temporal queries
            indexes = [
                "CREATE INDEX rule_valid_from IF NOT EXISTS FOR (r:Rule) ON (r.valid_from)",
                "CREATE INDEX rule_version IF NOT EXISTS FOR (r:Rule) ON (r.version)",
                "CREATE INDEX subrule_valid_from IF NOT EXISTS FOR (s:SubRule) ON (s.valid_from)",
            ]
            
            for constraint in constraints:
                try:
                    session.run(constraint)
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        print(f"Warning: {e}")
            
            for index in indexes:
                try:
                    session.run(index)
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        print(f"Warning: {e}")
            
            print("✓ Schema constraints and indexes created")
    
    def clear_database(self) -> None:
        """Clear all nodes and relationships (for testing)."""
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
        print("✓ Database cleared")
    
    def compute_hash(self, content: str) -> str:
        """Compute SHA256 hash of content."""
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def load_rules_genesis(self, genesis_path: str) -> dict:
        """
        Load rules from genesis JSON into Neo4j.
        
        Returns dict with counts of created nodes.
        """
        with open(genesis_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        counts = {"statutes": 0, "chapters": 0, "rules": 0, "subrules": 0, 
                  "provisos": 0, "clauses": 0, "edges": 0}
        
        with self.driver.session() as session:
            # Create Statute node
            statute_id = data["statute_id"]
            session.run("""
                MERGE (s:Statute {id: $id})
                SET s.title = $title,
                    s.valid_from = datetime($valid_from)
            """, id=statute_id, title=data["title"], 
                valid_from=data["_metadata"]["effective_date"] + "T00:00:00Z")
            counts["statutes"] += 1
            
            # Create Chapter node
            chapter = data["chapter"]
            session.run("""
                MERGE (c:Chapter {id: $id})
                SET c.number = $number,
                    c.title = $title
                WITH c
                MATCH (s:Statute {id: $statute_id})
                MERGE (s)-[:HAS_CHAPTER]->(c)
            """, id=chapter["id"], number=chapter["number"], 
                title=chapter["title"], statute_id=statute_id)
            counts["chapters"] += 1
            
            # Create Rule nodes
            for rule in data["rules"]:
                # Create rule node
                session.run("""
                    MERGE (r:Rule {id: $id})
                    SET r.label = $label,
                        r.heading = $heading,
                        r.content = $content,
                        r.version = $version,
                        r.valid_from = datetime($valid_from),
                        r.content_hash = $content_hash
                    WITH r
                    MATCH (c:Chapter {id: $chapter_id})
                    MERGE (c)-[:HAS_RULE]->(r)
                """, 
                    id=rule["id"],
                    label=rule["label"],
                    heading=rule["heading"],
                    content=rule.get("content", ""),
                    version=rule["version"],
                    valid_from=rule["valid_from"],
                    content_hash=self.compute_hash(rule.get("content", "")),
                    chapter_id=chapter["id"]
                )
                counts["rules"] += 1
                
                # Create SubRule nodes
                for subrule in rule.get("subrules", []):
                    session.run("""
                        MERGE (sr:SubRule {id: $id})
                        SET sr.label = $label,
                            sr.content = $content,
                            sr.version = $version,
                            sr.valid_from = datetime($valid_from),
                            sr.content_hash = $content_hash
                        WITH sr
                        MATCH (r:Rule {id: $rule_id})
                        MERGE (r)-[:HAS_SUBRULE]->(sr)
                    """,
                        id=subrule["id"],
                        label=subrule["label"],
                        content=subrule["content"],
                        version=rule["version"],
                        valid_from=rule["valid_from"],
                        content_hash=self.compute_hash(subrule["content"]),
                        rule_id=rule["id"]
                    )
                    counts["subrules"] += 1
                    
                    # Create Proviso nodes
                    for proviso in subrule.get("provisos", []):
                        session.run("""
                            MERGE (p:Proviso {id: $id})
                            SET p.label = $label,
                                p.ordinal = $ordinal,
                                p.content = $content
                            WITH p
                            MATCH (sr:SubRule {id: $subrule_id})
                            MERGE (sr)-[:HAS_PROVISO]->(p)
                        """,
                            id=proviso["id"],
                            label=proviso["label"],
                            ordinal=proviso["ordinal"],
                            content=proviso["content"],
                            subrule_id=subrule["id"]
                        )
                        counts["provisos"] += 1
                    
                    # Create Clause nodes
                    for clause in subrule.get("clauses", []):
                        session.run("""
                            MERGE (cl:Clause {id: $id})
                            SET cl.label = $label,
                                cl.content = $content
                            WITH cl
                            MATCH (sr:SubRule {id: $subrule_id})
                            MERGE (sr)-[:HAS_CLAUSE]->(cl)
                        """,
                            id=clause["id"],
                            label=clause["label"],
                            content=clause["content"],
                            subrule_id=subrule["id"]
                        )
                        counts["clauses"] += 1
                    
                    # Create semantic edges from subrule
                    for edge in subrule.get("edges", []):
                        counts["edges"] += self._create_edge(session, subrule["id"], edge)
        
        return counts
    
    def load_form_genesis(self, form_path: str) -> dict:
        """
        Load form from genesis JSON into Neo4j.
        
        Forms use HYBRID storage:
        - Form and FormSection are graph nodes
        - Field schemas stored as JSON property in FormSection
        """
        with open(form_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        counts = {"forms": 0, "sections": 0}
        
        with self.driver.session() as session:
            # Create Form node
            session.run("""
                MERGE (f:Form {id: $id})
                SET f.form_number = $form_number,
                    f.title = $title,
                    f.version = $version,
                    f.valid_from = datetime($valid_from)
            """,
                id=data["form_id"],
                form_number=data["form_number"],
                title=data["title"],
                version=data["version"],
                valid_from=data["valid_from"]
            )
            counts["forms"] += 1
            
            # Create DEFINED_BY edge if specified
            if "defined_by_rule" in data:
                session.run("""
                    MATCH (f:Form {id: $form_id})
                    MATCH (r:Rule {id: $rule_id})
                    MERGE (f)-[:DEFINED_BY]->(r)
                """, form_id=data["form_id"], rule_id=data["defined_by_rule"])
            
            # Create FormSection nodes
            for section in data["sections"]:
                # Store schema_payload as JSON string (Neo4j doesn't support nested maps well)
                schema_json = json.dumps(section["schema_payload"])
                
                # Handle table columns if present
                table_columns_json = json.dumps(section.get("table_columns", [])) if section.get("table_columns") else None
                table_column_widths_json = json.dumps(section.get("table_column_widths", [])) if section.get("table_column_widths") else None
                
                session.run("""
                    MERGE (s:FormSection {id: $id})
                    SET s.section_label = $section_label,
                        s.heading = $heading,
                        s.description = $description,
                        s.section_type = $section_type,
                        s.table_type = $table_type,
                        s.table_columns = $table_columns,
                        s.table_column_widths = $table_column_widths,
                        s.has_serial_column = $has_serial_column,
                        s.has_total_row = $has_total_row,
                        s.instructions = $instructions,
                        s.schema_payload = $schema_payload,
                        s.version = $version,
                        s.valid_from = datetime($valid_from)
                    WITH s
                    MATCH (f:Form {id: $form_id})
                    MERGE (f)-[:HAS_SECTION]->(s)
                """,
                    id=section["section_id"],
                    section_label=section["section_label"],
                    heading=section.get("heading", ""),
                    description=section.get("description", ""),
                    section_type=section.get("section_type", "fields"),
                    table_type=section.get("table_type"),
                    table_columns=table_columns_json,
                    table_column_widths=table_column_widths_json,
                    has_serial_column=section.get("has_serial_column", False),
                    has_total_row=section.get("has_total_row", False),
                    instructions=section.get("instructions"),
                    schema_payload=schema_json,
                    version=data["version"],
                    valid_from=data["valid_from"],
                    form_id=data["form_id"]
                )
                counts["sections"] += 1
        
        return counts
    
    def _create_edge(self, session, source_id: str, edge: dict) -> int:
        """Create a semantic edge in the graph."""
        edge_type = edge["type"]
        target_id = edge["target"]
        properties = edge.get("properties", {})
        
        # Determine source node type from ID
        if "/SubRule_" in source_id:
            source_label = "SubRule"
        elif "/Rule_" in source_id or source_id.startswith("CGST_Rules/Rule_"):
            source_label = "Rule"
        else:
            source_label = "Rule"  # Default
        
        # For REQUIRES_FORM edges, target is a Form
        if edge_type == "REQUIRES_FORM":
            props_str = ", ".join([f"e.{k} = ${k}" for k in properties.keys()])
            query = f"""
                MATCH (src:{source_label} {{id: $source_id}})
                MERGE (tgt:Form {{id: $target_id}})
                MERGE (src)-[e:REQUIRES_FORM]->(tgt)
                SET {props_str if props_str else "e.created = true"}
            """
            session.run(query, source_id=source_id, target_id=target_id, **properties)
            return 1
        
        # For REFERS_TO edges, we create a placeholder if target doesn't exist
        elif edge_type == "REFERS_TO":
            # Store as a property on the source node for now
            # (Full cross-statute linking requires more data)
            return 0
        
        return 0
    
    def validate_temporal_integrity(self) -> list[str]:
        """
        Check that no node has overlapping version time ranges.
        
        Returns list of violations.
        """
        violations = []
        
        with self.driver.session() as session:
            # Check for rules with same ID but overlapping valid_from/valid_to
            result = session.run("""
                MATCH (r1:Rule), (r2:Rule)
                WHERE r1.id = r2.id 
                  AND r1.version <> r2.version
                  AND r1.valid_from <= r2.valid_from
                  AND (r1.valid_to IS NULL OR r1.valid_to > r2.valid_from)
                RETURN r1.id as id, r1.version as v1, r2.version as v2
            """)
            
            for record in result:
                violations.append(
                    f"Temporal overlap: {record['id']} versions {record['v1']} and {record['v2']}"
                )
        
        if not violations:
            print("✓ Temporal integrity validated - no overlapping versions")
        else:
            print(f"⚠ Found {len(violations)} temporal integrity violations")
        
        return violations


    def load_act_genesis(self, act_path: str) -> dict:
        """
        Load Act sections from genesis JSON into Neo4j.
        """
        with open(act_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        counts = {"sections": 0, "subsections": 0, "clauses": 0, "provisos": 0}
        
        with self.driver.session() as session:
            # Create Statute node (if not exists)
            statute_id = data["statute_id"]
            session.run("""
                MERGE (s:Statute {id: $id})
                SET s.title = $title,
                    s.valid_from = datetime($valid_from)
            """, id=statute_id, title=data["title"], 
                valid_from=data["_metadata"]["effective_date"] + "T00:00:00Z")
            
            # Create Chapter nodes if present
            for chapter in data.get("chapters", []):
                session.run("""
                    MERGE (c:Chapter {id: $id})
                    SET c.number = $number,
                        c.title = $title
                    WITH c
                    MATCH (s:Statute {id: $statute_id})
                    MERGE (s)-[:HAS_CHAPTER]->(c)
                """, id=chapter["id"], number=chapter["number"], 
                    title=chapter["title"], statute_id=statute_id)
                
                # Create Section nodes within chapter
                for section in chapter.get("sections", []):
                    self._create_section(session, section, chapter["id"])
                    counts["sections"] += 1
            
            # Create orphan Sections (if no chapters)
            for section in data.get("sections", []):
                self._create_section(session, section, None, statute_id)
                counts["sections"] += 1
                
        return counts

    def _create_section(self, session, section, chapter_id=None, statute_id=None):
        """Helper to create section and children."""
        parent_query = ""
        parent_params = {}
        
        if chapter_id:
            parent_query = "MATCH (p:Chapter {id: $parent_id}) MERGE (p)-[:HAS_SECTION]->(s)"
            parent_params = {"parent_id": chapter_id}
        elif statute_id:
            parent_query = "MATCH (p:Statute {id: $parent_id}) MERGE (p)-[:HAS_SECTION]->(s)"
            parent_params = {"parent_id": statute_id}
            
        session.run(f"""
            MERGE (s:Section {{id: $id}})
            SET s.label = $label,
                s.heading = $heading,
                s.content = $content,
                s.version = $version,
                s.valid_from = datetime($valid_from),
                s.content_hash = $content_hash
            WITH s
            {parent_query}
        """, 
            id=section["id"],
            label=section["label"],
            heading=section["heading"],
            content=section.get("content", ""),
            version="v1",
            valid_from=section.get("valid_from"),
            content_hash=self.compute_hash(section.get("content", "")),
            **parent_params
        )
        
        # Create SubSections
        for subsection in section.get("subsections", []):
            session.run("""
                MERGE (ss:SubSection {id: $id})
                SET ss.label = $label,
                    ss.content = $content,
                    ss.version = $version,
                    ss.valid_from = datetime($valid_from),
                    ss.content_hash = $content_hash
                WITH ss
                MATCH (s:Section {id: $section_id})
                MERGE (s)-[:HAS_SUBSECTION]->(ss)
            """,
                id=subsection["id"],
                label=subsection["label"],
                content=subsection["content"],
                version="v1",
                valid_from=section.get("valid_from"),
                content_hash=self.compute_hash(subsection["content"]),
                section_id=section["id"]
            )
            
            # Add nested provisos/clauses logic here similar to Rules...

    def load_from_manifest(self, manifest_path: str) -> None:
        """Load all data defined in manifest.json."""
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        
        base_dir = Path(manifest_path).parent
        
        print(f"📦 Loading from manifest: {manifest['_metadata']['description']}")
        
        # 1. Load Acts (Statutes)
        for statute in manifest.get("statutes", []):
            if "genesis_file" in statute and (base_dir / statute["genesis_file"]).exists():
                print(f"  📜 Loading Act: {statute['title']}...")
                counts = self.load_act_genesis(str(base_dir / statute["genesis_file"]))
                print(f"     Loaded {counts}")
        
        # 2. Load Rules
        for rule_set in manifest.get("rules", []):
            print(f"  ⚖️  Loading Rules: {rule_set['title']}...")
            
            # Single file
            if "genesis_file" in rule_set and (base_dir / rule_set["genesis_file"]).exists():
                counts = self.load_rules_genesis(str(base_dir / rule_set["genesis_file"]))
                print(f"     Loaded {counts['rules']} rules from single file")
            
            # Multiple files (chapters)
            elif "genesis_files" in rule_set:
                total_rules = 0
                for file_path in rule_set["genesis_files"]:
                    full_path = base_dir / file_path
                    if full_path.exists():
                        counts = self.load_rules_genesis(str(full_path))
                        total_rules += counts["rules"]
                print(f"     Loaded {total_rules} rules from {len(rule_set['genesis_files'])} files")
        
        # 3. Load Forms
        # Scan forms directory directly or use categories
        forms_dir = base_dir / "forms"
        if forms_dir.exists():
            print("  📝 Loading Forms...")
            form_files = list(forms_dir.glob("*.json"))
            loaded_count = 0
            for form_file in form_files:
                try:
                    self.load_form_genesis(str(form_file))
                    loaded_count += 1
                except Exception as e:
                    print(f"     ❌ Error loading {form_file.name}: {e}")
            print(f"     Loaded {loaded_count} forms")


def load_genesis(data_dir: str = "data/genesis") -> None:
    """Load all genesis files into Neo4j."""
    loader = GraphLoader()
    
    try:
        loader.connect()
        loader.setup_schema()
        # loader.clear_database()  # Warning: don't auto-clear if incremental
        
        manifest_path = Path(data_dir) / "manifest.json"
        
        if manifest_path.exists():
            loader.load_from_manifest(str(manifest_path))
        else:
            print("⚠ manifest.json not found, falling back to legacy loading...")
            
            # Legacy fallback
            rules_path = Path(data_dir) / "legacy/cgst_rules_chapter3.json" # paths moved
            # ... (legacy logic omitted for brevity, encourage manifest use)
            print("Please create a manifest.json file.")
            
        # Validate
        loader.validate_temporal_integrity()
        
        print("\n✓ Genesis load complete!")
        print("  Open Neo4j Browser at http://localhost:7474")
        
    finally:
        loader.close()


if __name__ == "__main__":
    import sys
    
    # Get data directory from args or use default
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data/genesis"
    load_genesis(data_dir)
