"""Bounded model retries: actual urllib Request bodies, entirely offline.

No import app.main, user DB, real API key, live proxy or sleeping. Run directly.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.model_client import ModelClient, ModelUnavailable


class Response:
    def __init__(self, content):
        self.payload = json.dumps({'choices': [{'message': {'content': content}}]}).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.payload


def http_error(code, detail='offline error'):
    return urllib.error.HTTPError('http://invalid.test/v1/chat/completions', code,
                                  'offline', {}, io.BytesIO(detail.encode()))


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.client = ModelClient(base_url='http://invalid.test/v1', model='offline-model', timeout=7)
        self.client.api_key = 'FAKE_TEST_KEY_NEVER_LIVE'
        self.messages = [{'role': 'system', 'content': '只输出对象'},
                         {'role': 'user', 'content': '私有测试正文，不进public日志'}]
        self.calls = []
        self.sleeps = []

    def run_offline(self, actions, fn):
        actions = iter(actions)
        def urlopen(req, timeout):
            body = json.loads(req.data.decode('utf-8'))
            self.calls.append({'body': body, 'headers': dict(req.header_items()),
                               'url': req.full_url, 'timeout': timeout, 'method': req.method})
            action = next(actions)
            if isinstance(action, BaseException):
                raise action
            return Response(action)
        with patch('app.model_client.urllib.request.urlopen', side_effect=urlopen), \
                patch('app.model_client.time.sleep', side_effect=self.sleeps.append):
            return fn()

    @property
    def budgets(self):
        return [c['body']['max_tokens'] for c in self.calls]

    def test_empty_empty_valid_json_reaches_existing_ceiling(self):
        out = self.run_offline([None, '  ', '{"ok":true}'], lambda: self.client.chat_json(self.messages))
        self.assertEqual(out, {'ok': True})
        self.assertEqual(self.budgets, [1600, 3200, 8000])
        self.assertEqual(self.sleeps, [1.5, 3.0])
        for c in self.calls:
            self.assertEqual(c['body']['messages'], self.messages)
            self.assertEqual(c['body']['model'], 'offline-model')
            self.assertEqual(c['body']['temperature'], 0.2)
            self.assertEqual(c['headers']['Authorization'], 'Bearer FAKE_TEST_KEY_NEVER_LIVE')
            self.assertEqual(c['method'], 'POST')
            self.assertEqual(c['timeout'], 7)

    def test_always_empty_bounded_failure_not_success(self):
        with self.assertRaisesRegex(ModelUnavailable, '模型返回了空内容'):
            self.run_offline(['', None, ' '], lambda: self.client.chat_json(self.messages))
        self.assertEqual(self.budgets, [1600, 3200, 8000])
        self.assertEqual(len(self.sleeps), 2)

    def test_success_first_attempt_does_not_increase_cost(self):
        self.assertEqual(self.run_offline(['有效'], lambda: self.client.chat(self.messages)), '有效')
        self.assertEqual(self.budgets, [800])
        self.assertEqual(self.sleeps, [])

    def test_explicit_above_ceiling_never_shrinks(self):
        self.run_offline(['', '', '有效'], lambda: self.client.chat(self.messages, max_tokens=12000))
        self.assertEqual(self.budgets, [12000, 12000, 12000])

    def test_near_ceiling_never_shrinks(self):
        self.run_offline(['', '', '有效'], lambda: self.client.chat(self.messages, max_tokens=7000))
        self.assertEqual(self.budgets, [7000, 8000, 8000])

    def test_transient_http_retains_original_budget(self):
        for code in (502, 503, 504):
            with self.subTest(code=code):
                self.calls.clear(); self.sleeps.clear()
                self.assertEqual(self.run_offline([http_error(code), http_error(code), '有效'],
                    lambda: self.client.chat(self.messages, max_tokens=1600)), '有效')
                self.assertEqual(self.budgets, [1600, 1600, 1600])
                self.assertEqual(self.sleeps, [1.5, 3.0])

    def test_transient_http_exhaustion_raises(self):
        with self.assertRaisesRegex(ModelUnavailable, 'HTTP 503'):
            self.run_offline([http_error(503) for _ in range(3)], lambda: self.client.chat(self.messages))
        self.assertEqual(self.budgets, [800, 800, 800])
        self.assertEqual(len(self.sleeps), 2)

    def test_nonretryable_http_immediately_fails(self):
        for code in (400, 401, 403, 422):
            with self.subTest(code=code):
                self.calls.clear(); self.sleeps.clear()
                with self.assertRaisesRegex(ModelUnavailable, f'HTTP {code}'):
                    self.run_offline([http_error(code)], lambda: self.client.chat(self.messages))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.sleeps, [])

    def test_mixed_empty_transient_keeps_latest_budget(self):
        self.run_offline(['', http_error(502), '{"ok":true}'], lambda: self.client.chat_json(self.messages))
        # Generic HTTP failures do not trigger token escalation.
        self.assertEqual(self.budgets, [1600, 3200, 3200])

    def test_vision_empty_retries_preserve_uri_text_and_header(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = self.run_offline([None, '', '{"read":"fixture"}'],
                lambda: self.client.chat_vision_json('私有系统正文', 'FAKE_BASE64_IMAGE',
                                                     user_prompt='私有图片备注'))
        self.assertEqual(result, {'read': 'fixture'})
        self.assertEqual(self.budgets, [1600, 3200, 8000])
        for c in self.calls:
            msg = c['body']['messages']
            self.assertEqual(msg[0]['content'], '私有系统正文')
            self.assertEqual(msg[1]['content'][0]['text'], '私有图片备注')
            self.assertEqual(msg[1]['content'][1]['image_url']['url'], 'data:image/jpeg;base64,FAKE_BASE64_IMAGE')
            self.assertEqual(c['headers']['Authorization'], 'Bearer FAKE_TEST_KEY_NEVER_LIVE')
        self.assertEqual(output.getvalue(), '')

    def test_vision_transient_urlinvalid_retains_request(self):
        self.run_offline([http_error(400, 'url invalid'), '有效'],
                        lambda: self.client.chat_vision('系统正文', 'FAKE_IMAGE'))
        self.assertEqual(self.budgets, [800, 800])
        self.assertEqual(self.calls[0]['body'], self.calls[1]['body'])

    def test_json_parse_correction_preserves_original_contract(self):
        result = self.run_offline(['not JSON', '{"ok":1}'], lambda: self.client.chat_json(self.messages))
        self.assertEqual(result, {'ok': 1})
        self.assertEqual(self.budgets, [1600, 2400])
        corrected = self.calls[1]['body']
        self.assertEqual(corrected['messages'][:-1], self.messages)
        self.assertIn('上一次输出不是合法 JSON', corrected['messages'][-1]['content'])
        self.assertEqual(corrected['temperature'], 0)

    def test_json_parse_correction_maximum_six_http_attempts(self):
        with self.assertRaisesRegex(ModelUnavailable, '模型两次未返回可解析的 JSON'):
            self.run_offline(['', '', 'not JSON', '', '', 'still not JSON'],
                            lambda: self.client.chat_json(self.messages))
        self.assertEqual(self.budgets, [1600, 3200, 8000, 2400, 4800, 8000])
        self.assertEqual(len(self.sleeps), 4)

    def test_vision_parse_correction_keeps_image(self):
        self.run_offline(['bad JSON', '{"ok":true}'],
                        lambda: self.client.chat_vision_json('original system', 'FAKE_IMAGE', user_prompt='original text'))
        first, second = [c['body']['messages'] for c in self.calls]
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1]['content'][1], second[1]['content'][1])
        self.assertTrue(second[1]['content'][0]['text'].startswith('original text'))
        self.assertEqual(self.budgets, [1600, 2400])


if __name__ == '__main__':
    unittest.main(verbosity=2)
