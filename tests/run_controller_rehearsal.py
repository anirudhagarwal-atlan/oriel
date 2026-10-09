"""Manual offline controller rehearsal with full validation and synthetic evidence.

Run with the repository's Python environment: python tests/run_controller_rehearsal.py.
This uses in-memory source doubles and deliberately returns private authored gold.
It tests execution and accounting mechanics, not model performance or acceptance.
Native requests and real provider constructors are blocked. Outputs are retained
in the printed temporary directory for inspection; no automatic resume or retry.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from contextbase.acceptance_protocol import freeze_protocol
from contextbase.context import load_context
from contextbase.study_binding import _hash
from contextbase.study_controller import run_study
from contextbase.workspace import Workspace
from test_acceptance_protocol import protocol_inputs
from test_rystic_benchmark import make_fixture
from test_study_controller import NoTools
from test_study_costs import cost_fixture
from test_study_transitions import MutationPair


def now():
    return datetime.now(timezone.utc).isoformat()


def history_event(kind, start, end):
    span = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    return {'event_id': 'synthetic-' + kind, 'kind': kind, 'batch_id': None, 'phase_id': None,
            'started_at': start, 'finished_at': end, 'elapsed_seconds': span,
            'cost_usd': .01, 'allocation': {'source': .5, 'workspace': .5},
            'evidence_sha256': _hash({'synthetic_context_work': kind, 'start': start, 'end': end}),
            'duration_role': 'exclusive_overhead', 'cost_role': 'incremental', 'attempt_id': None}


class AuthoredGold:
    def __init__(self, protocol, output):
        self.protocol, self.output, self.calls = protocol, output, 0
        self.tasks = {row['id']: row for row in protocol['tasks']}
        self.cache = {}
        self.timeout_seconds = 90

    def complete(self, system, messages, max_cost_usd):
        # Deliberately read evaluator gold. This checks mechanics, never model quality.
        row = self.protocol['planned'][self.calls]
        phase = self.tasks[row['task_id']]['phase_id']
        key = row['batch_id'], phase
        if key not in self.cache:
            path = self.output / 'workspaces' / row['batch_id'] / (phase + '-binding.json')
            binding = json.loads(path.read_text())
            self.cache[key] = {private['id']: (public, private) for public, private
                               in zip(binding['public_tasks'], binding['evaluator_tasks'])}
        public, private = self.cache[key][row['task_id']]
        assert messages[0]['content'] == public['question']
        self.calls += 1
        if self.calls % 120 == 0:
            print(json.dumps({'synthetic_scripted_calls': self.calls}), flush=True)
        return {'response': json.dumps({'answer': private['expected']}), 'cost_usd': .001,
                'model_usage': {'synthetic-test-model': {}}, 'is_error': False}


def main():
    started = time.perf_counter()
    directory = Path(tempfile.mkdtemp(prefix='contextbase-offline-controller-'))
    output = directory / 'execution'
    print(json.dumps({'synthetic_test': True, 'output': str(directory), 'protocol_validation_stubs': 0}), flush=True)
    with patch('contextbase.rystic.RysticClient.request', side_effect=AssertionError('No native or network calls')), \
         patch('contextbase.model.ClaudeCLI', side_effect=AssertionError('No real model')), \
         patch('contextbase.study_controller.ClaudeCLI', side_effect=AssertionError('No real model')):
        allocation, intents, settings = protocol_inputs()
        creation_start = now()
        authored = Workspace(directory / 'authored-policy')
        make_fixture(authored)
        approved = load_context(authored)
        creation_end = now()
        review_start = now()
        assert load_context(authored) == approved
        context_digest = _hash(approved)
        review_end = now()
        history = [history_event('context_creation', creation_start, creation_end),
                   history_event('context_review', review_start, review_end)]
        _, contract = cost_fixture()
        contract['assignments'] = {}
        pairs = {}
        for index, intent in enumerate(intents):
            pair = MutationPair(intent['phase_plan'])
            pair.stripe.url = 'http://127.0.0.1:' + str(21000 + index * 2)
            pair.linear.url = 'http://127.0.0.1:' + str(21001 + index * 2)
            pairs[(pair.stripe.url, pair.linear.url)] = pair
            contract['assignments'][intent['batch_id']] = {
                'environment_id': intent['provisioning']['environment_id'],
                'stripe_url': pair.stripe.url, 'linear_url': pair.linear.url}
        for rule in contract['rules'].values():
            rule.update(method='declared_zero', rate=0, max_seconds=600)
        for kind, rate in {'workspace_ingestion': .01, 'refresh': .01, 'workflow_configuration': .005,
                           'phase_transitions': .005, 'evaluator_validation': .001, 'runtime_license': .05}.items():
            contract['rules'][kind].update(method='fixed_per_operation', rate=rate)
        contract['rules']['runtime_license']['max_seconds'] = settings['budgets']['study_seconds']
        settings['context_sha256'] = context_digest
        settings['accounting_policy']['evidence_sha256'] = _hash(contract)
        protocol = freeze_protocol(allocation, intents, settings, directory / 'protocol.json',
                                   historical_context_events=history)
        (directory / 'cost-contract.json').write_text(json.dumps(contract, indent=2))
        print(json.dumps({'phase': 'frozen', 'batches': len(protocol['batches']),
                          'planned_scripted_trials': len(protocol['planned']),
                          'preparation_seconds': round(time.perf_counter() - started, 3)}), flush=True)
        model = AuthoredGold(protocol, output)
        result = run_study(protocol, contract, approved, output,
            approve=protocol['protocol_sha256'], allow_model_data=True, allow_source_transitions=True,
            client=model, pair_factory=lambda stripe, linear: pairs[(stripe, linear)],
            tools_factory=lambda workspace, pair, arm: NoTools())
        report = json.loads((output / 'report.json').read_text())
        assert result['outcome'] == 'completed', result
        assert result['completed_phases'] == result['recorded_phases'] == 120, result
        assert result['unexecuted_trials'] == 0 and model.calls == 720, result
        assert result['budget']['model_calls'] == 720, result
        assert not result['budget']['pending'] and not result['budget']['blocked_reasons'], result
        assert set(result['reasons']) == {'Synthetic source fixtures cannot support observed acceptance results',
                                          'Injected model responses are test evidence only',
                                          'Full billing reconciliation is required before charter readiness'}, result
        assert result['statistical_acceptance'] == 'inconclusive' and report['metrics'] is None, report
        assert not result['claim_eligible'] and not result['automatic_resume_allowed'], result
        assert abs(report['known_total_study_spend_usd'] - result['budget']['known_total_cost_usd']) < 1e-9, report
        writes = sum(len(pair.writes) for pair in pairs.values())
        expected_writes = sum(len(transition['changes']) for intent in intents for transition in intent['phase_plan']['transitions'])
        assert writes == expected_writes
        summary = {'synthetic_test': True, 'protocol_validation_stubs': 0,
                   'batches': 30, 'completed_phases': 120, 'scripted_answers': model.calls,
                   'simulated_source_writes': writes, 'outcome': result['outcome'],
                   'statistical_acceptance': result['statistical_acceptance'], 'claim_eligible': False,
                   'observed_model_performance_evidence': False, 'reasons': result['reasons'],
                   'elapsed_test_seconds': round(time.perf_counter() - started, 3),
                   'artifact_bytes': sum(path.stat().st_size for path in directory.rglob('*') if path.is_file()),
                   'output': str(directory)}
        (directory / 'smoke-summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
