import os
import unittest

os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("ENTERPRISE_SLUG", "test-enterprise")

from enterprise_team_copilot_combined_report import aggregate_users, metrics_row_for_user


class InteractionAggregationTests(unittest.TestCase):
    def test_uses_feature_interactions_when_top_level_is_zero(self):
        users = aggregate_users([
            {
                "user_login": "octocat",
                "user_initiated_interaction_count": 0,
                "totals_by_feature": [
                    {"feature": "chat_panel_ask_mode", "user_initiated_interaction_count": 4},
                    {"feature": "chat_panel_agent_mode", "user_initiated_interaction_count": 3},
                ],
            }
        ])

        self.assertEqual(metrics_row_for_user(users["octocat"])["metrics_interactions_28d"], 7)

    def test_uses_total_chats_from_feature_breakdown(self):
        users = aggregate_users([
            {
                "user_login": "octocat",
                "user_initiated_interaction_count": 0,
                "totals_by_feature": [
                    {"feature": "chat_panel_ask_mode", "total_chats": 9},
                ],
            }
        ])

        self.assertEqual(metrics_row_for_user(users["octocat"])["metrics_interactions_28d"], 9)

    def test_does_not_double_count_when_top_level_is_nonzero(self):
        users = aggregate_users([
            {
                "user_login": "octocat",
                "user_initiated_interaction_count": 5,
                "totals_by_feature": [
                    {"feature": "chat_panel_ask_mode", "user_initiated_interaction_count": 5},
                ],
            }
        ])

        self.assertEqual(metrics_row_for_user(users["octocat"])["metrics_interactions_28d"], 5)

    def test_uses_model_interactions_when_feature_counts_are_missing(self):
        users = aggregate_users([
            {
                "user_login": "octocat",
                "totals_by_model_feature": [
                    {"model": "gpt-4.1", "user_initiated_interaction_count": 2},
                    {"model": "claude-sonnet-4", "user_initiated_interaction_count": 6},
                ],
            }
        ])

        self.assertEqual(metrics_row_for_user(users["octocat"])["metrics_interactions_28d"], 8)

    def test_uses_nested_chat_totals_from_current_metrics_shape(self):
        users = aggregate_users([
            {
                "user_login": "octocat",
                "copilot_ide_chat": {
                    "editors": [
                        {
                            "name": "vscode",
                            "models": [
                                {"name": "default", "total_chats": 4},
                                {"name": "claude-sonnet-4", "total_chats": 3},
                            ],
                        }
                    ]
                },
                "copilot_dotcom_chat": {
                    "models": [
                        {"name": "default", "total_chats": 5},
                    ]
                },
            }
        ])

        self.assertEqual(metrics_row_for_user(users["octocat"])["metrics_interactions_28d"], 12)


if __name__ == "__main__":
    unittest.main()
