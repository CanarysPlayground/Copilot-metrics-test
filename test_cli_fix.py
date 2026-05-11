#!/usr/bin/env python3
"""Test CLI metrics parsing from totals_by_cli API structure."""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from enterprise_team_copilot_combined_report import aggregate_users, metrics_row_for_user


def test_cli_metrics_from_totals_by_cli():
    """Test that CLI metrics are correctly parsed from the totals_by_cli object."""
    print("=" * 70)
    print("TEST: CLI metrics parsing from totals_by_cli")
    print("=" * 70)

    # Simulated NDJSON rows matching the actual GitHub API schema
    rows = [
        {
            "user_login": "testuser",
            "day": "2026-05-01",
            "used_cli": True,
            "user_initiated_interaction_count": 5,
            "code_generation_activity_count": 3,
            "code_acceptance_activity_count": 2,
            "totals_by_cli": {
                "session_count": 4,
                "request_count": 10,
                "prompt_count": 7,
                "token_usage": {
                    "output_tokens_sum": 5000,
                    "prompt_tokens_sum": 3800,
                    "avg_tokens_per_request": 880.0,
                },
                "last_known_cli_version": {
                    "cli_version": "1.0.8",
                    "sampled_at": "2026-05-01T00:01:43.000Z",
                },
            },
            "totals_by_ide": [
                {
                    "ide": "vscode",
                    "user_initiated_interaction_count": 5,
                    "code_generation_activity_count": 3,
                    "code_acceptance_activity_count": 2,
                    "loc_suggested_to_add_sum": 100,
                    "loc_added_sum": 80,
                }
            ],
            "totals_by_feature": [
                {
                    "feature": "code_completion",
                    "user_initiated_interaction_count": 5,
                    "code_generation_activity_count": 3,
                    "code_acceptance_activity_count": 2,
                    "loc_suggested_to_add_sum": 100,
                    "loc_added_sum": 80,
                    "loc_deleted_sum": 0,
                }
            ],
        },
        {
            "user_login": "testuser",
            "day": "2026-05-02",
            "used_cli": True,
            "user_initiated_interaction_count": 3,
            "code_generation_activity_count": 2,
            "code_acceptance_activity_count": 1,
            "totals_by_cli": {
                "session_count": 2,
                "request_count": 5,
                "prompt_count": 3,
                "token_usage": {
                    "output_tokens_sum": 2500,
                    "prompt_tokens_sum": 1900,
                },
            },
            "totals_by_ide": [
                {
                    "ide": "vscode",
                    "user_initiated_interaction_count": 3,
                    "code_generation_activity_count": 2,
                    "code_acceptance_activity_count": 1,
                    "loc_suggested_to_add_sum": 50,
                    "loc_added_sum": 40,
                }
            ],
            "totals_by_feature": [
                {
                    "feature": "code_completion",
                    "user_initiated_interaction_count": 3,
                    "code_generation_activity_count": 2,
                    "code_acceptance_activity_count": 1,
                    "loc_suggested_to_add_sum": 50,
                    "loc_added_sum": 40,
                    "loc_deleted_sum": 0,
                }
            ],
        },
        # Row without CLI usage
        {
            "user_login": "noncliuser",
            "day": "2026-05-01",
            "used_cli": False,
            "user_initiated_interaction_count": 10,
            "code_generation_activity_count": 8,
            "code_acceptance_activity_count": 6,
            "totals_by_ide": [
                {
                    "ide": "jetbrains",
                    "user_initiated_interaction_count": 10,
                    "code_generation_activity_count": 8,
                    "code_acceptance_activity_count": 6,
                    "loc_suggested_to_add_sum": 200,
                    "loc_added_sum": 150,
                }
            ],
            "totals_by_feature": [
                {
                    "feature": "code_completion",
                    "user_initiated_interaction_count": 10,
                    "code_generation_activity_count": 8,
                    "code_acceptance_activity_count": 6,
                    "loc_suggested_to_add_sum": 200,
                    "loc_added_sum": 150,
                    "loc_deleted_sum": 0,
                }
            ],
        },
    ]

    users = aggregate_users(rows)
    all_pass = True

    # Test CLI user
    agg = users.get("testuser")
    if not agg:
        print("✗ testuser not found in aggregated users")
        return False

    checks = [
        ("cli_sessions", agg.cli_sessions, 6.0),
        ("cli_requests", agg.cli_requests, 15.0),
        ("cli_prompts", agg.cli_prompts, 10.0),
        ("cli_output_tokens", agg.cli_output_tokens, 7500.0),
        ("cli_prompt_tokens", agg.cli_prompt_tokens, 5700.0),
        ("cli_days_active", agg.cli_days_active, 2),
        ("last_known_cli_version", agg.last_known_cli_version, "1.0.8"),
    ]

    for name, actual, expected in checks:
        status = "✓" if actual == expected else "✗"
        if actual != expected:
            all_pass = False
            print(f"{status} {name}: {actual} (expected: {expected}) FAILED")
        else:
            print(f"{status} {name}: {actual}")

    # Test non-CLI user
    non_cli = users.get("noncliuser")
    if not non_cli:
        print("✗ noncliuser not found")
        return False

    non_cli_checks = [
        ("cli_sessions", non_cli.cli_sessions, 0.0),
        ("cli_requests", non_cli.cli_requests, 0.0),
        ("cli_days_active", non_cli.cli_days_active, 0),
    ]

    for name, actual, expected in non_cli_checks:
        status = "✓" if actual == expected else "✗"
        if actual != expected:
            all_pass = False
            print(f"{status} noncliuser.{name}: {actual} (expected: {expected}) FAILED")
        else:
            print(f"{status} noncliuser.{name}: {actual}")

    # Test metrics_row_for_user output
    metrics = metrics_row_for_user(agg)
    metrics_checks = [
        ("metrics_cli_sessions_28d", metrics.get("metrics_cli_sessions_28d"), 6),
        ("metrics_cli_requests_28d", metrics.get("metrics_cli_requests_28d"), 15),
        ("metrics_cli_prompts_28d", metrics.get("metrics_cli_prompts_28d"), 10),
        ("metrics_cli_output_tokens_28d", metrics.get("metrics_cli_output_tokens_28d"), 7500),
        ("metrics_cli_prompt_tokens_28d", metrics.get("metrics_cli_prompt_tokens_28d"), 5700),
        ("metrics_cli_days_active_28d", metrics.get("metrics_cli_days_active_28d"), 2),
        ("metrics_cli_last_version", metrics.get("metrics_cli_last_version"), "1.0.8"),
    ]

    print("\nMetrics dict output:")
    for name, actual, expected in metrics_checks:
        status = "✓" if actual == expected else "✗"
        if actual != expected:
            all_pass = False
            print(f"{status} {name}: {actual} (expected: {expected}) FAILED")
        else:
            print(f"{status} {name}: {actual}")

    # Test IDE tracking still works
    print("\nIDE tracking:")
    ide_checks = [
        ("vscode interactions", agg.editor_interactions.get("vscode", 0), 8.0),
        ("jetbrains interactions (noncliuser)", non_cli.editor_interactions.get("jetbrains", 0), 10.0),
    ]
    for name, actual, expected in ide_checks:
        status = "✓" if actual == expected else "✗"
        if actual != expected:
            all_pass = False
            print(f"{status} {name}: {actual} (expected: {expected}) FAILED")
        else:
            print(f"{status} {name}: {actual}")

    print("=" * 70)
    if all_pass:
        print("✓ ALL TESTS PASSED!")
        print()
        print("The fix correctly parses CLI metrics from totals_by_cli:")
        print("  • session_count, request_count, prompt_count")
        print("  • token_usage (output_tokens_sum, prompt_tokens_sum)")
        print("  • last_known_cli_version")
        print("  • used_cli boolean flag for days active")
        print("  • IDE tracking via totals_by_ide still works correctly")
    else:
        print("✗ SOME TESTS FAILED")
    print("=" * 70)
    return all_pass


if __name__ == "__main__":
    success = test_cli_metrics_from_totals_by_cli()
    sys.exit(0 if success else 1)
