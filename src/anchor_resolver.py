"""
Anchor Resolver - Resolves text anchors with strict matching.

This module implements the anchor resolution strategy:
1. EXACT MATCH → Return index
2. NORMALIZED MATCH → Return index + flag for review
3. NO MATCH → Raise error (FAIL LOUDLY)

Never use semantic/fuzzy matching for mutations - that's dangerous!
"""

import re
from dataclasses import dataclass
from typing import Optional, Tuple
from enum import Enum


class MatchType(Enum):
    """Type of match found."""
    EXACT = "exact"
    NORMALIZED = "normalized"
    NOT_FOUND = "not_found"


class AnchorNotFoundError(Exception):
    """Raised when an anchor cannot be resolved."""
    def __init__(self, anchor: str, target_id: str, target_preview: str):
        self.anchor = anchor
        self.target_id = target_id
        self.target_preview = target_preview
        super().__init__(
            f"Anchor not found: '{anchor}' in node {target_id}\n"
            f"Content preview: {target_preview[:200]}..."
        )


@dataclass
class AnchorMatch:
    """Result of anchor resolution."""
    anchor: str
    position: int          # Character index where anchor starts
    match_type: MatchType
    matched_text: str      # The actual text that matched (may differ from anchor if normalized)
    needs_review: bool     # True if normalized match (human should verify)


def normalize_text(text: str) -> str:
    """
    Normalize text for comparison.
    
    - Collapse multiple spaces/newlines to single space
    - Normalize quotes (" → ", ' → ')
    - Lowercase
    - Strip leading/trailing whitespace
    """
    # Replace various quote types with standard ones
    text = re.sub(r'[""]', '"', text)
    text = re.sub(r"['']", "'", text)
    
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)
    
    # Strip and lowercase
    text = text.strip().lower()
    
    return text


def find_anchor_position(target_content: str, anchor: str) -> Tuple[int, str]:
    """
    Find the starting position of anchor in target content.
    
    Returns (position, matched_text) or raises if not found.
    """
    # Try exact match first
    pos = target_content.find(anchor)
    if pos != -1:
        return pos, anchor
    
    # Not found
    return -1, ""


def resolve_anchor(
    target_content: str, 
    anchor: str,
    target_id: str = "unknown"
) -> AnchorMatch:
    """
    Resolve an anchor string to a position in target content.
    
    Follows strict priority:
    1. EXACT MATCH - Return position
    2. NORMALIZED MATCH - Return position with review flag
    3. NOT FOUND - Raise AnchorNotFoundError
    
    Args:
        target_content: The full text to search in
        anchor: The anchor string to find
        target_id: ID of target node (for error messages)
    
    Returns:
        AnchorMatch with position and match details
    
    Raises:
        AnchorNotFoundError: If anchor cannot be found
    """
    # Priority 1: Exact match
    pos, matched = find_anchor_position(target_content, anchor)
    if pos != -1:
        return AnchorMatch(
            anchor=anchor,
            position=pos,
            match_type=MatchType.EXACT,
            matched_text=matched,
            needs_review=False
        )
    
    # Priority 2: Normalized match
    normalized_content = normalize_text(target_content)
    normalized_anchor = normalize_text(anchor)
    
    pos_normalized = normalized_content.find(normalized_anchor)
    if pos_normalized != -1:
        # Find the actual position in original content
        # We need to map back from normalized position to original
        original_pos = _map_normalized_to_original(target_content, normalized_anchor)
        
        if original_pos != -1:
            # Extract the actual matched text from original
            end_pos = original_pos + len(anchor) + 10  # Approximate
            actual_matched = target_content[original_pos:end_pos].split()[0:len(anchor.split())]
            actual_matched = " ".join(actual_matched) if actual_matched else anchor
            
            return AnchorMatch(
                anchor=anchor,
                position=original_pos,
                match_type=MatchType.NORMALIZED,
                matched_text=actual_matched,
                needs_review=True  # Human should verify!
            )
    
    # Priority 3: FAIL LOUDLY
    raise AnchorNotFoundError(
        anchor=anchor,
        target_id=target_id,
        target_preview=target_content[:500]
    )


def _map_normalized_to_original(original: str, normalized_anchor: str) -> int:
    """
    Map a position in normalized text back to original text.
    
    This is approximate - finds the best matching position.
    """
    # Simple approach: find words from anchor in original
    anchor_words = normalized_anchor.split()
    if not anchor_words:
        return -1
    
    first_word = anchor_words[0]
    original_lower = original.lower()
    
    # Find all occurrences of first word
    pos = 0
    while True:
        pos = original_lower.find(first_word, pos)
        if pos == -1:
            break
        
        # Check if remaining words follow
        check_text = normalize_text(original[pos:pos + len(normalized_anchor) * 2])
        if check_text.startswith(normalized_anchor):
            return pos
        
        pos += 1
    
    return -1


def compute_splice_result(
    original_content: str,
    anchor: str,
    insert_text: str,
    position: str  # "before" or "after"
) -> Tuple[str, AnchorMatch]:
    """
    Compute the result of a SPLICE operation.
    
    Args:
        original_content: Original text
        anchor: Text anchor to find
        insert_text: Text to insert
        position: "before" or "after" the anchor
    
    Returns:
        (new_content, anchor_match)
    """
    match = resolve_anchor(original_content, anchor)
    
    if position == "after":
        insert_pos = match.position + len(match.matched_text)
    else:  # before
        insert_pos = match.position
    
    # Insert the text
    new_content = (
        original_content[:insert_pos] + 
        insert_text + 
        original_content[insert_pos:]
    )
    
    return new_content, match


def compute_substitute_result(
    original_content: str,
    pattern: str,
    replacement: str,
    all_occurrences: bool = False
) -> Tuple[str, int]:
    """
    Compute the result of a SUBSTITUTE operation.
    
    Args:
        original_content: Original text
        pattern: Text pattern to find and replace
        replacement: Replacement text
        all_occurrences: If True, replace all; if False, replace first only
    
    Returns:
        (new_content, count_of_replacements)
    """
    if all_occurrences:
        new_content = original_content.replace(pattern, replacement)
        count = original_content.count(pattern)
    else:
        new_content = original_content.replace(pattern, replacement, 1)
        count = 1 if pattern in original_content else 0
    
    if count == 0:
        # Pattern not found - this should fail
        raise AnchorNotFoundError(
            anchor=pattern,
            target_id="substitute_operation",
            target_preview=original_content[:500]
        )
    
    return new_content, count


# Example usage and testing
if __name__ == "__main__":
    # Test case from the architecture document
    test_content = """Subject to the provisions of sub-section (12) of section 25, 
    where the application for grant of registration has been approved under rule 9, 
    a certificate of registration in FORM GST REG-06 showing the principal place 
    of business and additional place or places of business shall be made available 
    to the applicant on the common portal."""
    
    # Test 1: Exact match
    try:
        match = resolve_anchor(test_content, "under rule 9,")
        print(f"✓ Exact match found at position {match.position}")
        print(f"  Match type: {match.match_type}")
        print(f"  Needs review: {match.needs_review}")
    except AnchorNotFoundError as e:
        print(f"✗ Failed: {e}")
    
    # Test 2: SPLICE operation
    try:
        new_content, match = compute_splice_result(
            test_content,
            anchor="under rule 9,",
            insert_text=" rule 9A and rule 14A,",
            position="after"
        )
        print(f"\n✓ SPLICE operation successful")
        print(f"  Inserted after position {match.position + len(match.matched_text)}")
        print(f"  Preview: ...{new_content[match.position:match.position+80]}...")
    except AnchorNotFoundError as e:
        print(f"✗ SPLICE failed: {e}")
    
    # Test 3: Missing anchor (should fail loudly)
    try:
        match = resolve_anchor(test_content, "nonexistent anchor text")
        print("✗ Should have failed!")
    except AnchorNotFoundError as e:
        print(f"\n✓ Correctly failed for missing anchor")
        print(f"  Anchor: {e.anchor}")
