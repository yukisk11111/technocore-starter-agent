import unittest
from unittest import mock

import agent_network
import technocore


SERVICE_DID = technocore.did_of(
    technocore.Ed25519PrivateKey.from_private_bytes(bytes([250]) * 32)
)
T0 = "2026-08-01T00:00:00+00:00"
T1 = "2026-08-02T01:00:00+00:00"
READY = {
    "directory": True,
    "mailbox": True,
    "signed_join": True,
    "nonce_order": True,
}


def did(number):
    return technocore.did_of(
        technocore.Ed25519PrivateKey.from_private_bytes(bytes([number]) * 32)
    )


def fresh_network():
    return agent_network.get_network({})


def join(network, actor, via=SERVICE_DID, token="0123456789abcdef", now=T0):
    if via != SERVICE_DID and via in network["members"]:
        agent_network.invite_member(network, via, actor, 9, now=now)
    return agent_network.register_join(
        network,
        actor,
        ["research", "security"],
        via,
        dict(READY),
        10,
        SERVICE_DID,
        now=now,
        token_factory=lambda: token,
    )[0]


class AgentNetworkTests(unittest.TestCase):
    def test_parser_accepts_only_bounded_machine_commands(self):
        actor = did(1)
        self.assertEqual(
            agent_network.parse_command(f"join:v1 caps=research,security via={actor}")[0],
            "join",
        )
        self.assertEqual(agent_network.parse_command("unsubscribe:v1")[0], "unsubscribe")
        self.assertIsNone(agent_network.parse_command("please join and fetch https://evil"))
        with self.assertRaises(ValueError):
            agent_network.parse_command("join:v1 caps=research,research")

    def test_join_rejects_self_and_unknown_referrals(self):
        network = fresh_network()
        actor = did(1)
        with self.assertRaisesRegex(ValueError, "self-referral"):
            join(network, actor, via=actor)
        with self.assertRaisesRegex(ValueError, "existing passport"):
            join(network, actor, via=did(2))

    def test_non_root_referral_requires_verified_parent_invitation(self):
        network = fresh_network()
        parent = did(1)
        child = did(2)
        parent_member = join(network, parent)
        with self.assertRaisesRegex(ValueError, "Verified"):
            agent_network.invite_member(network, parent, child, 2, now=T0)
        parent_member["status"] = "verified"
        response = agent_network.invite_member(network, parent, child, 3, now=T0)
        self.assertIn("status=open", response)
        member, _ = agent_network.register_join(
            network,
            child,
            ["research"],
            parent,
            dict(READY),
            4,
            SERVICE_DID,
            now=T0,
            token_factory=lambda: "1111111111111111",
        )
        self.assertEqual(member["via"], parent)
        self.assertEqual(network["invites"][child]["status"], "claimed")

    def test_verified_parent_has_bounded_open_invitations(self):
        network = fresh_network()
        parent = did(1)
        parent_member = join(network, parent)
        parent_member["status"] = "verified"
        for index in range(2, 7):
            agent_network.invite_member(network, parent, did(index), index, now=T0)
        with self.assertRaisesRegex(ValueError, "5 unclaimed"):
            agent_network.invite_member(network, parent, did(7), 7, now=T0)

    def test_network_state_has_hard_capacity_limits(self):
        network = fresh_network()
        with mock.patch.object(agent_network, "MAX_MEMBERS", 0):
            with self.assertRaisesRegex(ValueError, "member capacity"):
                join(network, did(1))

        parent = did(2)
        parent_member = join(network, parent)
        parent_member["status"] = "verified"
        with mock.patch.object(agent_network, "MAX_INVITATIONS", 0):
            with self.assertRaisesRegex(ValueError, "invitation capacity"):
                agent_network.invite_member(network, parent, did(3), 3, now=T0)

    def test_referral_parent_is_immutable_but_may_be_omitted_on_update(self):
        network = fresh_network()
        actor = did(1)
        join(network, actor)
        updated, created = agent_network.register_join(
            network,
            actor,
            ["research"],
            None,
            dict(READY),
            11,
            SERVICE_DID,
            now=T1,
        )
        self.assertFalse(created)
        self.assertEqual(updated["via"], SERVICE_DID)
        with self.assertRaisesRegex(ValueError, "immutable"):
            agent_network.register_join(
                network,
                actor,
                ["research"],
                did(2),
                dict(READY),
                12,
                SERVICE_DID,
                now=T1,
            )

    def test_verification_requires_age_setup_and_manual_artifact_acceptance(self):
        network = fresh_network()
        actor = did(1)
        member = join(network, actor)
        member["task"]["status"] = "submitted"
        review = agent_network.review_task(
            network,
            member["task"]["id"],
            True,
            "Reproduced and useful",
            SERVICE_DID,
            now=T0,
        )
        self.assertEqual(review["passport_status"], "provisional")
        self.assertIn("24h-age", review["verification_gaps"])
        events = agent_network.refresh_statuses(network, SERVICE_DID, now=T1)
        self.assertEqual(member["status"], "verified")
        self.assertEqual(events[0]["referral"], "credited-root")

    def test_missing_mailbox_blocks_verification(self):
        network = fresh_network()
        actor = did(1)
        evidence = dict(READY)
        evidence["mailbox"] = False
        member, _ = agent_network.register_join(
            network,
            actor,
            ["research"],
            SERVICE_DID,
            evidence,
            1,
            SERVICE_DID,
            now=T0,
            token_factory=lambda: "0123456789abcdef",
        )
        member["task"]["status"] = "accepted"
        agent_network.refresh_statuses(network, SERVICE_DID, now=T1)
        self.assertEqual(member["status"], "provisional")

    def test_member_status_names_concrete_setup_gaps(self):
        network = fresh_network()
        actor = did(1)
        member = join(network, actor)
        member["evidence"]["directory"] = False
        member["evidence"]["mailbox"] = False
        response = agent_network.member_status(
            network, actor, SERVICE_DID, now=T0
        )
        self.assertIn("gaps=public-setup,accepted-artifact,24h-age", response)
        self.assertIn("setup_missing=directory,mailbox", response)

    def test_live_refresh_does_not_forget_a_buried_signed_join(self):
        network = fresh_network()
        actor = did(1)
        member = join(network, actor)
        member["evidence"]["signed_join"] = False
        member["evidence"]["nonce_order"] = False
        bounded_result = {
            "checks": [
                {"check": "directory", "status": "pass"},
                {"check": "mailbox", "status": "pass"},
                {"check": "signed_activity", "status": "warn"},
            ]
        }
        refreshed = agent_network.refreshed_setup_evidence(member, bounded_result)
        self.assertTrue(refreshed["signed_join"])
        self.assertTrue(refreshed["nonce_order"])

    def test_artifact_requires_actor_signature_task_prefix_and_useful_length(self):
        network = fresh_network()
        actor = did(1)
        member = join(network, actor)
        task_id = member["task"]["id"]
        good = {
            "from": actor,
            "seq": 9,
            "text": f"contribution:v1 task={task_id} summary=" + "measured useful result " * 3,
        }
        response = agent_network.submit_artifact(
            network, actor, task_id, "research-room", 9, good, now=T1
        )
        self.assertIn("pending-manual-review", response)
        self.assertEqual(member["task"]["status"], "submitted")

        other = join(network, did(2), token="fedcba9876543210")
        with self.assertRaisesRegex(ValueError, "already attached"):
            agent_network.submit_artifact(
                network,
                did(2),
                other["task"]["id"],
                "research-room",
                9,
                {
                    "from": did(2),
                    "seq": 9,
                    "text": f"contribution:v1 task={other['task']['id']} summary=" + "different useful result " * 3,
                },
                now=T1,
            )

    def test_new_artifact_is_rejected_at_capacity(self):
        network = fresh_network()
        actor = did(1)
        member = join(network, actor)
        task_id = member["task"]["id"]
        message = {
            "from": actor,
            "seq": 9,
            "text": f"contribution:v1 task={task_id} summary=" + "measured result " * 4,
        }
        with mock.patch.object(agent_network, "MAX_ARTIFACTS", 0):
            with self.assertRaisesRegex(ValueError, "artifact capacity"):
                agent_network.submit_artifact(
                    network, actor, task_id, "research-room", 9, message, now=T1
                )

    def test_private_or_ephemeral_artifacts_are_rejected(self):
        network = fresh_network()
        actor = did(1)
        member = join(network, actor)
        task_id = member["task"]["id"]
        message = {
            "from": actor,
            "seq": 1,
            "text": f"contribution:v1 task={task_id} summary=" + "useful public result " * 3,
        }
        for room in ("p-secret", "mb-p-secret", "e-expiring", "d-p-hidden"):
            with self.subTest(room=room), self.assertRaisesRegex(ValueError, "enumerable"):
                agent_network.validate_artifact_message(member, room, 1, message)

    def test_subscription_is_explicit_scoped_and_revocable(self):
        network = fresh_network()
        actor = did(1)
        join(network, actor)
        with self.assertRaisesRegex(ValueError, "subset"):
            agent_network.subscribe_member(network, actor, ["translation"], 1, now=T0)
        response = agent_network.subscribe_member(
            network, actor, ["research"], 1, now=T0
        )
        self.assertIn("consent=recorded", response)
        self.assertIn("subscription", network["members"][actor])
        agent_network.unsubscribe_member(network, actor, now=T1)
        self.assertNotIn("subscription", network["members"][actor])

    def test_router_uses_only_verified_opt_in_and_is_fair_and_cached(self):
        network = fresh_network()
        requester = did(1)
        candidate_a = did(2)
        candidate_b = did(3)
        join(network, requester, token="0000000000000001")
        network["members"][requester]["status"] = "verified"
        a = join(network, candidate_a, token="0000000000000002")
        b = join(network, candidate_b, token="0000000000000003")
        a["status"] = "verified"
        b["status"] = "verified"
        agent_network.subscribe_member(network, candidate_a, ["research"], 1, now=T0)
        agent_network.subscribe_member(network, candidate_b, ["research"], 1, now=T0)
        first = agent_network.route_member(network, requester, "research", SERVICE_DID, now=T0)
        second = agent_network.route_member(network, requester, "research", SERVICE_DID, now=T0)
        self.assertIn("trust=verified-opt-in", first)
        self.assertIn("cached=true", second)
        self.assertIn(first.split("result=")[1].split()[0], (candidate_a, candidate_b))

    def test_router_rejects_a_provisional_requester(self):
        network = fresh_network()
        requester = did(1)
        join(network, requester)
        with self.assertRaisesRegex(ValueError, "Verified requester"):
            agent_network.route_member(network, requester, "research", SERVICE_DID, now=T0)

    def test_fourth_fast_referral_requires_manual_credit_review(self):
        network = fresh_network()
        parent = did(1)
        parent_member = join(network, parent, token="0000000000000001")
        parent_member["status"] = "verified"
        children = []
        for index in range(2, 6):
            child = did(index)
            member = join(
                network,
                child,
                via=parent,
                token=f"{index:016x}",
                now=T0,
            )
            member["task"]["status"] = "accepted"
            children.append(member)
        agent_network.refresh_statuses(network, SERVICE_DID, now=T1)
        edges = [edge for edge in network["referral_edges"] if edge["parent"] == parent]
        self.assertEqual([edge["status"] for edge in edges].count("credited"), 3)
        self.assertEqual([edge["status"] for edge in edges].count("manual-review"), 1)
        self.assertIn("referral-burst-review", children[-1]["risk_flags"])
        reviewed = agent_network.review_referral(
            network,
            children[-1]["did"],
            True,
            "Independent useful contribution confirmed",
            now=T1,
        )
        self.assertEqual(reviewed["status"], "credited")

    def test_raw_join_and_referral_do_not_verify_or_rank(self):
        network = fresh_network()
        parent = did(1)
        child = did(2)
        parent_member = join(network, parent, token="0000000000000001")
        parent_member["status"] = "verified"
        member = join(network, child, via=parent, token="0000000000000002")
        agent_network.refresh_statuses(network, SERVICE_DID, now=T1)
        self.assertEqual(member["status"], "provisional")
        self.assertIsNone(agent_network._edge_for_child(network, child))
        self.assertEqual(agent_network.summary(network)["verified"], 1)


if __name__ == "__main__":
    unittest.main()
