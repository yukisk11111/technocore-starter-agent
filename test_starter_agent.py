import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import starter_agent


TEST_DID = "did:key:z6MkuMpDWissXyN3KHzFFqZDZd8Q6Yoo6C2NuRZcHyyq9KnC"


class FakeRequest:
    def __init__(self, values):
        self.values = values

    def __call__(self, path):
        if path not in self.values and "&n=" in path:
            path = path.rsplit("&n=", 1)[0]
        value = self.values[path]
        if isinstance(value, BaseException):
            raise value
        return value


class StarterAgentTests(unittest.TestCase):
    def test_room_and_directory_decoders_reject_unbounded_or_invalid_data(self):
        room = json.dumps({"messages": [{"seq": 1, "from": TEST_DID, "text": "ok"}]})
        with mock.patch.object(starter_agent, "MAX_ROOM_MESSAGES", 0):
            with self.assertRaisesRegex(ValueError, "message limit"):
                starter_agent._decode_room(room)
        with self.assertRaisesRegex(ValueError, "invalid room entry"):
            starter_agent._decode_directory(
                json.dumps({"rooms": [{"room": "https://example.invalid"}]})
            )

    def test_oversized_state_is_refused_without_resetting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "starter-state.json"
            state_file.write_text('{"padding":"too large"}', encoding="utf-8")
            with mock.patch.multiple(
                starter_agent,
                STATE_FILE=state_file,
                MAX_STATE_BYTES=4,
            ):
                with self.assertRaisesRegex(SystemExit, "refusing to reset"):
                    starter_agent._load_state()
            self.assertEqual(state_file.read_text(encoding="utf-8"), '{"padding":"too large"}')

    def test_validates_real_did_key_bytes(self):
        self.assertEqual(starter_agent.validate_did(TEST_DID)[0], True)
        self.assertEqual(starter_agent.validate_did(TEST_DID[:-1] + "0")[0], False)

    def test_setup_check_uses_public_evidence(self):
        path = starter_agent.technocore.directory_path(TEST_DID)
        request = FakeRequest(
            {
                path: f"!! warning\n\n{TEST_DID} proof:lobby:2 mailbox:mb-p-example\n",
                "/r/mb-p-example?format=json&limit=200": json.dumps(
                    {
                        "messages": [
                            {"seq": 1, "from": TEST_DID, "nonce": 99, "text": "ready"},
                        ]
                    }
                ),
                "/r/lobby?format=json&limit=200": json.dumps(
                    {
                        "messages": [
                            {"seq": 2, "from": TEST_DID, "nonce": 100, "text": "hello"},
                        ]
                    }
                ),
            }
        )
        result = starter_agent.check_setup(TEST_DID, request=request)
        self.assertEqual(result["status"], "pass")
        self.assertEqual({item["check"] for item in result["checks"]}, {"did", "directory", "mailbox", "signed_activity", "nonce_order"})

    def test_setup_check_warns_when_advertised_mailbox_lacks_owner_activity(self):
        path = starter_agent.technocore.directory_path(TEST_DID)
        request = FakeRequest(
            {
                path: f"{TEST_DID} mailbox:mb-p-example",
                "/r/mb-p-example?format=json&limit=200": json.dumps(
                    {"messages": []}
                ),
                "/r/lobby?format=json&limit=200": json.dumps(
                    {
                        "messages": [
                            {"seq": 2, "from": TEST_DID, "nonce": 100, "text": "hello"},
                        ]
                    }
                ),
            }
        )
        result = starter_agent.check_setup(TEST_DID, request=request)
        mailbox = next(item for item in result["checks"] if item["check"] == "mailbox")
        self.assertEqual(mailbox["status"], "warn")
        self.assertIn("no activity signed", mailbox["detail"])

    def test_room_ranking_rewards_activity_and_diversity(self):
        active = {
            "window": 200,
            "idle_seconds": 0,
            "nick_diversity": 0.8,
            "zero_response_share": 0.1,
        }
        stale = {
            "window": 10,
            "idle_seconds": 100000,
            "nick_diversity": 0.1,
            "zero_response_share": 0.9,
        }
        self.assertGreater(starter_agent._room_score(active), starter_agent._room_score(stale))

    def test_build_suggestions_are_evidence_labeled(self):
        request = FakeRequest(
            {
                "/rooms?format=json&limit=200": json.dumps(
                    {
                        "total": 2,
                        "rooms": [
                            {"room": "did-help", "topic": "identity setup"},
                            {"room": "agent-search", "topic": "directory"},
                        ],
                    }
                )
            }
        )
        result = starter_agent.suggest_builds(request=request, top=2)
        self.assertEqual(result["scope"], 2)
        self.assertEqual(len(result["ideas"]), 2)
        self.assertIn("category_rooms", result["ideas"][0])
        self.assertEqual(result["novelty"]["library_total"], 91)
        self.assertEqual(result["novelty"]["controlled_generated_total"], 4200)
        self.assertEqual(result["novelty"]["candidate_space_total"], 4291)
        self.assertEqual(
            result["novelty"]["eligible_candidates_after_response"],
            4289,
        )
        self.assertIn("Candidate reserve after this response", starter_agent.format_builds(result))

    def test_build_suggestions_exclude_history_and_observed_room_names(self):
        request = FakeRequest(
            {
                "/rooms?format=json&limit=200": json.dumps(
                    {
                        "total": 2,
                        "rooms": [
                            {"room": "signature-receipt-archive", "topic": "identity"},
                            {"room": "agent-search", "topic": "directory"},
                        ],
                    }
                )
            }
        )
        result = starter_agent.suggest_builds(
            request=request,
            top=3,
            excluded_services=["room-change-monitor"],
            ranked_rooms=["market-claim-provenance"],
        )
        names = {item["service"] for item in result["ideas"]}
        self.assertNotIn("signature-receipt-archive", names)
        self.assertNotIn("room-change-monitor", names)
        self.assertNotIn("market-claim-provenance", names)
        self.assertEqual(result["novelty"]["match"], "normalized exact service/room name")

    def test_build_suggestions_never_repeat_when_candidate_space_is_exhausted(self):
        request = FakeRequest(
            {
                "/rooms?format=json&limit=200": json.dumps(
                    {"total": 0, "rooms": []}
                )
            }
        )
        every_name = [
            name
            for category_ideas in starter_agent._candidate_space().values()
            for name, _, _ in category_ideas
        ]
        result = starter_agent.suggest_builds(
            request=request,
            excluded_services=every_name,
        )
        self.assertEqual(result["ideas"], [])
        self.assertTrue(result["novelty"]["exhausted"])
        self.assertEqual(result["novelty"]["eligible_candidates_after_response"], 0)
        self.assertIn("No duplicate was returned", starter_agent.format_builds(result))

    def test_build_suggestions_recover_after_original_library_is_exhausted(self):
        request = FakeRequest(
            {
                "/rooms?format=json&limit=200": json.dumps(
                    {"total": 0, "rooms": []}
                )
            }
        )
        original_names = [
            name
            for category_ideas in starter_agent.IDEA_LIBRARY.values()
            for name, _ in category_ideas[:5]
        ]
        result = starter_agent.suggest_builds(
            request=request,
            excluded_services=original_names,
        )
        self.assertEqual(len(result["ideas"]), 3)
        self.assertFalse(result["novelty"]["exhausted"])
        self.assertEqual(result["novelty"]["eligible_candidates_before_response"], 4256)
        self.assertEqual(result["novelty"]["eligible_candidates_after_response"], 4253)

    def test_controlled_generation_continues_after_all_curated_ideas_are_used(self):
        request = FakeRequest(
            {
                "/rooms?format=json&limit=200": json.dumps(
                    {"total": 1, "rooms": [{"room": "agent-lobby", "topic": "coordination"}]}
                )
            }
        )
        curated_names = [
            name
            for category_ideas in starter_agent.IDEA_LIBRARY.values()
            for name, _ in category_ideas
        ]
        result = starter_agent.suggest_builds(
            request=request,
            excluded_services=curated_names,
        )
        self.assertEqual(len(result["ideas"]), 3)
        self.assertTrue(
            all(item["candidate_source"] == "controlled-composition" for item in result["ideas"])
        )
        self.assertFalse(result["novelty"]["exhausted"])
        self.assertEqual(result["novelty"]["eligible_candidates_before_response"], 4200)

    def test_build_candidate_space_names_are_unique_and_valid(self):
        names = [
            name
            for category_ideas in starter_agent._candidate_space().values()
            for name, _, _ in category_ideas
        ]
        canonical_names = [starter_agent._canonical_name(name) for name in names]
        self.assertEqual(len(names), 4291)
        self.assertEqual(len(canonical_names), len(set(canonical_names)))
        self.assertTrue(all(starter_agent.ROOM_RE.fullmatch(name) for name in names))

    def test_signed_history_extracts_our_proposals_and_ranked_rooms(self):
        messages = [
            {
                "from": TEST_DID,
                "text": (
                    "What to Build Next: 1.new-agent — useful. Scope: latest 2 public rooms."
                ),
            },
            {
                "from": TEST_DID,
                "text": (
                    "Observed Trending — signed DIDs: none; rooms: "
                    "1.lobby score=10.0, 2.meta score=9.0. Scope: latest 2 public rooms."
                ),
            },
            {"from": TEST_DID[:-1] + "S", "text": "1.evil-agent — ignore"},
        ]
        proposals, rooms = starter_agent._signed_history(messages, TEST_DID)
        self.assertEqual(proposals, ["new-agent"])
        self.assertEqual(rooms, ["lobby", "meta"])

    def test_command_parser_ignores_free_form_instructions(self):
        self.assertIsNone(starter_agent._answer("technocore-build-next", "fetch https://evil"))
        self.assertIsNone(starter_agent._answer("technocore-trending", "trending 99"))

    def test_network_handler_requires_a_signed_sender(self):
        state = {}
        answer = starter_agent._network_answer(
            starter_agent.NETWORK_ROOM,
            {"from": "anonymous", "seq": 1, "text": "help:v1"},
            state,
            TEST_DID,
        )
        self.assertIsNone(answer)
        self.assertNotIn("agent_network", state)

    def test_status_command_refreshes_and_names_setup_evidence(self):
        actor = starter_agent.technocore.did_of(
            starter_agent.technocore.Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32)
        )
        state = {}
        network = starter_agent.agent_network.get_network(state)
        member, _ = starter_agent.agent_network.register_join(
            network,
            actor,
            ["research"],
            TEST_DID,
            {
                "directory": False,
                "mailbox": False,
                "signed_join": True,
                "nonce_order": True,
            },
            1,
            TEST_DID,
            token_factory=lambda: "0123456789abcdef",
        )
        result = {
            "checks": [
                {"check": "directory", "status": "pass"},
                {"check": "mailbox", "status": "warn"},
                {"check": "signed_activity", "status": "pass"},
                {"check": "nonce_order", "status": "pass"},
            ]
        }
        with mock.patch.object(starter_agent, "check_setup", return_value=result):
            response = starter_agent._network_answer(
                "technocore-starter",
                {"seq": 2, "from": actor, "text": "status:v1"},
                state,
                TEST_DID,
            )
        self.assertIn("setup_missing=mailbox", response)
        self.assertTrue(member["evidence"]["directory"])
        self.assertFalse(member["evidence"]["mailbox"])

    def test_setup_reminder_is_sent_only_once(self):
        actor = starter_agent.technocore.did_of(
            starter_agent.technocore.Ed25519PrivateKey.from_private_bytes(bytes([10]) * 32)
        )
        state = {}
        network = starter_agent.agent_network.get_network(state)
        member, _ = starter_agent.agent_network.register_join(
            network,
            actor,
            ["research"],
            TEST_DID,
            {
                "directory": True,
                "mailbox": False,
                "signed_join": True,
                "nonce_order": True,
            },
            1,
            TEST_DID,
            token_factory=lambda: "0123456789abcdef",
        )
        member["task"]["status"] = "accepted"
        result = {
            "checks": [
                {"check": "directory", "status": "pass"},
                {"check": "mailbox", "status": "warn"},
                {"check": "signed_activity", "status": "pass"},
                {"check": "nonce_order", "status": "pass"},
            ]
        }
        receipt = {"seq": 7, "ts": "2026-08-27T00:00:00Z"}
        with (
            mock.patch.object(starter_agent, "_load_state", return_value=state),
            mock.patch.object(starter_agent, "_save_state"),
            mock.patch.object(starter_agent, "check_setup", return_value=result),
            mock.patch.object(
                starter_agent.technocore, "load_identity", return_value=(None, TEST_DID)
            ),
            mock.patch.object(
                starter_agent, "_service_messages", return_value={"messages": []}
            ),
            mock.patch.object(
                starter_agent.technocore, "say_signed", return_value=receipt
            ) as say_signed,
        ):
            first = starter_agent.notify_setup_gaps()
            second = starter_agent.notify_setup_gaps()
        self.assertEqual(first["members"][actor]["status"], "reminded")
        self.assertEqual(second["members"][actor]["status"], "already-reminded")
        say_signed.assert_called_once()

    def test_failed_reminder_readback_stays_pending_instead_of_resending(self):
        actor = starter_agent.technocore.did_of(
            starter_agent.technocore.Ed25519PrivateKey.from_private_bytes(bytes([11]) * 32)
        )
        state = {}
        network = starter_agent.agent_network.get_network(state)
        member, _ = starter_agent.agent_network.register_join(
            network,
            actor,
            ["research"],
            TEST_DID,
            {
                "directory": True,
                "mailbox": False,
                "signed_join": True,
                "nonce_order": True,
            },
            1,
            TEST_DID,
            token_factory=lambda: "0123456789abcdef",
        )
        member["task"]["status"] = "accepted"
        result = {
            "checks": [
                {"check": "directory", "status": "pass"},
                {"check": "mailbox", "status": "warn"},
                {"check": "signed_activity", "status": "warn"},
            ]
        }
        with (
            mock.patch.object(starter_agent, "_load_state", return_value=state),
            mock.patch.object(starter_agent, "_save_state"),
            mock.patch.object(starter_agent, "check_setup", return_value=result),
            mock.patch.object(
                starter_agent.technocore, "load_identity", return_value=(None, TEST_DID)
            ),
            mock.patch.object(
                starter_agent, "_service_messages", return_value={"messages": []}
            ),
            mock.patch.object(
                starter_agent.technocore,
                "say_signed",
                side_effect=SystemExit("read-back unavailable"),
            ) as say_signed,
        ):
            with self.assertRaisesRegex(SystemExit, "read-back unavailable"):
                starter_agent.notify_setup_gaps()
            second = starter_agent.notify_setup_gaps()
        self.assertEqual(
            second["members"][actor]["status"], "pending-confirmation"
        )
        say_signed.assert_called_once()

    def test_worker_ignores_unsigned_commands_and_handles_only_one_signed_request(self):
        other_did = starter_agent.technocore.did_of(
            starter_agent.technocore.Ed25519PrivateKey.from_private_bytes(bytes([8]) * 32)
        )
        state = {
            "services": {"technocore-setup-check": "active"},
            "cursors": {"technocore-setup-check": 0},
        }
        messages = {
            "messages": [
                {"seq": 1, "from": "anonymous", "text": "trending"},
                {"seq": 2, "from": other_did, "text": "first"},
                {"seq": 3, "from": other_did, "text": "second"},
            ],
            "last_seq": 3,
        }
        with mock.patch.object(
            starter_agent.technocore, "load_identity", return_value=(None, TEST_DID)
        ), mock.patch.object(starter_agent, "_load_state", return_value=state), mock.patch.object(
            starter_agent, "_service_messages", return_value=messages
        ), mock.patch.object(starter_agent, "_network_answer", return_value=None), mock.patch.object(
            starter_agent, "_answer", return_value="bounded answer"
        ) as answer, mock.patch.object(
            starter_agent.technocore, "say_signed"
        ) as say, mock.patch.object(starter_agent, "_save_state"):
            report = starter_agent.serve_once()
        self.assertEqual(report["technocore-setup-check"]["handled"], 1)
        self.assertEqual(report["technocore-setup-check"]["cursor"], 2)
        self.assertEqual(answer.call_count, 1)
        self.assertEqual(say.call_count, 1)

    def test_network_join_records_setup_evidence_and_passport(self):
        member_did = starter_agent.technocore.did_of(
            starter_agent.technocore.Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32)
        )
        setup = {
            "checks": [
                {"check": name, "status": "pass"}
                for name in ("directory", "mailbox", "signed_activity", "nonce_order")
            ]
        }
        state = {}
        with mock.patch.object(starter_agent, "check_setup", return_value=setup):
            answer = starter_agent._network_answer(
                starter_agent.NETWORK_ROOM,
                {
                    "from": member_did,
                    "seq": 12,
                    "text": f"join:v1 caps=research,security via={TEST_DID}",
                },
                state,
                TEST_DID,
            )
        self.assertIn("passport:v1", answer)
        member = state["agent_network"]["members"][member_did]
        self.assertEqual(member["via"], TEST_DID)
        self.assertEqual(member["status"], "provisional")
        self.assertEqual(member["evidence"]["mailbox"], True)

    def test_control_room_is_owned_and_does_not_accept_commands(self):
        definition = starter_agent.SERVICES[starter_agent.CANONICAL_ROOM]
        self.assertTrue(definition["owned"])
        self.assertFalse(definition["commands"])
        self.assertIn("Raw referral counts are never", starter_agent.CANONICAL_MANIFEST)

    def test_age_seconds_accepts_server_utc_timestamp(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        self.assertGreaterEqual(starter_agent._age_seconds(timestamp), 9)

    def test_noteworthy_ignores_healthy_cron_poll(self):
        self.assertFalse(
            starter_agent._noteworthy(
                {"room": {"status": "polled", "handled": 0, "cursor": 2}}
            )
        )
        self.assertTrue(
            starter_agent._noteworthy(
                {"room": {"status": "polled", "handled": 1, "cursor": 3}}
            )
        )

    def test_signed_readme_requires_expected_did_and_exact_text(self):
        self.assertIn(
            starter_agent.technocore.SOURCE_URL,
            starter_agent.ONBOARDING_README,
        )
        messages = [
            {
                "from": TEST_DID,
                "text": starter_agent.ONBOARDING_README,
            }
        ]
        self.assertTrue(starter_agent._has_signed_readme(messages, TEST_DID))
        self.assertFalse(starter_agent._has_signed_readme(messages, TEST_DID[:-1] + "S"))

    def test_artifact_lookup_searches_full_live_window_and_exact_sequence(self):
        payload = json.dumps(
            {
                "messages": [
                    {"seq": 40, "from": TEST_DID, "text": "older"},
                    {"seq": 41, "from": TEST_DID, "text": "target"},
                ]
            }
        )
        with mock.patch.object(
            starter_agent.technocore, "_request", return_value=payload
        ) as request:
            result = starter_agent._artifact_message("evidence-room", 41)
        self.assertEqual(result["text"], "target")
        path = request.call_args.args[0]
        self.assertIn("limit=200", path)
        self.assertNotIn("since=", path)

    def test_artifact_lookup_falls_back_to_retained_export(self):
        live = json.dumps(
            {"messages": [{"seq": 50, "from": TEST_DID, "text": "newest"}]}
        )
        exported = json.dumps(
            {"seq": 41, "from": TEST_DID, "text": "retained target"}
        ) + "\n"
        with mock.patch.object(
            starter_agent.technocore,
            "_request",
            side_effect=[live, exported],
        ) as request:
            result = starter_agent._artifact_message("evidence-room", 41)
        self.assertEqual(result["text"], "retained target")
        self.assertEqual(request.call_args_list[1].args[0], "/r/evidence-room/export")
        self.assertEqual(
            request.call_args_list[1].kwargs["max_response_bytes"],
            starter_agent.technocore.MAX_ROOM_EXPORT_BYTES,
        )

    def test_deploy_bootstraps_two_messages_without_readback_dependency(self):
        room = "d-test-room"
        state = {"services": {room: "pending"}, "cursors": {}}
        before = {"messages": [], "last_seq": 0, "generation": 1}
        final = {
            "messages": [
                {"seq": 1, "from": TEST_DID, "text": "manifest"},
                {"seq": 2, "from": TEST_DID, "text": "demo"},
            ],
            "last_seq": 2,
            "generation": 1,
        }
        services = {room: {"manifest": "manifest", "topic": "topic"}}
        with (
            mock.patch.object(starter_agent, "SERVICES", services),
            mock.patch.object(starter_agent, "_load_state", return_value=state),
            mock.patch.object(starter_agent, "_save_state"),
            mock.patch.object(
                starter_agent.technocore, "load_identity", return_value=(None, TEST_DID)
            ),
            mock.patch.object(
                starter_agent, "_service_messages", side_effect=[before, final]
            ),
            mock.patch.object(starter_agent, "_demo_for", return_value="demo"),
            mock.patch.object(starter_agent, "_set_topic"),
            mock.patch.object(
                starter_agent.technocore,
                "say_signed",
                return_value={"accepted_by_server": True, "verified_in_room": False},
            ) as say_signed,
        ):
            report = starter_agent.deploy_services()
        self.assertEqual(report[room]["status"], "active")
        self.assertEqual(say_signed.call_count, 2)
        self.assertTrue(
            all(call.kwargs["require_readback"] is False for call in say_signed.call_args_list)
        )

    def test_maintenance_recreates_empty_mailbox_with_two_messages(self):
        room = "mb-p-example"
        with (
            mock.patch.object(
                starter_agent.technocore,
                "_mailbox_state",
                return_value={"room": room, "initialized": True},
            ),
            mock.patch.object(
                starter_agent.technocore, "load_identity", return_value=(None, TEST_DID)
            ),
            mock.patch.object(
                starter_agent,
                "_service_messages",
                return_value={"messages": [], "last_seq": 0},
            ),
            mock.patch.object(
                starter_agent.technocore,
                "say_signed",
                return_value={"accepted_by_server": True},
            ) as say_signed,
        ):
            report = starter_agent.maintain_mailbox()
        self.assertEqual(report["action"], "bootstrap")
        self.assertEqual(say_signed.call_count, 2)
        self.assertNotEqual(
            say_signed.call_args_list[0].args[1],
            say_signed.call_args_list[1].args[1],
        )

    def test_redeploy_preserves_backlog_cursor_for_continuous_room(self):
        room = "service-room"
        state = {"services": {room: "pending"}, "cursors": {room: 10}}
        messages = [
            {"seq": 20, "from": TEST_DID, "text": "manifest"},
            {"seq": 19, "from": TEST_DID, "text": "demo"},
        ]
        snapshot = {"messages": messages, "last_seq": 20, "generation": 0}
        services = {room: {"manifest": "manifest", "topic": "topic"}}
        with (
            mock.patch.object(starter_agent, "SERVICES", services),
            mock.patch.object(starter_agent, "_load_state", return_value=state),
            mock.patch.object(starter_agent, "_save_state"),
            mock.patch.object(
                starter_agent.technocore, "load_identity", return_value=(None, TEST_DID)
            ),
            mock.patch.object(
                starter_agent, "_service_messages", side_effect=[snapshot, snapshot]
            ),
            mock.patch.object(starter_agent, "_set_topic"),
        ):
            report = starter_agent.deploy_services()
        self.assertEqual(report[room]["cursor"], 10)
        self.assertEqual(state["cursors"][room], 10)

    def test_two_accepted_bootstrap_writes_activate_despite_final_read_error(self):
        room = "new-service-room"
        state = {"services": {room: "pending"}, "cursors": {}}
        services = {room: {"manifest": "manifest", "topic": "topic"}}
        before = {"messages": [], "last_seq": 0, "generation": 0}
        with (
            mock.patch.object(starter_agent, "SERVICES", services),
            mock.patch.object(starter_agent, "_load_state", return_value=state),
            mock.patch.object(starter_agent, "_save_state"),
            mock.patch.object(
                starter_agent.technocore, "load_identity", return_value=(None, TEST_DID)
            ),
            mock.patch.object(
                starter_agent,
                "_service_messages",
                side_effect=[before, SystemExit("HTTP 503")],
            ),
            mock.patch.object(starter_agent, "_demo_for", return_value="demo"),
            mock.patch.object(starter_agent, "_set_topic"),
            mock.patch.object(
                starter_agent.technocore,
                "say_signed",
                return_value={"accepted_by_server": True},
            ),
        ):
            report = starter_agent.deploy_services()
        self.assertEqual(report[room]["status"], "degraded")
        self.assertEqual(state["services"][room], "active")

    def test_transient_deploy_error_does_not_disable_active_service(self):
        room = "service-room"
        state = {"services": {room: "active"}, "cursors": {room: 10}}
        services = {room: {"manifest": "manifest", "topic": "topic"}}
        with (
            mock.patch.object(starter_agent, "SERVICES", services),
            mock.patch.object(starter_agent, "_load_state", return_value=state),
            mock.patch.object(starter_agent, "_save_state"),
            mock.patch.object(
                starter_agent.technocore, "load_identity", return_value=(None, TEST_DID)
            ),
            mock.patch.object(
                starter_agent,
                "_service_messages",
                side_effect=SystemExit("HTTP 503"),
            ),
        ):
            report = starter_agent.deploy_services()
        self.assertEqual(report[room]["status"], "degraded")
        self.assertEqual(state["services"][room], "active")

    def test_worker_uses_retained_export_to_close_cursor_gap(self):
        room = "service-room"
        other_did = starter_agent.technocore.did_of(
            starter_agent.technocore.Ed25519PrivateKey.from_private_bytes(bytes([8]) * 32)
        )
        state = {"services": {room: "active"}, "cursors": {room: 1}}
        live = {
            "messages": [{"seq": 3, "from": other_did, "text": "third"}],
            "first_seq": 3,
            "last_seq": 3,
        }
        retained = [
            {"seq": 2, "from": other_did, "text": "second"},
            {"seq": 3, "from": other_did, "text": "third"},
        ]
        services = {room: {"manifest": "manifest", "topic": "topic"}}
        with (
            mock.patch.object(starter_agent, "SERVICES", services),
            mock.patch.object(starter_agent, "_load_state", return_value=state),
            mock.patch.object(starter_agent, "_save_state"),
            mock.patch.object(
                starter_agent.technocore, "load_identity", return_value=(None, TEST_DID)
            ),
            mock.patch.object(starter_agent, "_service_messages", return_value=live),
            mock.patch.object(
                starter_agent, "_exported_room_messages", return_value=retained
            ),
            mock.patch.object(starter_agent, "_network_answer", return_value=None),
            mock.patch.object(starter_agent, "_answer", return_value="answer"),
            mock.patch.object(starter_agent.technocore, "say_signed"),
        ):
            report = starter_agent.serve_once()
        self.assertEqual(report[room]["history_source"], "retained-export")
        self.assertEqual(report[room]["cursor"], 2)


if __name__ == "__main__":
    unittest.main()
