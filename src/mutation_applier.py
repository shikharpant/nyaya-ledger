"""
Mutation Applier - Applies parsed mutations to the Legal AST using Copy-on-Write.

This module:
1. Takes parsed MutationOperations
2. Validates targets exist (Reference Check)
3. Resolves anchors (Anchor Check)
4. Creates new versions using Copy-on-Write pattern
5. Validates temporal integrity (no overlaps)
"""

import json
import hashlib
from datetime import datetime
from typing import Optional, Tuple
from dataclasses import dataclass
from neo4j import Driver, GraphDatabase
from dotenv import load_dotenv
import os

from .mutation_parser import ParsedMutation, ParsedNotification
from .anchor_resolver import (
    resolve_anchor, 
    compute_splice_result, 
    compute_substitute_result,
    AnchorNotFoundError
)

load_dotenv()


@dataclass
class MutationResult:
    """Result of applying a single mutation."""
    mutation_id: str
    success: bool
    new_node_id: Optional[str] = None
    new_version: Optional[str] = None
    error: Optional[str] = None
    needs_review: bool = False
    review_reason: Optional[str] = None


@dataclass
class BatchResult:
    """Result of applying a batch of mutations."""
    notification_id: str
    total_mutations: int
    successful: int
    failed: int
    needs_review: int
    results: list[MutationResult]
    
    @property
    def all_successful(self) -> bool:
        return self.failed == 0 and self.needs_review == 0


class MutationApplier:
    """Applies mutations to the Legal AST graph."""
    
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
    
    def compute_hash(self, content: str) -> str:
        """Compute content hash."""
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def validate_target_exists(self, node_path: str) -> Tuple[bool, Optional[str]]:
        """
        Validate that the target node exists.
        
        Returns (exists, node_label) or (False, error_message)
        """
        with self.driver.session() as session:
            # Parse the node path
            parts = node_path.split("/")
            
            # Try to find the node
            if "SubRule" in node_path:
                result = session.run("""
                    MATCH (sr:SubRule {id: $id})
                    RETURN sr.label as label
                """, id=node_path)
            elif "Rule" in node_path:
                result = session.run("""
                    MATCH (r:Rule {id: $id})
                    RETURN r.label as label
                """, id=node_path)
            else:
                return False, f"Unknown node type in path: {node_path}"
            
            record = result.single()
            if record:
                return True, record["label"]
            else:
                return False, f"Node not found: {node_path}"
    
    def get_current_content(self, node_path: str) -> Optional[str]:
        """Get the current content of a node."""
        with self.driver.session() as session:
            if "SubRule" in node_path:
                result = session.run("""
                    MATCH (sr:SubRule {id: $id})
                    RETURN sr.content as content
                """, id=node_path)
            elif "Rule" in node_path:
                result = session.run("""
                    MATCH (r:Rule {id: $id})
                    RETURN r.content as content
                """, id=node_path)
            else:
                return None
            
            record = result.single()
            return record["content"] if record else None
    
    def apply_mutation(
        self, 
        mutation: ParsedMutation, 
        effective_date: datetime,
        source_notification: str,
        dry_run: bool = False
    ) -> MutationResult:
        """
        Apply a single mutation to the graph.
        
        Uses Copy-on-Write: creates new version instead of modifying in place.
        """
        # Step 1: Validate target exists
        exists, label_or_error = self.validate_target_exists(mutation.target_node_path)
        if not exists:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=False,
                error=f"Reference Check Failed: {label_or_error}"
            )
        
        # Step 2: Handle based on operation type
        try:
            if mutation.operation == "INSERT_SIBLING":
                return self._apply_insert_sibling(mutation, effective_date, source_notification, dry_run)
            
            elif mutation.operation == "INSERT_CHILD":
                return self._apply_insert_child(mutation, effective_date, source_notification, dry_run)
            
            elif mutation.operation == "SPLICE":
                return self._apply_splice(mutation, effective_date, source_notification, dry_run)
            
            elif mutation.operation == "SUBSTITUTE":
                return self._apply_substitute(mutation, effective_date, source_notification, dry_run)
            
            elif mutation.operation in ("DELETE", "OMIT"):
                return self._apply_delete(mutation, effective_date, source_notification, dry_run)
            
            else:
                return MutationResult(
                    mutation_id=mutation.mutation_id,
                    success=False,
                    error=f"Unknown operation: {mutation.operation}"
                )
                
        except AnchorNotFoundError as e:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=False,
                error=f"Anchor Check Failed: {e}"
            )
        except Exception as e:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=False,
                error=f"Unexpected error: {str(e)}"
            )
    
    def _apply_insert_sibling(
        self, 
        mutation: ParsedMutation,
        effective_date: datetime,
        source_notification: str,
        dry_run: bool
    ) -> MutationResult:
        """Insert a new sibling node after/before the target."""
        payload = mutation.payload
        new_label = payload.get("label", "")
        heading = payload.get("heading", "")
        content = payload.get("content", "")
        node_type = payload.get("node_type", "rule")
        
        # Determine parent from target path
        target_parts = mutation.target_node_path.split("/")
        
        if node_type == "rule":
            # New rule goes under the same chapter
            # Get the chapter from the target rule
            with self.driver.session() as session:
                result = session.run("""
                    MATCH (c:Chapter)-[:HAS_RULE]->(r:Rule {id: $target_id})
                    RETURN c.id as chapter_id
                """, target_id=mutation.target_node_path)
                record = result.single()
                if not record:
                    return MutationResult(
                        mutation_id=mutation.mutation_id,
                        success=False,
                        error="Could not find parent chapter"
                    )
                chapter_id = record["chapter_id"]
            
            # Create new rule ID
            new_id = f"CGST_Rules/Rule_{new_label}"
            
            if dry_run:
                return MutationResult(
                    mutation_id=mutation.mutation_id,
                    success=True,
                    new_node_id=new_id,
                    new_version="v1"
                )
            
            with self.driver.session() as session:
                session.run("""
                    MATCH (c:Chapter {id: $chapter_id})
                    CREATE (r:Rule {
                        id: $new_id,
                        label: $label,
                        heading: $heading,
                        content: $content,
                        version: 'v1',
                        valid_from: datetime($valid_from),
                        content_hash: $content_hash,
                        source_mutation: $source_notification
                    })
                    CREATE (c)-[:HAS_RULE]->(r)
                """,
                    chapter_id=chapter_id,
                    new_id=new_id,
                    label=new_label,
                    heading=heading,
                    content=content,
                    valid_from=effective_date.isoformat(),
                    content_hash=self.compute_hash(content),
                    source_notification=source_notification
                )
            
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=True,
                new_node_id=new_id,
                new_version="v1"
            )
        
        else:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=False,
                error=f"INSERT_SIBLING for {node_type} not yet implemented"
            )
    
    def _apply_insert_child(
        self,
        mutation: ParsedMutation,
        effective_date: datetime,
        source_notification: str,
        dry_run: bool
    ) -> MutationResult:
        """Insert a new child node (sub-rule, clause, etc.)."""
        payload = mutation.payload
        new_label = payload.get("label", "")
        content = payload.get("content", "")
        node_type = payload.get("node_type", "subrule")
        
        if node_type == "subrule":
            # Create new sub-rule under the target rule
            new_id = f"{mutation.target_node_path}/SubRule_{new_label.strip('()')}"
            
            if dry_run:
                return MutationResult(
                    mutation_id=mutation.mutation_id,
                    success=True,
                    new_node_id=new_id,
                    new_version="v1"
                )
            
            with self.driver.session() as session:
                session.run("""
                    MATCH (r:Rule {id: $parent_id})
                    CREATE (sr:SubRule {
                        id: $new_id,
                        label: $label,
                        content: $content,
                        version: 'v1',
                        valid_from: datetime($valid_from),
                        content_hash: $content_hash,
                        source_mutation: $source_notification
                    })
                    CREATE (r)-[:HAS_SUBRULE]->(sr)
                """,
                    parent_id=mutation.target_node_path,
                    new_id=new_id,
                    label=new_label,
                    content=content,
                    valid_from=effective_date.isoformat(),
                    content_hash=self.compute_hash(content),
                    source_notification=source_notification
                )
            
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=True,
                new_node_id=new_id,
                new_version="v1"
            )
        
        else:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=False,
                error=f"INSERT_CHILD for {node_type} not yet implemented"
            )
    
    def _apply_splice(
        self,
        mutation: ParsedMutation,
        effective_date: datetime,
        source_notification: str,
        dry_run: bool
    ) -> MutationResult:
        """Insert text at an anchor position within existing content."""
        if not mutation.anchor:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=False,
                error="SPLICE operation requires an anchor"
            )
        
        # Get current content
        current_content = self.get_current_content(mutation.target_node_path)
        if current_content is None:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=False,
                error=f"Could not get content for {mutation.target_node_path}"
            )
        
        # Compute new content using anchor resolver
        insert_text = mutation.payload.get("content", "")
        position = mutation.anchor_position or "after"
        
        new_content, anchor_match = compute_splice_result(
            current_content,
            mutation.anchor,
            insert_text,
            position
        )
        
        if dry_run:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=True,
                new_version="v2",
                needs_review=anchor_match.needs_review,
                review_reason="Normalized match used" if anchor_match.needs_review else None
            )
        
        # Copy-on-Write: Create new version
        return self._create_new_version(
            mutation.target_node_path,
            new_content,
            effective_date,
            source_notification,
            mutation.mutation_id,
            anchor_match.needs_review
        )
    
    def _apply_substitute(
        self,
        mutation: ParsedMutation,
        effective_date: datetime,
        source_notification: str,
        dry_run: bool
    ) -> MutationResult:
        """Substitute one text pattern with another."""
        # Get current content
        current_content = self.get_current_content(mutation.target_node_path)
        if current_content is None:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=False,
                error=f"Could not get content for {mutation.target_node_path}"
            )
        
        pattern = mutation.anchor or mutation.payload.get("pattern", "")
        replacement = mutation.payload.get("content", "") or mutation.payload.get("replacement", "")
        
        new_content, count = compute_substitute_result(
            current_content,
            pattern,
            replacement
        )
        
        if dry_run:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=True,
                new_version="v2"
            )
        
        return self._create_new_version(
            mutation.target_node_path,
            new_content,
            effective_date,
            source_notification,
            mutation.mutation_id,
            False
        )
    
    def _apply_delete(
        self,
        mutation: ParsedMutation,
        effective_date: datetime,
        source_notification: str,
        dry_run: bool
    ) -> MutationResult:
        """Delete/omit a node (marks as deleted, doesn't physically remove)."""
        if dry_run:
            return MutationResult(
                mutation_id=mutation.mutation_id,
                success=True,
                new_version="deleted"
            )
        
        with self.driver.session() as session:
            # Set valid_to on the current version
            if "SubRule" in mutation.target_node_path:
                session.run("""
                    MATCH (sr:SubRule {id: $id})
                    WHERE sr.valid_to IS NULL
                    SET sr.valid_to = datetime($valid_to),
                        sr.deleted_by = $source
                """, id=mutation.target_node_path, 
                    valid_to=effective_date.isoformat(),
                    source=source_notification)
            else:
                session.run("""
                    MATCH (r:Rule {id: $id})
                    WHERE r.valid_to IS NULL
                    SET r.valid_to = datetime($valid_to),
                        r.deleted_by = $source
                """, id=mutation.target_node_path,
                    valid_to=effective_date.isoformat(),
                    source=source_notification)
        
        return MutationResult(
            mutation_id=mutation.mutation_id,
            success=True,
            new_version="deleted"
        )
    
    def _create_new_version(
        self,
        node_path: str,
        new_content: str,
        effective_date: datetime,
        source_notification: str,
        mutation_id: str,
        needs_review: bool
    ) -> MutationResult:
        """
        Create a new version of a node using Copy-on-Write pattern.
        
        1. Set valid_to on current version
        2. Create new version with incremented version number
        3. Create NEXT_VERSION edge
        """
        with self.driver.session() as session:
            if "SubRule" in node_path:
                # Get current version info
                result = session.run("""
                    MATCH (sr:SubRule {id: $id})
                    WHERE sr.valid_to IS NULL
                    RETURN sr.version as version, sr.label as label
                """, id=node_path)
                record = result.single()
                
                if not record:
                    return MutationResult(
                        mutation_id=mutation_id,
                        success=False,
                        error="Current version not found"
                    )
                
                current_version = record["version"]
                label = record["label"]
                new_version = f"v{int(current_version[1:]) + 1}"
                new_id = f"{node_path}_{new_version}"
                
                # Update current version's valid_to
                session.run("""
                    MATCH (sr:SubRule {id: $id})
                    WHERE sr.valid_to IS NULL
                    SET sr.valid_to = datetime($valid_to)
                """, id=node_path, valid_to=effective_date.isoformat())
                
                # Create new version
                session.run("""
                    MATCH (old:SubRule {id: $old_id, version: $old_version})
                    MATCH (parent:Rule)-[:HAS_SUBRULE]->(old)
                    CREATE (new:SubRule {
                        id: $old_id,
                        label: $label,
                        content: $new_content,
                        version: $new_version,
                        valid_from: datetime($valid_from),
                        content_hash: $content_hash,
                        source_mutation: $source_notification
                    })
                    CREATE (parent)-[:HAS_SUBRULE]->(new)
                    CREATE (old)-[:NEXT_VERSION {effective_date: datetime($valid_from)}]->(new)
                """,
                    old_id=node_path,
                    old_version=current_version,
                    label=label,
                    new_content=new_content,
                    new_version=new_version,
                    valid_from=effective_date.isoformat(),
                    content_hash=self.compute_hash(new_content),
                    source_notification=source_notification
                )
                
                return MutationResult(
                    mutation_id=mutation_id,
                    success=True,
                    new_node_id=node_path,
                    new_version=new_version,
                    needs_review=needs_review,
                    review_reason="Normalized anchor match" if needs_review else None
                )
            
            else:
                return MutationResult(
                    mutation_id=mutation_id,
                    success=False,
                    error="Version creation for Rule nodes not yet implemented"
                )
    
    def apply_batch(
        self,
        notification: ParsedNotification,
        dry_run: bool = False
    ) -> BatchResult:
        """
        Apply all mutations from a notification as a batch.
        
        If dry_run=True, validates without making changes.
        """
        results = []
        
        for mutation in notification.mutations:
            result = self.apply_mutation(
                mutation,
                notification.effective_date,
                notification.notification_id,
                dry_run
            )
            results.append(result)
        
        return BatchResult(
            notification_id=notification.notification_id,
            total_mutations=len(results),
            successful=sum(1 for r in results if r.success and not r.needs_review),
            failed=sum(1 for r in results if not r.success),
            needs_review=sum(1 for r in results if r.needs_review),
            results=results
        )


def main():
    """Test the mutation applier."""
    from .mutation_parser import parse_notification_offline
    
    sample_notification = """
    2. After rule 9 of the said rules, the following rule shall be inserted, namely:-
       "9A. Grant of registration electronically.- The registration shall be granted 
       electronically in FORM GST REG-06."
    
    3. In rule 10, in sub-rule (1), after the words "under rule 9," the words 
       "rule 9A and rule 14A," shall be inserted.
    """
    
    # Parse
    parsed = parse_notification_offline(sample_notification)
    parsed.effective_date = datetime(2025, 11, 1)
    parsed.notification_id = "Noti_18_2025_CT"
    
    print(f"Parsed {len(parsed.mutations)} mutations")
    
    # Apply (dry run)
    with MutationApplier() as applier:
        batch_result = applier.apply_batch(parsed, dry_run=True)
        
        print(f"\nDry run results:")
        print(f"  Total: {batch_result.total_mutations}")
        print(f"  Successful: {batch_result.successful}")
        print(f"  Failed: {batch_result.failed}")
        print(f"  Needs review: {batch_result.needs_review}")
        
        for result in batch_result.results:
            status = "✓" if result.success else "✗"
            print(f"  {status} {result.mutation_id}: {result.error or 'OK'}")


if __name__ == "__main__":
    main()
