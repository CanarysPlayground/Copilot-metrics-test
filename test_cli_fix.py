#!/usr/bin/env python3
"""Test CLI metrics detection fix without requiring environment variables."""

def normalize_editor_name(editor_value: str) -> str:
    """Normalize editor name for consistent tracking, especially CLI variations."""
    normalized = editor_value.lower().strip()
    
    # Match CLI-specific patterns to avoid false positives like "eclipse"
    # Check for exact match or CLI as a word/component (preceded/followed by delimiter)
    if normalized == "cli" or normalized.startswith("cli-") or normalized.startswith("cli_") or \
       normalized.endswith("-cli") or normalized.endswith("_cli") or \
       "-cli-" in normalized or "_cli_" in normalized or \
       "gh-cli" in normalized or "copilot_cli" in normalized or "github-cli" in normalized:
        return "cli"
    
    return normalized


def test_normalize_editor_name():
    """Test the normalize_editor_name function."""
    print("="*70)
    print("TEST: Testing normalize_editor_name function")
    print("="*70)
    
    test_cases = [
        ("cli", "cli"),
        ("CLI", "cli"),
        ("Cli", "cli"),
        (" cli ", "cli"),
        ("gh-cli", "cli"),
        ("github-cli", "cli"),
        ("copilot_cli", "cli"),
        ("gh_cli", "cli"),
        ("GH-CLI", "cli"),
        ("cli-tool", "cli"),
        ("tool-cli", "cli"),
        ("vscode", "vscode"),
        ("VSCode", "vscode"),
        ("jetbrains", "jetbrains"),
        ("neovim", "neovim"),
        ("eclipse", "eclipse"),  # Should NOT match as CLI
        ("", ""),
        ("unknown", "unknown"),
    ]
    
    all_pass = True
    for input_val, expected in test_cases:
        result = normalize_editor_name(input_val)
        status = "✓" if result == expected else "✗"
        if result != expected:
            all_pass = False
            print(f"{status} {input_val!r:20s} -> {result!r:15s} (expected: {expected!r}) FAILED")
        else:
            print(f"{status} {input_val!r:20s} -> {result!r:15s}")
    
    print("="*70)
    if all_pass:
        print("✓ ALL TESTS PASSED!")
        print()
        print("The fix successfully handles:")
        print("  • Standard 'cli' editor value (case-insensitive)")
        print("  • 'gh-cli' variation (GitHub CLI)")
        print("  • 'copilot_cli' variation")
        print("  • 'github-cli' and other CLI variations")
        print("  • Non-CLI editors (vscode, jetbrains, etc.) are correctly preserved")
    else:
        print("✗ SOME TESTS FAILED")
        return False
    
    print("="*70)
    return True


if __name__ == "__main__":
    import sys
    success = test_normalize_editor_name()
    sys.exit(0 if success else 1)
