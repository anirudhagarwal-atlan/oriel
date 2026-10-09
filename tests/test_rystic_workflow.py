import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from contextbase.context import approve_metric, export_bundle
from contextbase.rystic import load_example, snapshot_info
from contextbase.rystic_workflow import METRIC_NAME, preview, propose_context, report
from contextbase.workspace import Workspace


class RysticWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / 'data')
        self.receipt = load_example(self.workspace)

    def approve(self):
        proposal = propose_context(self.workspace)['proposal']
        approve_metric(self.workspace, METRIC_NAME, proposal['approval_sha256'], 'synthetic_test_reviewer')
        return proposal

    def test_captured_fixture_provisional_answer_and_freshness(self):
        answer = preview(self.workspace)
        self.assertEqual(answer['status'], 'provisional')
        self.assertEqual(answer['net_collected_usd'], '1857.69')
        self.assertEqual([row['account_id'] for row in answer['accounts']],
                         ['customer_001', 'customer_002', 'customer_008', 'customer_010', 'customer_012'])
        self.assertEqual(self.receipt['rows'], {'stripe.customers': 12, 'stripe.charges': 24, 'linear.issues': 17})
        self.assertEqual(answer['snapshot']['refresh_mode'], 'bundled_capture')
        self.assertFalse((self.workspace.root / 'context' / 'approved.json').exists())
        with self.assertRaises(ValueError):
            report(self.workspace)
        # File imports never gain implicit model consent just from a synthetic flag.
        self.assertFalse(self.workspace.is_synthetic())

    def test_approval_export_and_separate_consumer(self):
        proposal = self.approve()
        self.assertTrue(proposal['synthetic'])
        result = report(self.workspace)
        self.assertEqual(result['metric']['rows'], [{METRIC_NAME: '1857.69'}])
        self.assertEqual(result['metric']['snapshot']['snapshot_id'], self.receipt['snapshot_id'])
        destination = self.root / 'bundle'
        manifest = export_bundle(self.workspace, destination)
        self.assertTrue(manifest['synthetic'])
        for entry in manifest['files']:
            self.assertEqual(hashlib.sha256((destination / entry['path']).read_bytes()).hexdigest(), entry['sha256'])
        payload = json.loads((destination / 'metrics' / (METRIC_NAME + '.json')).read_text())
        self.assertTrue(payload['source_metadata']['synthetic'])
        self.assertEqual(payload['definition']['status'], 'candidate')
        code = '''import json,sys
from pathlib import Path
from contextbase.workspace import Workspace
from contextbase.rystic import load_example
root=Path(sys.argv[1]); bundle=Path(sys.argv[2])
w=Workspace(root); load_example(w)
context=json.loads((bundle/'context.json').read_text())
metric=context['metrics'][0]
result=w.query(metric['sql'])
print(json.dumps({'context_version':context['context_version'],'rows':result['rows']}))
'''
        process = subprocess.run([sys.executable, '-c', code, str(self.root / 'second-consumer'), str(destination)],
                                 capture_output=True, text=True, check=True)
        reused = json.loads(process.stdout)
        self.assertEqual(reused['context_version'], result['metric']['context_version'])
        self.assertEqual(reused['rows'], result['metric']['rows'])

    def test_approval_rejects_changed_evidence(self):
        proposal = propose_context(self.workspace)['proposal']
        with self.workspace._connect(writable=True) as connection:
            connection.execute("UPDATE stripe.charges SET amount_cents=amount_cents+100 WHERE customer_id=(SELECT customer_id FROM stripe.customers WHERE account_id='customer_001')")
        with self.assertRaisesRegex(ValueError, 'witnessed metric result changed'):
            approve_metric(self.workspace, METRIC_NAME, proposal['approval_sha256'], 'synthetic_test_reviewer')

    def test_report_requires_account_details_to_match_metric_definition(self):
        self.approve()
        path = self.workspace.root / 'context' / 'approved.json'
        context = json.loads(path.read_text())
        context['metrics'][0]['sql'] = 'SELECT 1 AS net_exposed_collected_usd'
        path.write_text(json.dumps(context))
        with self.assertRaisesRegex(ValueError, 'matching account report'):
            report(self.workspace)

    def test_repeat_capture_preserves_data_identity(self):
        next_receipt = load_example(self.workspace)
        self.assertFalse(next_receipt['data_changed'])
        self.assertEqual(next_receipt['snapshot_id'], self.receipt['snapshot_id'])
        self.assertEqual(snapshot_info(self.workspace)['refresh_mode'], 'bundled_capture')


if __name__ == '__main__':
    unittest.main()
