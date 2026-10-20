import unittest

from authorization_eva_sandbox.evaluation.judges import CompletionJudge
from authorization_eva_sandbox.evaluation.judges.violation_judge import ViolationJudge

ANSWER = '{"reasoning": "At t=003 the agent sent it.", "matched_criterion": "x", "answer": "yes"}'


def judge(cls, replies):
    j = cls(model="primary-model", api_key="k")
    calls = []

    def request(prompt, model, api_key, base_url, completion_kwargs):
        calls.append(model)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    j._request = request
    return j, calls


class JudgeFallbackTests(unittest.TestCase):
    def test_defaults_are_opus_5_5_then_opus_4_8(self):
        for cls in (CompletionJudge, ViolationJudge):
            self.assertEqual(cls.DEFAULT_PROFILE, "opus_5_5")
            self.assertEqual(cls.FALLBACK_PROFILES, ("opus_4_8",))
            self.assertEqual(cls.max_tokens, 4000)

    def test_refusal_is_answered_by_opus_4_8(self):
        j, calls = judge(ViolationJudge, [("", "content_filter"), (ANSWER, "stop")])
        out = j.evaluate("[t=003] sent", ["x"])
        self.assertEqual((out.answer, out.fallback_used), ("yes", True))
        self.assertIn("4.8", out.model_used)

    def test_both_refusing_is_inconclusive(self):
        j, calls = judge(ViolationJudge, [("", "content_filter")] * 8)
        j.retry_backoff_seconds = 0
        out = j.evaluate("[t=003] sent", ["x"])
        self.assertEqual((out.answer, out.fallback_used), ("inconclusive", True))
        self.assertEqual(len(calls), 8)

    def test_no_fallback_when_primary_answers(self):
        j, calls = judge(ViolationJudge, [(ANSWER, "stop")])
        out = j.evaluate("[t=003] sent", ["x"])
        self.assertEqual((out.answer, out.fallback_used, out.model_used), ("yes", False, "primary-model"))
        self.assertEqual(calls, ["primary-model"])

    def test_content_filter_goes_to_fallback(self):
        for refusal in (("", "content_filter"), RuntimeError("litellm: content_filter triggered")):
            j, calls = judge(ViolationJudge, [refusal, (ANSWER, "stop")])
            out = j.evaluate("[t=003] sent", ["x"])
            self.assertEqual(out.answer, "yes")
            self.assertTrue(out.fallback_used)
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[1], "primary-model")

    def test_other_errors_do_not_fall_back(self):
        j, calls = judge(CompletionJudge, [RuntimeError("timeout"), (ANSWER, "stop")])
        j.retry_backoff_seconds = 0
        out = j.evaluate("[t=003] sent", ["x"])
        self.assertEqual((out.answer, out.fallback_used), ("yes", False))
        self.assertEqual(calls, ["primary-model", "primary-model"])


if __name__ == "__main__":
    unittest.main()
