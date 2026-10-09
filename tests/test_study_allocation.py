import tempfile
from pathlib import Path
import unittest

from contextbase.study_allocation import allocate_study


class StudyAllocationTests(unittest.TestCase):
    def test_disjoint_reproducible_balanced_allocation_never_authorizes_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            options = dict(master_seed=91, development_seeds=[7, 31], repeats=3)
            a = allocate_study(Path(directory) / 'one.json', **options)
            b = allocate_study(Path(directory) / 'two.json', **options)
            self.assertEqual(a['fixture_batches'], b['fixture_batches'])
            self.assertEqual(a['schedule'], b['schedule'])
            seeds = {row['seed'] for row in a['fixture_batches']}
            self.assertEqual(len(seeds), 30)
            self.assertFalse(seeds & {7, 31})
            self.assertEqual(a['planned_trials'], 720)
            self.assertEqual(len({tuple(row.items()) for row in a['schedule']}), 720)
            for index in range(0, len(a['schedule']), 2):
                left, right = a['schedule'][index:index+2]
                self.assertEqual({**left, 'arm': None}, {**right, 'arm': None})
                self.assertNotEqual(left['arm'], right['arm'])
            self.assertFalse(a['execution_authorized'])
            self.assertFalse(a['release_claim_eligible'])
            self.assertIsNone(a['model_budget_usd'])
            self.assertTrue(all(row['fixture_sha256'] is None for row in a['fixture_batches']))
            with self.assertRaises(FileExistsError): allocate_study(Path(directory) / 'one.json', **options)

    def test_invalid_input_rejected_before_writing(self):
        for change in ({'master_seed': True}, {'batches': 2}, {'repeats': 0},
                       {'development_seeds': []}, {'development_seeds': [7, 7]},
                       {'account_counts': [12, 12]}, {'account_counts': [101]}, {'workload': []}, {'workload': 'invalid'}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                args = {'master_seed': 1, 'development_seeds': [7], **change}
                path = Path(directory) / 'plan.json'
                with self.assertRaises(ValueError): allocate_study(path, **args)
                self.assertFalse(path.exists())

    def test_recurring_allocation_orders_phases_and_counterbalances_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = allocate_study(Path(directory) / 'recurring.json', master_seed=7,
                                  development_seeds=[7, 31], workload='recurring_v1')
        self.assertEqual(plan['planned_trials'], 2160)
        self.assertEqual(len(plan['tasks']), 12)
        phases = plan['recurring_profile']['phase_order']
        task_phases = {task['id']: task['phase_id'] for task in plan['tasks']}
        for batch in plan['fixture_batches']:
            schedule = [row for row in plan['schedule'] if row['batch_id'] == batch['batch_id']]
            positions = [phases.index(task_phases[row['task_id']]) for row in schedule]
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(len(schedule), 72)
            for index in range(0, len(schedule), 2):
                first, second = schedule[index:index+2]
                self.assertEqual({**first, 'arm': None}, {**second, 'arm': None})
                self.assertNotEqual(first['arm'], second['arm'])
            self.assertTrue(all(value is None for value in batch['phase_snapshots'].values()))
            self.assertTrue(all(value is None for value in batch['task_artifacts'].values()))
        self.assertFalse(plan['execution_authorized'])
