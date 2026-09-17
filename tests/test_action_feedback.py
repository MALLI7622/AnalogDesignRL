"""Action validity remains distinct from circuit measurement validity."""
import json
import unittest

from training.client import observation_text, system_prompt
from training.worker import public_action_feedback


class ActionFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.task = {'initial_parameters': {'C': 2., 'M': 3},
                     'parameters': {'C': {'min': 1., 'max': 4.},
                                    'M': {'min': 1, 'max': 8, 'integer': True}}}
        self.before = dict(self.task['initial_parameters'])

    def test_valid_edit_with_failed_ac_is_visible_to_learner(self):
        result = {'status': 'failed', 'parameters': {'C': 3., 'M': 3},
                  'error': 'Expected positive low-frequency gain and non-inverting phase.'}
        feedback = public_action_feedback(result, self.task, self.before, {'C': 3.})
        rendered = json.loads(observation_text({**result, **feedback}))
        self.assertTrue(rendered['action_valid'])
        self.assertEqual(rendered['parameters_changed'], ['C'])
        self.assertFalse(rendered['measurement_success'])
        self.assertEqual(rendered['failure_category'], 'ac_measurement_failed')
        self.assertFalse(public_action_feedback(result, self.task, result['parameters'], {'C': 3.})['parameters_changed'])

    def test_invalid_values_and_unknown_keys_are_distinct_from_measurement_failure(self):
        result = {'status': 'failed', 'parameters': self.before, 'error': 'private server detail'}
        for action in ({'C': 5.}, {'M': 2.5}, {'secret/path': 2.}, 'invalid JSON', {'C': True}):
            with self.subTest(action=action):
                feedback = public_action_feedback(result, self.task, self.before, action)
                self.assertFalse(feedback['action_valid'])
                self.assertEqual(feedback['failure_category'], 'invalid_action')
                self.assertEqual(feedback['parameters_changed'], [])
                self.assertNotIn('private', json.dumps(feedback))
                self.assertNotIn('secret/path', json.dumps(feedback))

    def test_unknown_server_diagnostics_never_pass_through(self):
        result = {'status': 'failed', 'parameters': self.before,
                  'error': '/private/reference/path: hidden reference values'}
        feedback = public_action_feedback(result, self.task, self.before, {})
        self.assertTrue(feedback['action_valid'])
        self.assertEqual(feedback['failure_category'], 'evaluation_failed')
        self.assertNotIn('private', json.dumps(feedback))
        success = public_action_feedback({**result, 'status': 'ok'}, self.task, self.before, {})
        self.assertTrue(success['measurement_success'])
        self.assertIsNone(success['failure_category'])
        self.assertNotIn('error', success)

    def test_recovery_instruction_is_present(self):
        prompt = system_prompt('single_change_recovery_v1')
        self.assertIn('Start with a modest change', prompt)
        self.assertIn('try a different candidate', prompt)


if __name__ == '__main__':
    unittest.main()
