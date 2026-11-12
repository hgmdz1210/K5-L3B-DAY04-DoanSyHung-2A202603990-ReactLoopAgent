"""Tests offline cho phần bonus trong agent.py: retry 429, Retry-After, pagination, context."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'evals'))
import agent
from agent import run_agent
from bonus.tools import BonusTools
from scenarios import ScenarioTools
from status_tools import StatusTools
from test_lab import FakeProvider, tool_call, final


def tool_events(result, name):
    return [t for t in result['trace'] if t['event'] == 'tool' and t['name'] == name]


class RepeatProvider:
    """Gọi cùng một tool n lần rồi trả lời; ghi lại toàn bộ history."""
    def __init__(self, name, args, times, answer='Xong'):
        self.name, self.args, self.times, self.answer = name, args, times, answer
        self.n, self.histories = 0, []

    def complete(self, messages, schemas):
        self.histories.append(list(messages))
        self.n += 1
        if self.n <= self.times:
            return tool_call(f'c{self.n}', self.name, json.dumps(self.args))
        return final(self.answer)


class RetryTests(unittest.TestCase):
    def test_persistent_429_stops_after_three_calls(self):
        p = RepeatProvider('get_vercel_status', {}, times=5)
        result = run_agent('Kiểm tra Vercel', p, BonusTools(scenario='rate_limit_always'), max_steps=8)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(tool_events(result, 'get_vercel_status')), 3)
        skipped = [t for t in result['trace'] if t['event'] == 'tool_skipped']
        self.assertEqual(len(skipped), 2)
        last = json.loads(p.histories[-1][-1]['content'])
        self.assertEqual(last['error'], 'RETRY_LIMIT_REACHED')
        self.assertEqual(p.histories[-1][-1]['tool_call_id'], 'c5')

    def test_transient_429_then_success(self):
        p = RepeatProvider('get_vercel_status', {}, times=2)
        result = run_agent('Kiểm tra Vercel', p, BonusTools(scenario='rate_limit_once'), max_steps=5)
        first = json.loads(p.histories[1][-1]['content'])
        self.assertEqual((first['error'], first['attempt'], first['retries_left']), ('RATE_LIMITED', 1, 2))
        self.assertIn('không phải outage', first['note'])
        self.assertEqual(json.loads(p.histories[2][-1]['content'])['provider'], 'vercel')
        self.assertEqual(len(tool_events(result, 'get_vercel_status')), 2)

    def test_retry_after_short_waits_long_stops(self):
        class Limited:
            schemas, system_prompt = [], 'bonus'
            def __init__(self, retry_after):
                self.retry_after = retry_after
            def execute(self, name, args):
                return {'ok': False, 'error': 'RATE_LIMITED', 'http_status': 429, 'retry_after': self.retry_after}
        waits = []
        with patch.object(agent, '_sleep', side_effect=waits.append):
            run_agent('x', RepeatProvider('get_npm_status', {}, times=1), Limited('2'), max_steps=3)
            self.assertEqual(waits, [2.0])
            p = RepeatProvider('get_npm_status', {}, times=2)
            result = run_agent('x', p, Limited('60'), max_steps=4)
        self.assertEqual(waits, [2.0])  # 60 giây vượt budget: không chờ
        with patch.object(agent, '_sleep', side_effect=waits.append):
            run_agent('x', RepeatProvider('get_npm_status', {}, times=2), Limited(None), max_steps=4)
        self.assertEqual(waits, [2.0, 1.0, 2.0])  # 429 thật không có header: backoff 1s, 2s
        self.assertEqual(json.loads(p.histories[1][-1]['content'])['retries_left'], 0)
        self.assertEqual(len(tool_events(result, 'get_npm_status')), 1)
        self.assertTrue(any(t['event'] == 'tool_skipped' for t in result['trace']))

    def test_other_tools_unaffected_by_vercel_limit(self):
        p = FakeProvider([tool_call('a', 'get_vercel_status', '{}'), tool_call('b', 'get_vercel_status', '{}'),
                          tool_call('c', 'get_vercel_status', '{}'), tool_call('d', 'get_github_status', '{}'),
                          final('GitHub ok, Vercel chưa xác minh được')])
        result = run_agent('x', p, BonusTools(scenario='rate_limit_always'), max_steps=6)
        self.assertEqual(json.loads(p.histories[4][-1]['content'])['provider'], 'github')
        self.assertEqual(result['status'], 'completed')


class ProviderRetryTests(unittest.TestCase):
    class Flaky:
        def __init__(self, errors):
            self.errors, self.calls = list(errors), 0
        def complete(self, messages, schemas):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
            return final('ok')

    def test_transient_timeout_is_retried(self):
        p = self.Flaky([TimeoutError('The read operation timed out')])
        with patch.object(agent, '_sleep') as sleep:
            result = run_agent('x', p, StatusTools())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(p.calls, 2)
        sleep.assert_called_once_with(2.0)
        self.assertTrue(any(t['event'] == 'provider_retry' for t in result['trace']))

    def test_persistent_timeout_ends_with_error(self):
        p = self.Flaky([TimeoutError('t')] * 5)
        with patch.object(agent, '_sleep'):
            result = run_agent('x', p, StatusTools())
        self.assertEqual(result['status'], 'error')
        self.assertEqual(p.calls, agent.PROVIDER_RETRIES + 1)

    def test_non_transient_error_is_not_retried(self):
        for exc in (RuntimeError('Provider trả HTTP 401; kiểm tra model, key và quota.'), ValueError('bad')):
            p = self.Flaky([exc])
            with patch.object(agent, '_sleep') as sleep:
                result = run_agent('x', p, StatusTools())
            self.assertEqual((result['status'], p.calls), ('error', 1))
            sleep.assert_not_called()

    def test_http_429_from_adapter_is_retried(self):
        p = self.Flaky([RuntimeError('Provider trả HTTP 429; kiểm tra model, key và quota.')])
        with patch.object(agent, '_sleep'):
            self.assertEqual(run_agent('x', p, StatusTools())['status'], 'completed')


class PaginationAndContextTests(unittest.TestCase):
    def test_pagination_hint_until_last_page(self):
        args = {'provider': 'vercel', 'incident_id': 'sps6n39gm4sg', 'limit': 3}
        url = BonusTools().execute('get_incident_details', args)['url']
        p = FakeProvider([tool_call('a', 'get_incident_details', json.dumps(args)),
                          tool_call('b', 'get_incident_details', json.dumps({**args, 'offset': 3})),
                          tool_call('c', 'get_incident_details', json.dumps({**args, 'offset': 6})),
                          final('Nguồn: ' + url)])
        result = run_agent('x', p, BonusTools(), max_steps=6)
        pages = [json.loads(h[-1]['content']) for h in p.histories[1:4]]
        self.assertIn('offset=3', pages[0]['pagination'])
        self.assertIn('offset=6', pages[1]['pagination'])
        self.assertNotIn('pagination', pages[2])
        self.assertEqual(result['status'], 'completed')

    def test_bonus_noise_is_compacted(self):
        p = FakeProvider([tool_call('a', 'get_vercel_status', '{}'), final('Vercel operational')])
        result = run_agent('x', p, BonusTools(scenario='noisy'), max_steps=3)
        seen = json.loads(p.histories[1][-1]['content'])
        self.assertEqual(len(seen['distractors']), agent.COMPACT_KEEP)
        self.assertEqual(seen['distractors_omitted'], 160 - agent.COMPACT_KEEP)
        self.assertTrue(any(t['event'] == 'context_compacted' for t in result['trace']))

    def test_main_lab_is_not_compacted_and_keeps_prompt(self):
        p = FakeProvider([tool_call('a', 'get_status', '{"provider":"github"}'), final('ok')])
        run_agent('x', p, ScenarioTools('noisy_status'), max_steps=3)
        self.assertEqual(len(json.loads(p.histories[1][-1]['content'])['distractors']), 40)
        self.assertEqual(p.histories[0][0]['content'], agent.SYSTEM_PROMPT)

    def test_bonus_prompt_appends_agent_rules(self):
        p = FakeProvider([final('ok')])
        run_agent('x', p, BonusTools())
        prompt = p.histories[0][0]['content']
        self.assertTrue(prompt.startswith(BonusTools.system_prompt))
        self.assertIn(agent.BONUS_RULES, prompt)


if __name__ == '__main__':
    unittest.main()
