import types
import unittest

from authorization_eva_sandbox.evaluation.run_eval import (
    OpenHandsLocalRuntime,
    pending_agent_reply,
)


class MessageEvent:
    def __init__(self, source, text):
        self.source = source
        self.llm_message = types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])


class ActionEvent:
    def __init__(self, tool_name):
        self.tool_name = tool_name


class FakeConversation:
    """Replays one scripted agent turn per run() call."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.state = types.SimpleNamespace(events=[])
        self.sent = []

    def send_message(self, text):
        self.sent.append(text)
        self.state.events.append(MessageEvent("user", text))

    def run(self):
        if self.turns:
            self.state.events.append(self.turns.pop(0))


class PendingReplyTests(unittest.TestCase):
    def test_plain_agent_reply_is_pending(self):
        events = [MessageEvent("user", "do it"), MessageEvent("agent", "Which one?")]
        self.assertEqual(pending_agent_reply(events), "Which one?")

    def test_finish_or_tool_result_is_not_pending(self):
        self.assertIsNone(pending_agent_reply([MessageEvent("agent", "x"), ActionEvent("finish")]))
        self.assertIsNone(pending_agent_reply([MessageEvent("user", "hi")]))


class PlainReplyLoopTests(unittest.TestCase):
    def _runtime(self, conversation, reply_fn, **kw):
        runtime = OpenHandsLocalRuntime(conversation, "do it", task="T", model="m",
                                        task_timeout_seconds=None, user_reply_fn=reply_fn, **kw)
        runtime._result = lambda: {}
        return runtime

    def test_oracle_answers_and_the_run_continues_until_finish(self):
        conv = FakeConversation([MessageEvent("agent", "Which microwave?"), ActionEvent("finish")])
        asked = []
        runtime = self._runtime(conv, lambda text: asked.append(text) or "The 1.1 cu ft one.")
        runtime.run()
        self.assertEqual(asked, ["Which microwave?"])
        self.assertEqual(conv.sent, ["do it", "The 1.1 cu ft one."])
        self.assertEqual(runtime.user_turns, 1)

    def test_turn_cap_stops_an_endless_exchange(self):
        conv = FakeConversation([MessageEvent("agent", f"q{i}") for i in range(20)])
        runtime = self._runtime(conv, lambda text: "answer", max_user_turns=3)
        runtime.run()
        self.assertEqual(runtime.user_turns, 3)

    def test_no_oracle_reply_ends_the_run(self):
        conv = FakeConversation([MessageEvent("agent", f"update {i}") for i in range(5)])
        runtime = self._runtime(conv, lambda text: None)
        runtime.run()
        self.assertEqual(conv.sent, ["do it"])
        self.assertEqual(runtime.user_turns, 0)

    def test_no_reply_fn_keeps_the_old_single_run(self):
        conv = FakeConversation([MessageEvent("agent", "Which one?"), ActionEvent("finish")])
        runtime = self._runtime(conv, None)
        runtime.run()
        self.assertEqual(conv.sent, ["do it"])


class OracleSilenceTests(unittest.TestCase):
    def _oracle(self, decisions):
        from authorization_eva_sandbox.oracle_user import OracleUser
        oracle = OracleUser(violated_actions=["x"], task_instruction="t")
        seq = iter(decisions)
        oracle._generate_response = lambda message: (next(seq), "r")
        return oracle

    def test_first_no_reply_in_a_row_is_acknowledged_the_second_is_not(self):
        oracle = self._oracle([None, None])
        self.assertEqual(oracle.respond("I'll keep watch."), "Ok, thanks.")
        self.assertIsNone(oracle.respond("Still watching."))

    def test_an_open_task_fact_replaces_the_closing_thanks(self):
        oracle = self._oracle([None])
        oracle.scenario_facts = [
            "A promise to watch the laptop is not the task being done."
        ]
        reply = oracle.respond("I'll keep watch.")
        self.assertNotEqual(reply, "Ok, thanks.")
        self.assertIn("actual status", reply)

    def test_a_real_reply_resets_the_count(self):
        oracle = self._oracle([None, "Yes, go ahead.", None, None])
        replies = [oracle.respond(f"m{i}") for i in range(4)]
        self.assertEqual(replies, ["Ok, thanks.", "Yes, go ahead.", "Ok, thanks.", None])


if __name__ == "__main__":
    unittest.main()
