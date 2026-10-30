import copy
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import harness
import llm_client


def structured_summary(**overrides):
    summary = harness.empty_compaction_summary()
    summary.update(
        {
            "current_goal": "Complete reconstruction and inspect J12.",
            "current_stage": "Initial reconstruction",
            "jobs": [
                {
                    "job_uid": "J12",
                    "job_type": "homo_abinit",
                    "status": "completed",
                    "purpose": "Create an initial volume.",
                    "inputs": ["particles from J10"],
                    "key_parameters": ["symmetry=C1"],
                    "results": ["volume available"],
                    "next_action": "Inspect the volume before refinement.",
                }
            ],
            "observations": ["J12 completed successfully."],
            "next_actions": ["Inspect the J12 volume."],
            "memory_search_terms": ["J12", "initial volume"],
        }
    )
    summary.update(overrides)
    return summary


def image_message(payload="AAAA"):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "inspect this class average"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
        ],
    }


def sample_messages():
    return [
        {"role": "developer", "content": "instructions"},
        {"role": "user", "content": "initial request"},
        {"role": "assistant", "content": "old analysis " + ("x" * 200)},
        {"role": "user", "content": "middle cryoSPARC state J12"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "run_code",
                        "arguments": '{"code": "print(job_summary(job))"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true, "stdout": "J12 completed"}'},
        {"role": "assistant", "content": "latest assistant context"},
        {"role": "user", "content": "latest active user request"},
    ]


def responses_tool_messages():
    return [
        {"role": "developer", "content": "instructions"},
        {"role": "user", "content": "initial request"},
        {"role": "assistant", "content": "old state"},
        {"role": "assistant", "content": "older state"},
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "run_code",
            "arguments": '{"code": "print(1)"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'},
        {"role": "assistant", "content": "latest assistant context"},
        {"role": "user", "content": "latest active user request"},
        {"role": "assistant", "content": "latest final context"},
    ]


class HarnessCompactionTests(unittest.TestCase):
    def setUp(self):
        harness.reset_agent_memory()
        self._trace_tools = harness.TRACE_TOOLS
        self._context_window = harness.CONTEXT_WINDOW
        self._threshold = harness.CONTEXT_COMPACTION_THRESHOLD
        self._target_ratio = harness.CONTEXT_COMPACTION_TARGET_RATIO
        harness.TRACE_TOOLS = False
        harness.CONTEXT_WINDOW = 1000
        harness.CONTEXT_COMPACTION_THRESHOLD = 0.5
        harness.CONTEXT_COMPACTION_TARGET_RATIO = 0.2

    def tearDown(self):
        harness.reset_agent_memory()
        harness.TRACE_TOOLS = self._trace_tools
        harness.CONTEXT_WINDOW = self._context_window
        harness.CONTEXT_COMPACTION_THRESHOLD = self._threshold
        harness.CONTEXT_COMPACTION_TARGET_RATIO = self._target_ratio

    def test_token_estimation_counts_text_tool_calls_and_images(self):
        text_tokens = harness.estimate_message_tokens({"role": "user", "content": "x" * 40})
        image_tokens = harness.estimate_message_tokens(image_message())
        tool_tokens = harness.estimate_message_tokens(sample_messages()[4])

        self.assertGreater(image_tokens, text_tokens)
        self.assertGreater(tool_tokens, harness.estimate_message_tokens({"role": "assistant", "content": ""}))

    def test_tail_alignment_keeps_tool_call_parent_with_tool_result(self):
        messages = sample_messages()
        tail_start = harness.align_tail_start_for_tool_groups(messages, 5, 2)

        self.assertEqual(tail_start, 4)
        self.assertEqual(messages[tail_start]["role"], "assistant")
        self.assertEqual(messages[tail_start + 1]["role"], "tool")

    def test_tail_alignment_keeps_reasoning_with_responses_function_call(self):
        messages = responses_tool_messages()

        from_function_call = harness.align_tail_start_for_tool_groups(messages, 5, 2)
        from_tool_result = harness.align_tail_start_for_tool_groups(messages, 6, 2)

        self.assertEqual(from_function_call, 4)
        self.assertEqual(from_tool_result, 4)
        self.assertEqual(messages[from_tool_result]["type"], "reasoning")
        self.assertEqual(messages[from_tool_result + 1]["type"], "function_call")
        self.assertEqual(messages[from_tool_result + 2]["type"], "function_call_output")

    def test_compaction_preserves_head_tail_and_records_summary(self):
        messages = sample_messages()
        harness.remember_messages(messages)
        summary = structured_summary()

        compacted = harness.compact_messages(
            messages,
            force=True,
            summary_func=lambda turns, previous: summary,
        )

        self.assertEqual(compacted[0], messages[0])
        self.assertEqual(compacted[1], messages[1])
        self.assertTrue(compacted[2]["content"].startswith(harness.COMPACTION_SUMMARY_PREFIX))
        serialized_summary = compacted[2]["content"].split("\n\n", 1)[1]
        expected_json = json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(serialized_summary, expected_json)
        self.assertEqual(json.loads(serialized_summary), summary)
        self.assertLess(len(compacted), len(messages))
        self.assertIsInstance(harness.agent_memory["last_summary"], dict)
        self.assertEqual(harness.agent_memory["last_summary"], summary)
        self.assertEqual(len(harness.agent_memory["compactions"]), 1)

        full_history = harness.agent_memory["full_history"]
        self.assertEqual(len(full_history), len(messages) + 1)
        self.assertTrue(harness.is_compaction_summary_message(full_history[-1]))

    def test_compaction_does_not_split_responses_reasoning_tool_group(self):
        messages = responses_tool_messages()
        harness.remember_messages(messages)

        compacted = harness.compact_messages(
            messages,
            force=True,
            summary_func=lambda turns, previous: structured_summary(
                current_stage="Immediately before the retained tool group"
            ),
        )

        retained_types = [message.get("type") for message in compacted]
        self.assertLess(len(compacted), len(messages))
        self.assertIn("reasoning", retained_types)
        self.assertIn("function_call", retained_types)
        self.assertIn("function_call_output", retained_types)

        reasoning_idx = retained_types.index("reasoning")
        self.assertEqual(retained_types[reasoning_idx + 1], "function_call")
        self.assertEqual(retained_types[reasoning_idx + 2], "function_call_output")

    def test_full_history_preserves_exact_image_payload_by_copy(self):
        original = image_message(payload="BASE64_PAYLOAD")
        mutated = copy.deepcopy(original)
        harness.remember_messages([mutated])
        mutated["content"][1]["image_url"]["url"] = "data:image/png;base64,CHANGED"

        stored = harness.agent_memory["full_history"][0]
        self.assertIn("BASE64_PAYLOAD", stored["content"][1]["image_url"]["url"])
        self.assertIs(harness.python_globals["conversation_history"], harness.agent_memory["full_history"])

    def test_summary_serialization_omits_image_payload_but_keeps_memory_hint(self):
        serialized = harness.serialize_message_for_summary(image_message(payload="SECRET_BASE64"))

        self.assertIn("image omitted from summary", serialized)
        self.assertIn("original preserved in agent_memory", serialized)
        self.assertNotIn("SECRET_BASE64", serialized)

    def test_fallback_summary_points_to_repl_memory(self):
        fallback = harness.build_fallback_summary(sample_messages()[2:5], RuntimeError("context length"))

        self.assertEqual(fallback["schema_version"], harness.COMPACTION_SCHEMA_VERSION)
        self.assertIn("context length", fallback["summary_failure"])
        self.assertIn("memory_search", " ".join(fallback["resume_notes"]))
        self.assertIn("memory_slice", " ".join(fallback["resume_notes"]))
        self.assertEqual(fallback["memory_search_terms"], ["job UID", "error", "selection", "file path"])
        self.assertEqual(harness.validate_compaction_summary(fallback), fallback)

    def test_fallback_preserves_redacted_raw_output_from_second_invalid_attempt(self):
        messages = sample_messages()
        harness.remember_messages(messages)
        first_raw = '{"schema_version":"wrong-version"}'
        second_raw = "SECOND INVALID OUTPUT\npassword=secret-two\n{unfinished-json"
        responses = [
            SimpleNamespace(output_text=first_raw, output=[]),
            SimpleNamespace(output_text=second_raw, output=[]),
        ]

        with (
            mock.patch.object(harness, "client", object()),
            mock.patch.object(harness, "create_response", side_effect=responses) as create,
        ):
            compacted = harness.compact_messages(messages, force=True)

        self.assertEqual(create.call_count, 2)
        fallback = harness.agent_memory["last_summary"]
        self.assertIn("structured and prompt-only attempts", fallback["summary_failure"])
        self.assertEqual(
            fallback["resume_notes"][-1],
            "SECOND INVALID OUTPUT\npassword=[REDACTED]\n{unfinished-json",
        )
        self.assertNotIn("wrong-version", fallback["resume_notes"][-1])
        self.assertNotIn("secret-two", compacted[2]["content"])
        self.assertEqual(harness.validate_compaction_summary(fallback), fallback)

    def test_string_summary_normalizes_to_structured_resume_note(self):
        normalized = harness.normalize_compaction_summary("legacy summary for J12")

        self.assertEqual(normalized["schema_version"], harness.COMPACTION_SCHEMA_VERSION)
        self.assertEqual(normalized["resume_notes"], ["legacy summary for J12"])
        self.assertIsNone(normalized["summary_failure"])
        self.assertEqual(harness.validate_compaction_summary(normalized), normalized)

    def test_validate_compaction_summary_enforces_strict_schema(self):
        valid = structured_summary(
            artifacts=[
                {
                    "path": "/cryosparc/P1/J12/J12_001_volume.mrc",
                    "description": "Initial volume",
                    "source_job_uid": "J12",
                }
            ],
            selections=["Keep classes 1 and 3"],
            decisions=[
                {
                    "decision": "Refine J12",
                    "rationale": "The volume has interpretable features.",
                    "evidence": ["J12 volume inspection"],
                }
            ],
            failures_and_warnings=[
                {
                    "description": "Preferred orientations remain possible.",
                    "status": "open",
                    "recovery": "Inspect orientation diagnostics after refinement.",
                }
            ],
        )

        validated = harness.validate_compaction_summary(valid)
        self.assertEqual(validated, valid)
        self.assertIsNot(validated, valid)
        self.assertIsNot(validated["jobs"], valid["jobs"])

        invalid_cases = {
            "missing top-level key": lambda value: value.pop("summary_failure"),
            "unknown top-level key": lambda value: value.update({"future": True}),
            "wrong schema version": lambda value: value.update({"schema_version": "v0"}),
            "wrong top-level type": lambda value: value.update({"current_goal": None}),
            "invalid nested shape": lambda value: value["jobs"][0].pop("status"),
            "invalid nested list item": lambda value: value["jobs"][0]["inputs"].append(1),
            "invalid string array item": lambda value: value["memory_search_terms"].append(None),
        }
        for label, mutate in invalid_cases.items():
            with self.subTest(label=label):
                invalid = copy.deepcopy(valid)
                mutate(invalid)
                with self.assertRaises(ValueError):
                    harness.validate_compaction_summary(invalid)

    def test_response_output_items_preserves_none_phase_and_unknown_fields(self):
        source = {
            "type": "message",
            "id": "msg_1",
            "phase": "analysis",
            "status": None,
            "unknown_future_field": {"nested": None},
            "content": [{"type": "output_text", "text": "done", "annotations": None}],
        }

        output = harness.response_output_items(SimpleNamespace(output=[source]))

        self.assertEqual(output, [source])
        self.assertIn("status", output[0])
        self.assertIsNone(output[0]["status"])
        self.assertEqual(output[0]["phase"], "analysis")
        self.assertEqual(output[0]["unknown_future_field"], {"nested": None})
        source["unknown_future_field"]["nested"] = "changed"
        self.assertIsNone(output[0]["unknown_future_field"]["nested"])

    def test_response_output_items_requests_null_fields_from_typed_items(self):
        class TypedItem:
            def __init__(self):
                self.dump_options = None

            def model_dump(self, **kwargs):
                self.dump_options = kwargs
                return {
                    "type": "message",
                    "phase": "final_answer",
                    "status": None,
                    "unknown_future_field": None,
                }

        item = TypedItem()
        output = harness.response_output_items(SimpleNamespace(output=[item]))

        self.assertEqual(item.dump_options, {"mode": "json", "exclude_none": False})
        self.assertEqual(output[0]["phase"], "final_answer")
        self.assertIn("status", output[0])
        self.assertIsNone(output[0]["unknown_future_field"])

    def test_response_output_items_wraps_compatible_output_text_only(self):
        output = harness.response_output_items(
            SimpleNamespace(output=[], output_text="compatible provider answer")
        )

        self.assertEqual(
            output,
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "compatible provider answer",
                            "annotations": [],
                        }
                    ],
                }
            ],
        )

    def test_response_output_items_keeps_reasoning_and_wraps_separate_output_text(self):
        reasoning = {
            "type": "reasoning",
            "id": "reasoning_1",
            "encrypted_content": "opaque",
        }

        output = harness.response_output_items(
            SimpleNamespace(output=[reasoning], output_text="compatible provider answer")
        )

        self.assertEqual(output[0], reasoning)
        self.assertEqual(output[1]["type"], "message")
        self.assertEqual(
            output[1]["content"][0]["text"], "compatible provider answer"
        )

    def test_memory_search_and_slice_return_expected_history(self):
        messages = sample_messages()
        harness.remember_messages(messages)

        hits = harness.memory_search("J12")
        exact = harness.memory_slice(hits[0]["index"], hits[0]["index"] + 1)

        self.assertEqual(hits[0]["role"], "user")
        self.assertEqual(exact[0]["content"], "middle cryoSPARC state J12")


class CompactionSummaryRequestTests(unittest.TestCase):
    """Failures seen in production: empty or cut-off summaries at xhigh effort."""

    def setUp(self):
        harness.reset_agent_memory()
        self._globals = {
            name: getattr(harness, name)
            for name in (
                "TRACE_TOOLS",
                "COMPACT_MAX_TOKENS",
                "COMPACT_REASONING_EFFORT",
            )
        }
        harness.TRACE_TOOLS = False
        harness.COMPACT_MAX_TOKENS = 16000
        harness.COMPACT_REASONING_EFFORT = "low"

    def tearDown(self):
        harness.reset_agent_memory()
        for name, value in self._globals.items():
            setattr(harness, name, value)

    def test_create_response_keeps_an_explicit_reasoning_effort(self):
        calls = []
        fake_client = llm_client.OpenAIResponsesClient(
            SimpleNamespace(
                responses=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs))
            ),
            reasoning_effort="xhigh",
        )
        with mock.patch.object(harness, "client", fake_client):
            harness.create_response(model="m", input=[])
            harness.create_response(model="m", input=[], reasoning={"effort": "low"})

        self.assertEqual(calls[0]["reasoning"], {"effort": "xhigh"})
        self.assertEqual(calls[1]["reasoning"], {"effort": "low"})

    def test_summary_uses_low_effort_and_retries_a_cut_off_response(self):
        cut_off = SimpleNamespace(
            output_text="",
            output=[],
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        )
        complete = SimpleNamespace(
            output_text=json.dumps(structured_summary()), output=[], status="completed"
        )
        requests = []

        def fake_create(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            return cut_off if len(requests) == 1 else complete

        with (
            mock.patch.object(harness, "client", object()),
            mock.patch.object(harness, "create_response", side_effect=fake_create),
        ):
            summary = harness.generate_compaction_summary(sample_messages()[2:6])

        self.assertEqual(summary["current_goal"], "Complete reconstruction and inspect J12.")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["reasoning"], {"effort": "low"})
        self.assertEqual(requests[0]["max_output_tokens"], 16000)
        self.assertEqual(requests[1]["max_output_tokens"], 32000)

    def test_cut_off_at_the_ceiling_is_reported_not_retried_forever(self):
        harness.COMPACT_MAX_TOKENS = harness.COMPACT_MAX_TOKENS_CEILING
        cut_off = SimpleNamespace(
            output_text='{"schema_version":',
            output=[],
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
        )
        with (
            mock.patch.object(harness, "client", object()),
            mock.patch.object(harness, "create_response", return_value=cut_off) as create,
        ):
            with self.assertRaisesRegex(
                harness.CompactionSummaryGenerationError, "max_output_tokens"
            ):
                harness.generate_compaction_summary(sample_messages()[2:6])
        self.assertEqual(create.call_count, 2)

    def test_failed_summary_keeps_previous_checkpoint_and_recent_conclusions(self):
        previous = structured_summary()
        turns = [
            {"role": "assistant", "content": "J17 is the best map at 3.5 A; keep J22 as validation."},
            {"type": "function_call", "call_id": "c1", "name": "run_code", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": '{"ok": true}'},
        ]

        fallback = harness.build_fallback_summary(
            turns, RuntimeError("empty content"), previous
        )

        self.assertEqual(fallback["current_goal"], previous["current_goal"])
        self.assertEqual(fallback["jobs"], previous["jobs"])
        self.assertTrue(
            any("J17 is the best map" in item for item in fallback["observations"])
        )
        self.assertIn("empty content", fallback["summary_failure"])
        self.assertEqual(harness.validate_compaction_summary(fallback), fallback)

    def test_summary_input_keeps_the_start_and_end_of_long_histories(self):
        turns = [{"role": "user", "content": "FIRST SETUP TURN"}]
        turns += [{"role": "assistant", "content": "x" * 5000} for _ in range(80)]
        turns += [{"role": "user", "content": "LATEST TURN"}]

        text = harness.serialize_messages_for_summary(turns)

        self.assertLessEqual(len(text), harness.SUMMARY_INPUT_MAX_CHARS + 200)
        self.assertIn("FIRST SETUP TURN", text)
        self.assertIn("LATEST TURN", text)
        self.assertIn("Middle of the serialized compaction input was truncated", text)

    def test_compaction_reasoning_effort_round_trips_through_session_config(self):
        harness.COMPACT_REASONING_EFFORT = "medium"
        config = harness.compact_configuration()
        harness.COMPACT_REASONING_EFFORT = "low"

        harness.restore_compact_configuration(config)

        self.assertEqual(harness.COMPACT_REASONING_EFFORT, "medium")


class ToolOutputClippingTests(unittest.TestCase):
    def test_large_run_code_output_is_clipped_but_kept_in_the_repl(self):
        code = "print('START' + 'x' * 200000 + 'END')"
        with mock.patch.object(harness, "TOOL_OUTPUT_MAX_CHARS", 1000):
            result = harness.run_python(code)

        self.assertTrue(result["ok"])
        self.assertEqual(result["output_clipped"], ["stdout"])
        self.assertLess(len(result["stdout"]), 1200)
        self.assertTrue(result["stdout"].startswith("START"))
        self.assertIn("END", result["stdout"])
        self.assertIn("last_tool_output", result["stdout"])
        full = harness.python_globals["last_tool_output"]["stdout"]
        self.assertEqual(len(full), len("START") + 200000 + len("END") + 1)

    def test_small_output_is_unchanged(self):
        result = harness.run_python("print('J12 completed')")
        self.assertEqual(result["stdout"], "J12 completed\n")
        self.assertNotIn("output_clipped", result)


if __name__ == "__main__":
    unittest.main()
