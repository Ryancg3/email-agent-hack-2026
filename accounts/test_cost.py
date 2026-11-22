"""Claude tokens and cost per email, as an eval measures them (SHY-14)."""

from unittest import mock

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

import actions
import agent
import cost
import run_context

from .tests import EMAIL, RESULT, FakeClaude, tool_call


def usage(input_tokens, output_tokens):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}


class PriceTests(SimpleTestCase):
    def test_sonnet_costs_2_dollars_in_and_10_out_per_million_tokens(self):
        self.assertAlmostEqual(cost.dollars("claude-sonnet-5-5", 1_000_000, 0), 2.00)
        self.assertAlmostEqual(cost.dollars("claude-sonnet-5-5", 0, 1_000_000), 10.00)
        self.assertAlmostEqual(cost.dollars("claude-sonnet-5-5", 3_000, 500), 0.011)

    def test_the_agents_model_and_the_eval_models_have_a_price(self):
        for model in [agent.MODEL, "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]:
            self.assertIn(model, cost.PRICES)

    def test_a_model_without_a_price_fails_loudly(self):
        with self.assertRaisesMessage(KeyError, "No price for claude-unknown"):
            cost.dollars("claude-unknown", 10, 10)


class TallyTests(SimpleTestCase):
    def test_outside_track_nothing_is_recorded(self):
        cost.record("claude-sonnet-5-5", usage(100, 10))  # no error, nowhere to go
        with cost.track() as tally:
            pass
        self.assertEqual(tally.calls, 0)
        self.assertEqual(tally.dollars(), 0)

    def test_a_split_setup_prices_each_model_at_its_own_rate(self):
        with cost.track() as tally:
            cost.record("claude-haiku-5-5", usage(2_000, 100))
            cost.record("claude-sonnet-5-5", usage(3_000, 500))
        self.assertEqual((tally.calls, tally.input_tokens, tally.output_tokens), (2, 5_000, 600))
        self.assertAlmostEqual(tally.dollars(), 0.00025 + 0.011)

    def test_tracks_nest_without_mixing(self):
        with cost.track() as outer:
            cost.record("claude-sonnet-5-5", usage(1, 1))
            with cost.track() as inner:
                cost.record("claude-sonnet-5-5", usage(5, 5))
        self.assertEqual((outer.input_tokens, inner.input_tokens), (1, 5))


class AgentRecordsTokensTests(SimpleTestCase):
    def answer(self, stop_reason, tokens):
        raw = AIMessage(content="", response_metadata={"stop_reason": stop_reason}, usage_metadata=tokens)
        return {"raw": raw, "parsed": "parsed", "parsing_error": None}

    def test_ask_records_its_call_also_when_refused(self):
        with mock.patch.object(agent, "ChatAnthropic") as chat, cost.track() as tally:
            invoke = chat.return_value.with_structured_output.return_value.invoke
            invoke.return_value = self.answer("end_turn", usage(1_200, 80))
            agent.ask("system", "user", agent.Reply, max_tokens=10)
            invoke.return_value = self.answer("refusal", usage(1_000, 5))
            with self.assertRaises(RuntimeError):
                agent.ask("system", "user", agent.Reply, max_tokens=10)
        self.assertEqual(tally.tokens, {agent.MODEL: {"input": 2_200, "output": 85}})
        self.assertEqual(tally.calls, 2)

    def test_act_records_every_claude_call_in_its_loop(self):
        def take_note(note: str) -> str:
            """Take a note about the email."""
            return "noted"

        first = tool_call("take_note", {"note": "hi"})
        first.usage_metadata = usage(3_000, 40)
        fake = FakeClaude(messages=iter([first, AIMessage(content="Noted.", usage_metadata=usage(3_100, 20))]), bound=[])
        with mock.patch.object(agent, "act_model", lambda: fake), mock.patch.object(
            actions, "TOOLS", [tool(take_note)]
        ), run_context.use(run_context.RunContext(None)), mock.patch("builtins.print"), cost.track() as tally:
            agent.act(EMAIL, RESULT, [])
        self.assertEqual(tally.calls, 2)
        self.assertEqual((tally.input_tokens, tally.output_tokens), (6_100, 60))
        self.assertAlmostEqual(tally.dollars(), 6_100 * 2 / 1e6 + 60 * 10 / 1e6)
