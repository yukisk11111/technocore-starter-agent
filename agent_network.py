#!/usr/bin/env python3
"""Permissioned Technocore passport, subscription, routing, and referral state."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Callable


DID_RE = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}\Z")
TAG_RE = re.compile(r"[a-z][a-z0-9-]{0,23}\Z")
ROOM_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")
TASK_RE = re.compile(r"[0-9a-f]{16}\Z")
MAX_TAGS = 5
MIN_VERIFICATION_AGE_SECONDS = 24 * 60 * 60
REFERRAL_WINDOW_SECONDS = 7 * 24 * 60 * 60
AUTO_REFERRAL_CREDITS_PER_WINDOW = 3
INVITE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_OPEN_INVITES = 5
ROUTE_COOLDOWN_SECONDS = 60 * 60
MIN_CONTRIBUTION_SUMMARY_CHARS = 40


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _age_seconds(value: str, now: str) -> float:
    return max(_timestamp(now) - _timestamp(value), 0)


def _parse_tags(raw: str) -> list[str]:
    tags = raw.split(",")
    if not 1 <= len(tags) <= MAX_TAGS:
        raise ValueError(f"expected 1-{MAX_TAGS} comma-separated tags")
    if any(not TAG_RE.fullmatch(tag) for tag in tags):
        raise ValueError("tags must match [a-z][a-z0-9-]{0,23}")
    if len(set(tags)) != len(tags):
        raise ValueError("duplicate tags are not allowed")
    return tags


def parse_command(text: str) -> tuple[str, dict[str, Any]] | None:
    if text == "help:v1":
        return "help", {}
    match = re.fullmatch(
        r"join:v1 caps=([a-z0-9,-]+)(?: via=(did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}))?",
        text,
    )
    if match:
        return "join", {"caps": _parse_tags(match.group(1)), "via": match.group(2)}
    match = re.fullmatch(r"subscribe:v1 topics=([a-z0-9,-]+) max=([12])/day", text)
    if match:
        return "subscribe", {
            "topics": _parse_tags(match.group(1)),
            "max_per_day": int(match.group(2)),
        }
    if text == "unsubscribe:v1":
        return "unsubscribe", {}
    match = re.fullmatch(r"route:v1 need=([a-z][a-z0-9-]{0,23})", text)
    if match:
        return "route", {"need": match.group(1)}
    match = re.fullmatch(r"invite:v1 child=(did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44})", text)
    if match:
        return "invite", {"child": match.group(1)}
    if text == "status:v1":
        return "status", {}
    match = re.fullmatch(
        r"submit:v1 task=([0-9a-f]{16}) room=([a-z0-9][a-z0-9_-]{0,47}) seq=([1-9][0-9]*)",
        text,
    )
    if match:
        return "submit", {
            "task_id": match.group(1),
            "room": match.group(2),
            "seq": int(match.group(3)),
        }
    return None


def get_network(state: dict[str, Any]) -> dict[str, Any]:
    network = state.setdefault(
        "agent_network",
        {
            "version": 1,
            "members": {},
            "artifacts": {},
            "invites": {},
            "referral_edges": [],
        },
    )
    if not isinstance(network, dict):
        raise ValueError("agent_network state is not an object")
    network.setdefault("version", 1)
    network.setdefault("members", {})
    network.setdefault("artifacts", {})
    network.setdefault("invites", {})
    network.setdefault("referral_edges", [])
    if network["version"] != 1:
        raise ValueError("unsupported agent_network state version")
    if not isinstance(network["members"], dict):
        raise ValueError("agent_network members is not an object")
    if not isinstance(network["artifacts"], dict):
        raise ValueError("agent_network artifacts is not an object")
    if not isinstance(network["invites"], dict):
        raise ValueError("agent_network invites is not an object")
    if not isinstance(network["referral_edges"], list):
        raise ValueError("agent_network referral_edges is not a list")
    return network


def setup_evidence(result: dict[str, Any]) -> dict[str, bool]:
    checks = {
        str(item.get("check")): item.get("status") == "pass"
        for item in result.get("checks", [])
        if isinstance(item, dict)
    }
    return {
        "directory": bool(checks.get("directory")),
        "mailbox": bool(checks.get("mailbox")),
        "signed_join": bool(checks.get("signed_activity")),
        "nonce_order": bool(checks.get("nonce_order")),
    }


def _setup_ready(member: dict[str, Any]) -> bool:
    evidence = member.get("evidence", {})
    return all(
        evidence.get(name) is True
        for name in ("directory", "mailbox", "signed_join", "nonce_order")
    )


def _passport_id(did: str) -> str:
    return hashlib.sha256(f"passport:v1|{did}".encode("utf-8")).hexdigest()[:16]


def _would_cycle(members: dict[str, Any], child: str, parent: str) -> bool:
    seen: set[str] = set()
    current: str | None = parent
    while current and current not in seen:
        if current == child:
            return True
        seen.add(current)
        record = members.get(current)
        current = str(record.get("via")) if isinstance(record, dict) and record.get("via") else None
    return False


def _recent_children(
    members: dict[str, Any], parent: str, now: str, seconds: int = 24 * 60 * 60
) -> int:
    count = 0
    for member in members.values():
        if not isinstance(member, dict) or member.get("via") != parent:
            continue
        try:
            if _age_seconds(str(member["joined_at"]), now) <= seconds:
                count += 1
        except (KeyError, TypeError, ValueError):
            continue
    return count


def _new_task(
    did: str,
    now: str,
    token_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    token_factory = token_factory or (lambda: secrets.token_hex(8))
    task_id = token_factory()
    if not TASK_RE.fullmatch(task_id):
        raise ValueError("task token factory must return 16 lowercase hex characters")
    return {
        "id": task_id,
        "status": "assigned",
        "issued_at": now,
        "instruction": (
            f"Post a signed public message beginning 'contribution:v1 task={task_id} "
            "summary=' followed by a reproducible useful finding of at least 40 characters; "
            f"then send submit:v1 task={task_id} room=<room> seq=<seq>."
        ),
    }


def invite_member(
    network: dict[str, Any],
    parent: str,
    child: str,
    request_seq: int,
    *,
    now: str | None = None,
) -> str:
    now = now or now_iso()
    parent_member = network["members"].get(parent)
    if not isinstance(parent_member, dict) or parent_member.get("status") != "verified":
        raise ValueError("only a Verified passport may issue referral invitations")
    if child == parent:
        raise ValueError("self-invitation is not allowed")
    if child in network["members"]:
        raise ValueError("child already has a passport")
    current = network["invites"].get(child)
    if isinstance(current, dict):
        if current.get("parent") != parent:
            raise ValueError("child already has an invitation from another parent")
        if current.get("status") == "open":
            try:
                still_open = _age_seconds(str(current["created_at"]), now) <= INVITE_TTL_SECONDS
            except (KeyError, TypeError, ValueError):
                still_open = False
            if still_open:
                return (
                    f"invite:v1 parent={parent} child={child} status=open expires=7d "
                    "single-child attribution; child must join with this parent DID."
                )
            current["status"] = "expired"
    open_invites = 0
    for invitation in network["invites"].values():
        if not isinstance(invitation, dict) or invitation.get("parent") != parent:
            continue
        if invitation.get("status") != "open":
            continue
        try:
            if _age_seconds(str(invitation["created_at"]), now) <= INVITE_TTL_SECONDS:
                open_invites += 1
        except (KeyError, TypeError, ValueError):
            continue
    if open_invites >= MAX_OPEN_INVITES:
        raise ValueError("referrer already has 5 unclaimed invitations")
    network["invites"][child] = {
        "parent": parent,
        "child": child,
        "status": "open",
        "created_at": now,
        "request_seq": request_seq,
    }
    return (
        f"invite:v1 parent={parent} child={child} status=open expires=7d "
        "single-child attribution; child must join with this parent DID."
    )


def register_join(
    network: dict[str, Any],
    actor: str,
    caps: list[str],
    via: str | None,
    evidence: dict[str, bool],
    request_seq: int,
    service_did: str,
    *,
    now: str | None = None,
    token_factory: Callable[[], str] | None = None,
) -> tuple[dict[str, Any], bool]:
    now = now or now_iso()
    if not DID_RE.fullmatch(actor):
        raise ValueError("join must come from a signed Ed25519 did:key")
    caps = _parse_tags(",".join(caps))
    members = network["members"]
    existing = members.get(actor)
    if existing is not None:
        if not isinstance(existing, dict):
            raise ValueError("existing member record is invalid")
        if via is not None and existing.get("via") != via:
            raise ValueError("referral parent is immutable after the first join")
        existing["caps"] = caps
        existing["evidence"] = evidence
        existing["evidence_checked_at"] = now
        existing["updated_at"] = now
        return existing, False

    if via == actor:
        raise ValueError("self-referral is not allowed")
    if via and via != service_did and via not in members:
        raise ValueError("referrer must be the service DID or an existing passport member")
    if via and _would_cycle(members, actor, via):
        raise ValueError("referral cycle is not allowed")
    invitation = None
    if via and via != service_did:
        invitation = network["invites"].get(actor)
        if not isinstance(invitation, dict) or invitation.get("parent") != via:
            raise ValueError("referrer must first sign invite:v1 for this child DID")
        if invitation.get("status") != "open":
            raise ValueError("referral invitation is not open")
        try:
            expired = _age_seconds(str(invitation["created_at"]), now) > INVITE_TTL_SECONDS
        except (KeyError, TypeError, ValueError):
            expired = True
        if expired:
            invitation["status"] = "expired"
            raise ValueError("referral invitation expired")

    risk_flags: list[str] = []
    if via and via != service_did and _recent_children(members, via, now) >= 3:
        risk_flags.append("referral-burst-review")
    member = {
        "passport_id": _passport_id(actor),
        "did": actor,
        "joined_at": now,
        "updated_at": now,
        "join_request_seq": request_seq,
        "caps": caps,
        "via": via,
        "evidence": evidence,
        "evidence_checked_at": now,
        "status": "provisional",
        "risk_flags": risk_flags,
        "task": _new_task(actor, now, token_factory),
        "route_count": 0,
    }
    members[actor] = member
    if invitation is not None:
        invitation["status"] = "claimed"
        invitation["claimed_at"] = now
    return member, True


def join_response(member: dict[str, Any], created: bool) -> str:
    evidence = member["evidence"]
    missing = [name for name, passed in evidence.items() if not passed]
    task = member["task"]
    return (
        f"passport:v1 id={member['passport_id']} member={member['did']} "
        f"status={member['status']} action={'issued' if created else 'updated'} "
        f"caps={','.join(member['caps'])} setup_missing={','.join(missing) or 'none'} "
        f"task={task['id']} anti_sybil=24h+mailbox+signed-join+manual-artifact-review; "
        "raw joins and referrals never determine ranking. "
        f"{task['instruction']}"
    )


def subscribe_member(
    network: dict[str, Any],
    actor: str,
    topics: list[str],
    max_per_day: int,
    *,
    now: str | None = None,
) -> str:
    now = now or now_iso()
    member = network["members"].get(actor)
    if not isinstance(member, dict):
        raise ValueError("join:v1 is required before subscribe:v1")
    topics = _parse_tags(",".join(topics))
    if not set(topics).issubset(set(member.get("caps", []))):
        raise ValueError("subscription topics must be a subset of registered capabilities")
    if max_per_day not in (1, 2):
        raise ValueError("max must be 1/day or 2/day")
    member["subscription"] = {
        "topics": topics,
        "max_per_day": max_per_day,
        "consented_at": now,
    }
    member["updated_at"] = now
    return (
        f"subscription:v1 member={actor} topics={','.join(topics)} max={max_per_day}/day "
        "consent=recorded revoke=unsubscribe:v1; "
        "no mailbox delivery occurs outside these declared topics and limit."
    )


def unsubscribe_member(
    network: dict[str, Any], actor: str, *, now: str | None = None
) -> str:
    now = now or now_iso()
    member = network["members"].get(actor)
    if not isinstance(member, dict):
        raise ValueError("no passport subscription exists")
    member.pop("subscription", None)
    member["updated_at"] = now
    return f"subscription:v1 member={actor} status=revoked; no future mailbox task delivery is authorized."


def validate_artifact_message(
    member: dict[str, Any], room: str, seq: int, message: dict[str, Any]
) -> str:
    task = member.get("task", {})
    task_id = str(task.get("id", ""))
    if message.get("from") != member.get("did"):
        raise ValueError("artifact message is not signed by the passport DID")
    if int(message.get("seq", 0)) != seq:
        raise ValueError("artifact sequence does not match")
    expected = f"contribution:v1 task={task_id} summary="
    text = str(message.get("text", ""))
    if not text.startswith(expected):
        raise ValueError("artifact must use the assigned contribution:v1 prefix")
    summary = text[len(expected) :].strip()
    if len(summary) < MIN_CONTRIBUTION_SUMMARY_CHARS:
        raise ValueError(
            f"artifact summary must be at least {MIN_CONTRIBUTION_SUMMARY_CHARS} characters"
        )
    classes: list[str] = []
    remainder = room
    while True:
        matched = False
        for prefix in ("mb-", "d-", "e-", "p-"):
            if remainder.startswith(prefix):
                classes.append(prefix[:-1])
                remainder = remainder[len(prefix) :]
                matched = True
                break
        if not matched:
            break
    if not ROOM_RE.fullmatch(room) or "p" in classes or "e" in classes:
        raise ValueError("artifact must be in an enumerable, non-private, non-ephemeral room")
    return summary


def submit_artifact(
    network: dict[str, Any],
    actor: str,
    task_id: str,
    room: str,
    seq: int,
    message: dict[str, Any],
    *,
    now: str | None = None,
) -> str:
    now = now or now_iso()
    member = network["members"].get(actor)
    if not isinstance(member, dict):
        raise ValueError("join:v1 is required before submit:v1")
    task = member.get("task", {})
    if task.get("id") != task_id:
        raise ValueError("task does not belong to this passport")
    if task.get("status") == "accepted":
        raise ValueError("task is already accepted")
    artifact_key = f"{room}:{seq}"
    prior = network["artifacts"].get(artifact_key)
    if prior and prior != task_id:
        raise ValueError("artifact sequence is already attached to another task")
    summary = validate_artifact_message(member, room, seq, message)
    network["artifacts"][artifact_key] = task_id
    task.update(
        {
            "status": "submitted",
            "submitted_at": now,
            "artifact": {"room": room, "seq": seq, "summary": summary},
        }
    )
    task.pop("review_reason", None)
    member["updated_at"] = now
    return (
        f"submission:v1 task={task_id} member={actor} artifact={artifact_key} "
        "status=pending-manual-review; submission alone grants no verification or referral credit."
    )


def _verification_gaps(member: dict[str, Any], now: str) -> list[str]:
    gaps: list[str] = []
    if not _setup_ready(member):
        gaps.append("public-setup")
    if member.get("task", {}).get("status") != "accepted":
        gaps.append("accepted-artifact")
    try:
        if _age_seconds(str(member["joined_at"]), now) < MIN_VERIFICATION_AGE_SECONDS:
            gaps.append("24h-age")
    except (KeyError, TypeError, ValueError):
        gaps.append("valid-join-time")
    return gaps


def _edge_for_child(network: dict[str, Any], child: str) -> dict[str, Any] | None:
    for edge in network["referral_edges"]:
        if isinstance(edge, dict) and edge.get("child") == child:
            return edge
    return None


def _recent_credited_edges(network: dict[str, Any], parent: str, now: str) -> int:
    count = 0
    for edge in network["referral_edges"]:
        if not isinstance(edge, dict) or edge.get("parent") != parent:
            continue
        if edge.get("status") != "credited":
            continue
        try:
            if _age_seconds(str(edge["decided_at"]), now) <= REFERRAL_WINDOW_SECONDS:
                count += 1
        except (KeyError, TypeError, ValueError):
            continue
    return count


def _create_or_update_referral_edge(
    network: dict[str, Any], member: dict[str, Any], service_did: str, now: str
) -> dict[str, Any] | None:
    parent = member.get("via")
    if not parent:
        return None
    existing = _edge_for_child(network, str(member["did"]))
    if existing:
        if existing.get("status") == "pending-parent":
            parent_member = network["members"].get(parent)
            if parent == service_did or (
                isinstance(parent_member, dict) and parent_member.get("status") == "verified"
            ):
                existing["status"] = "credited-root" if parent == service_did else "credited"
                existing["decided_at"] = now
        return existing
    if parent == service_did:
        status = "credited-root"
        reason = "bootstrap-source-attribution"
    else:
        parent_member = network["members"].get(parent)
        if not isinstance(parent_member, dict) or parent_member.get("status") != "verified":
            status = "pending-parent"
            reason = "referrer-not-yet-verified"
        elif "referral-burst-review" in member.get("risk_flags", []):
            status = "manual-review"
            reason = "referral-burst"
        elif _recent_credited_edges(network, str(parent), now) >= AUTO_REFERRAL_CREDITS_PER_WINDOW:
            status = "manual-review"
            reason = "weekly-auto-credit-cap"
        else:
            status = "credited"
            reason = "verified-child"
    edge = {
        "parent": parent,
        "child": member["did"],
        "status": status,
        "reason": reason,
        "created_at": now,
    }
    if status in ("credited", "credited-root"):
        edge["decided_at"] = now
    network["referral_edges"].append(edge)
    return edge


def refresh_statuses(
    network: dict[str, Any], service_did: str, *, now: str | None = None
) -> list[dict[str, Any]]:
    now = now or now_iso()
    events: list[dict[str, Any]] = []
    for member in network["members"].values():
        if not isinstance(member, dict):
            continue
        if member.get("status") != "verified" and not _verification_gaps(member, now):
            member["status"] = "verified"
            member["verified_at"] = now
            edge = _create_or_update_referral_edge(network, member, service_did, now)
            events.append(
                {
                    "event": "passport-verified",
                    "did": member["did"],
                    "passport_id": member["passport_id"],
                    "referral": edge.get("status") if edge else "none",
                }
            )
    for member in network["members"].values():
        if isinstance(member, dict) and member.get("status") == "verified":
            _create_or_update_referral_edge(network, member, service_did, now)
    return events


def review_task(
    network: dict[str, Any],
    task_id: str,
    accept: bool,
    reason: str,
    service_did: str,
    *,
    evidence: dict[str, bool] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    now = now or now_iso()
    reason = " ".join(reason.split())[:160]
    if not reason:
        raise ValueError("review reason is required")
    matched: dict[str, Any] | None = None
    for member in network["members"].values():
        if isinstance(member, dict) and member.get("task", {}).get("id") == task_id:
            matched = member
            break
    if matched is None:
        raise ValueError("unknown task id")
    task = matched["task"]
    if task.get("status") != "submitted":
        raise ValueError("task must be submitted before review")
    if evidence is not None:
        matched["evidence"] = evidence
        matched["evidence_checked_at"] = now
    task["status"] = "accepted" if accept else "rejected"
    task["reviewed_at"] = now
    task["review_reason"] = reason
    matched["updated_at"] = now
    events = refresh_statuses(network, service_did, now=now)
    return {
        "task_id": task_id,
        "member": matched["did"],
        "decision": task["status"],
        "passport_status": matched["status"],
        "verification_gaps": _verification_gaps(matched, now),
        "reason": reason,
        "events": events,
    }


def review_referral(
    network: dict[str, Any],
    child: str,
    accept: bool,
    reason: str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    now = now or now_iso()
    edge = _edge_for_child(network, child)
    if edge is None:
        raise ValueError("no referral edge for child")
    if edge.get("status") not in ("manual-review", "pending-parent"):
        raise ValueError("referral edge is not awaiting review")
    edge["status"] = "credited" if accept else "rejected"
    edge["reason"] = " ".join(reason.split())[:160]
    edge["decided_at"] = now
    return dict(edge)


def member_status(
    network: dict[str, Any], actor: str, service_did: str, *, now: str | None = None
) -> str:
    now = now or now_iso()
    member = network["members"].get(actor)
    if not isinstance(member, dict):
        raise ValueError("no passport; send join:v1 first")
    edges = [
        edge
        for edge in network["referral_edges"]
        if isinstance(edge, dict) and edge.get("parent") == actor
    ]
    credited = sum(edge.get("status") == "credited" for edge in edges)
    pending = sum(edge.get("status") in ("pending-parent", "manual-review") for edge in edges)
    gaps = _verification_gaps(member, now)
    subscription = member.get("subscription")
    subscribed = (
        f"{','.join(subscription['topics'])}@{subscription['max_per_day']}/day"
        if isinstance(subscription, dict)
        else "none"
    )
    return (
        f"passport-status:v1 id={member['passport_id']} member={actor} "
        f"status={member['status']} gaps={','.join(gaps) or 'none'} "
        f"caps={','.join(member['caps'])} subscription={subscribed} "
        f"verified_referrals={credited} pending_referrals={pending}; "
        f"share transparently as via={actor}. Raw referrals are not ranking signals."
    )


def route_member(
    network: dict[str, Any], actor: str, need: str, service_did: str, *, now: str | None = None
) -> str:
    now = now or now_iso()
    requester = network["members"].get(actor)
    if not isinstance(requester, dict):
        raise ValueError("join:v1 is required before route:v1")
    if requester.get("status") != "verified":
        raise ValueError("route:v1 requires a Verified requester")
    route_requests = requester.setdefault("route_requests", {})
    prior = route_requests.get(need)
    if isinstance(prior, dict):
        try:
            if _age_seconds(str(prior["at"]), now) < ROUTE_COOLDOWN_SECONDS:
                result = prior.get("candidate") or "none"
                return (
                    f"route:v1 need={need} result={result} cached=true cooldown=1h; "
                    "verified opt-in capability match only, not endorsement."
                )
        except (KeyError, TypeError, ValueError):
            pass
    candidates: list[dict[str, Any]] = []
    for did, member in network["members"].items():
        if did == actor or not isinstance(member, dict) or member.get("status") != "verified":
            continue
        subscription = member.get("subscription")
        if not isinstance(subscription, dict) or need not in subscription.get("topics", []):
            continue
        candidates.append(member)
    candidates.sort(
        key=lambda member: (
            int(member.get("route_count", 0)),
            str(member.get("last_routed_at", "")),
            str(member.get("passport_id", "")),
        )
    )
    candidate = candidates[0] if candidates else None
    candidate_did = str(candidate["did"]) if candidate else None
    route_requests[need] = {"at": now, "candidate": candidate_did}
    if candidate:
        candidate["route_count"] = int(candidate.get("route_count", 0)) + 1
        candidate["last_routed_at"] = now
        return (
            f"route:v1 need={need} result={candidate_did} passport={candidate['passport_id']} "
            "trust=verified-opt-in; read and independently verify the candidate DID note. "
            "Fair least-routed selection, not popularity or endorsement."
        )
    return (
        f"route:v1 need={need} result=none; no verified opt-in capability match. "
        "Provisional members are never routed."
    )


def summary(network: dict[str, Any]) -> dict[str, Any]:
    members = [member for member in network["members"].values() if isinstance(member, dict)]
    return {
        "members": len(members),
        "verified": sum(member.get("status") == "verified" for member in members),
        "provisional": sum(member.get("status") != "verified" for member in members),
        "subscribed": sum(isinstance(member.get("subscription"), dict) for member in members),
        "open_invitations": sum(
            isinstance(invitation, dict) and invitation.get("status") == "open"
            for invitation in network["invites"].values()
        ),
        "tasks_submitted": sum(member.get("task", {}).get("status") == "submitted" for member in members),
        "tasks_accepted": sum(member.get("task", {}).get("status") == "accepted" for member in members),
        "referrals_credited": sum(
            isinstance(edge, dict) and edge.get("status") == "credited"
            for edge in network["referral_edges"]
        ),
        "referrals_pending_review": sum(
            isinstance(edge, dict) and edge.get("status") in ("manual-review", "pending-parent")
            for edge in network["referral_edges"]
        ),
    }
