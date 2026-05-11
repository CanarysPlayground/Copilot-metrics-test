#!/usr/bin/env python
"""
Test script to verify CLI metrics extraction logic.
This test simulates the API response structure and validates that CLI metrics
are correctly extracted and aggregated.
"""

import json
from dataclasses import asdict
from typing import Dict, Any, List

# Import the necessary functions from the main script
# We'll use a simplified version for testing purposes

def to_num(v):
    """Convert value to float"""
    try:
        if v is None:
            return 0.0
        if isinstance(v, bool):
            return float(int(v))
        return float(v)
    except Exception:
        return 0.0

def get_loc_field_value(row: Dict[str, Any], new_field: str, old_field: str) -> float:
    """Get LOC field value with fallback"""
    if new_field in row:
        return to_num(row.get(new_field))
    return to_num(row.get(old_field))

def test_cli_metrics_extraction():
    """Test CLI metrics extraction from API response"""
    
    # Simulate API response with CLI and IDE usage
    sample_rows = [
        {
            "user_login": "alice",
            "day": "2026-05-10",
            "totals_by_editor": [
                {
                    "editor": "cli",
                    "user_initiated_interaction_count": 15,
                    "code_generation_activity_count": 42,
                    "code_acceptance_activity_count": 38,
                    "loc_suggested_to_add_sum": 250,
                    "loc_added_sum": 230
                },
                {
                    "editor": "vscode",
                    "user_initiated_interaction_count": 50,
                    "code_generation_activity_count": 120,
                    "code_acceptance_activity_count": 100,
                    "loc_suggested_to_add_sum": 800,
                    "loc_added_sum": 700
                }
            ]
        },
        {
            "user_login": "alice",
            "day": "2026-05-11",
            "totals_by_editor": [
                {
                    "editor": "CLI",  # Test case-insensitive
                    "user_initiated_interaction_count": 10,
                    "code_generation_activity_count": 30,
                    "code_acceptance_activity_count": 25,
                    "loc_suggested_to_add_sum": 180,
                    "loc_added_sum": 150
                }
            ]
        },
        {
            "user_login": "bob",
            "day": "2026-05-10",
            "editor": "vscode",  # Flat format
            "user_initiated_interaction_count": 20,
            "code_generation_activity_count": 60,
            "code_acceptance_activity_count": 50,
            "loc_suggested_to_add_sum": 400,
            "loc_added_sum": 350
        }
    ]
    
    # Simulate aggregation
    users = {}
    
    for r in sample_rows:
        login = r.get("user_login", "")
        if not login:
            continue
        
        if login not in users:
            users[login] = {
                "editor_interactions": {},
                "editor_completions": {},
                "editor_acceptances": {},
                "editor_loc_suggested": {},
                "editor_loc_added": {},
                "editor_counts": {}
            }
        
        agg = users[login]
        
        # Process nested format
        tbe = r.get("totals_by_editor")
        if isinstance(tbe, list):
            for e in tbe:
                if not isinstance(e, dict):
                    continue
                editor = (e.get("editor") or "unknown").lower().strip()
                if not editor or editor == "unknown":
                    continue
                
                interaction_count = to_num(e.get("user_initiated_interaction_count"))
                completions_val = to_num(e.get("code_generation_activity_count"))
                acceptances_val = to_num(e.get("code_acceptance_activity_count"))
                editor_loc_sug = get_loc_field_value(e, "loc_suggested_to_add_sum", "loc_suggested")
                editor_loc_add = get_loc_field_value(e, "loc_added_sum", "loc_added")
                
                agg["editor_interactions"][editor] = agg["editor_interactions"].get(editor, 0.0) + interaction_count
                agg["editor_completions"][editor] = agg["editor_completions"].get(editor, 0.0) + completions_val
                agg["editor_acceptances"][editor] = agg["editor_acceptances"].get(editor, 0.0) + acceptances_val
                agg["editor_loc_suggested"][editor] = agg["editor_loc_suggested"].get(editor, 0.0) + editor_loc_sug
                agg["editor_loc_added"][editor] = agg["editor_loc_added"].get(editor, 0.0) + editor_loc_add
                agg["editor_counts"][editor] = agg["editor_counts"].get(editor, 0.0) + 1.0
        else:
            # Flat format
            editor = r.get("editor") or r.get("client") or r.get("ide")
            if isinstance(editor, str) and editor:
                editor = editor.lower().strip()
                if editor and editor != "unknown":
                    interaction_count = to_num(r.get("user_initiated_interaction_count"))
                    completions_val = to_num(r.get("code_generation_activity_count"))
                    acceptances_val = to_num(r.get("code_acceptance_activity_count"))
                    editor_loc_sug = get_loc_field_value(r, "loc_suggested_to_add_sum", "loc_suggested")
                    editor_loc_add = get_loc_field_value(r, "loc_added_sum", "loc_added")
                    
                    agg["editor_interactions"][editor] = agg["editor_interactions"].get(editor, 0.0) + interaction_count
                    agg["editor_completions"][editor] = agg["editor_completions"].get(editor, 0.0) + completions_val
                    agg["editor_acceptances"][editor] = agg["editor_acceptances"].get(editor, 0.0) + acceptances_val
                    agg["editor_loc_suggested"][editor] = agg["editor_loc_suggested"].get(editor, 0.0) + editor_loc_sug
                    agg["editor_loc_added"][editor] = agg["editor_loc_added"].get(editor, 0.0) + editor_loc_add
                    agg["editor_counts"][editor] = agg["editor_counts"].get(editor, 0.0) + 1.0
    
    # Validate results
    print("=== CLI Metrics Test Results ===\n")
    
    # Alice should have CLI metrics from two days
    alice = users.get("alice", {})
    alice_cli_interactions = alice.get("editor_interactions", {}).get("cli", 0.0)
    alice_cli_completions = alice.get("editor_completions", {}).get("cli", 0.0)
    alice_cli_acceptances = alice.get("editor_acceptances", {}).get("cli", 0.0)
    alice_cli_loc_suggested = alice.get("editor_loc_suggested", {}).get("cli", 0.0)
    alice_cli_loc_added = alice.get("editor_loc_added", {}).get("cli", 0.0)
    
    print(f"Alice CLI Metrics:")
    print(f"  Interactions: {alice_cli_interactions} (expected: 25)")
    print(f"  Completions: {alice_cli_completions} (expected: 72)")
    print(f"  Acceptances: {alice_cli_acceptances} (expected: 63)")
    print(f"  LOC Suggested: {alice_cli_loc_suggested} (expected: 430)")
    print(f"  LOC Added: {alice_cli_loc_added} (expected: 380)")
    
    # Bob should have no CLI metrics
    bob = users.get("bob", {})
    bob_cli_interactions = bob.get("editor_interactions", {}).get("cli", 0.0)
    
    print(f"\nBob CLI Metrics:")
    print(f"  Interactions: {bob_cli_interactions} (expected: 0)")
    print(f"  Editors used: {', '.join(bob.get('editor_counts', {}).keys())} (expected: vscode)")
    
    # Validate
    tests_passed = 0
    tests_failed = 0
    
    if alice_cli_interactions == 25.0:
        print("\n✓ Alice CLI interactions correct")
        tests_passed += 1
    else:
        print(f"\n✗ Alice CLI interactions incorrect: {alice_cli_interactions} != 25")
        tests_failed += 1
    
    if alice_cli_completions == 72.0:
        print("✓ Alice CLI completions correct")
        tests_passed += 1
    else:
        print(f"✗ Alice CLI completions incorrect: {alice_cli_completions} != 72")
        tests_failed += 1
    
    if alice_cli_acceptances == 63.0:
        print("✓ Alice CLI acceptances correct")
        tests_passed += 1
    else:
        print(f"✗ Alice CLI acceptances incorrect: {alice_cli_acceptances} != 63")
        tests_failed += 1
    
    if alice_cli_loc_suggested == 430.0:
        print("✓ Alice CLI LOC suggested correct")
        tests_passed += 1
    else:
        print(f"✗ Alice CLI LOC suggested incorrect: {alice_cli_loc_suggested} != 430")
        tests_failed += 1
    
    if alice_cli_loc_added == 380.0:
        print("✓ Alice CLI LOC added correct")
        tests_passed += 1
    else:
        print(f"✗ Alice CLI LOC added incorrect: {alice_cli_loc_added} != 380")
        tests_failed += 1
    
    if bob_cli_interactions == 0.0:
        print("✓ Bob has no CLI metrics (correct)")
        tests_passed += 1
    else:
        print(f"✗ Bob should have no CLI metrics: {bob_cli_interactions} != 0")
        tests_failed += 1
    
    if "vscode" in bob.get("editor_counts", {}):
        print("✓ Bob has VSCode metrics (correct)")
        tests_passed += 1
    else:
        print("✗ Bob should have VSCode metrics")
        tests_failed += 1
    
    print(f"\n{'='*40}")
    print(f"Tests passed: {tests_passed}/7")
    print(f"Tests failed: {tests_failed}/7")
    print(f"{'='*40}")
    
    if tests_failed == 0:
        print("\n✓ All CLI metrics tests passed!")
        return True
    else:
        print(f"\n✗ {tests_failed} test(s) failed")
        return False

if __name__ == "__main__":
    success = test_cli_metrics_extraction()
    exit(0 if success else 1)
