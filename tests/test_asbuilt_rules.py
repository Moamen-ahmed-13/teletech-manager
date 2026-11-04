import unittest

from asbuilt_apply import markup_policy, relocation_destination_conflict, relocation_destination_match


class AsBuiltRuleTests(unittest.TestCase):
    def test_red_uses_the_same_delete_and_label_shift_policy_as_pink(self):
        self.assertEqual(markup_policy('red'), markup_policy('pink'))

    def test_yellow_keeps_site_and_reserves_patch_panel_slot(self):
        self.assertEqual(markup_policy('yellow'), {
            'site_action': 'KEEP',
            'patch_panel_action': 'RESERVE_SLOT',
            'label_action': 'KEEP',
        })

    def test_pink_removes_site_and_shifts_label(self):
        self.assertEqual(markup_policy('pink'), {
            'site_action': 'DELETE',
            'patch_panel_action': 'NO_SLOT',
            'label_action': 'SHIFT',
        })

    def test_orange_keeps_patch_panel_slot(self):
        self.assertEqual(markup_policy('orange')['patch_panel_action'], 'KEEP_SLOT')

    def test_move_rejects_same_source_destination(self):
        reason = relocation_destination_conflict([100, 100], [100.1, 100], [], 0, 1.0)
        self.assertIn('no-op', reason)

    def test_move_rejects_collision_with_other_endpoint(self):
        instances = [{'world': [100, 100]}, {'world': [250, 250]}]
        reason = relocation_destination_conflict([100, 100], [250.5, 250], instances, 0, 1.0)
        self.assertIn('overlaps another', reason)

    def test_destination_match_accepts_close_distinct_endpoint(self):
        instances = [{'world': [0, 0]}, {'world': [100, 0]}, {'world': [1000, 0]}]
        result = relocation_destination_match([110, 0], instances, 0, 100)
        self.assertTrue(result['ok'], result)

    def test_destination_match_rejects_far_endpoint(self):
        instances = [{'world': [0, 0]}, {'world': [100, 0]}, {'world': [1000, 0]}]
        result = relocation_destination_match([250, 0], instances, 0, 100)
        self.assertFalse(result['ok'], result)

    def test_destination_match_rejects_ambiguous_neighbours(self):
        instances = [{'world': [0, 0]}, {'world': [100, 0]}, {'world': [120, 0]}]
        result = relocation_destination_match([110, 0], instances, 0, 100)
        self.assertFalse(result['ok'], result)

    def test_move_allows_clear_destination(self):
        instances = [{'world': [100, 100]}, {'world': [250, 250]}]
        reason = relocation_destination_conflict([100, 100], [400, 400], instances, 0, 1.0)
        self.assertEqual(reason, '')


if __name__ == '__main__':
    unittest.main()
