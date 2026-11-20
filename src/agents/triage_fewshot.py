"""Triage few-shot retrieval — the Agent-1 side of Hermes Stage 1.

src/hermes is a *draft-reply* retriever (incoming → sent reply) used by Agent 4; it
does not carry triage labels. This module is its classification analogue for Agent 1:
it seeds a ChromaDB collection with independent (email → correct triage output)
examples, retrieves the k most similar ones for an incoming email, and renders them
into the `{{few_shot_examples}}` slot of skills/triage.md.

It plugs in via the injectable triage_runner — no change to the orchestrator or to
run_triage, both of which already accept the pieces this needs.

Seed examples are hand-written and deliberately DISJOINT from evaluation/golden_set
to keep evaluation honest (no train/test contamination).
"""

from __future__ import annotations

import json

from src.agents.triage import run_triage
from src.llm.base import LLMClient
from src.pipeline.schemas import CleanedEmail, TriageOutput
from src.rag.chroma_store import ChromaStore

COLLECTION = "triage_fewshot"

SEED_EXAMPLES: list[dict] = [
    {
        "id": "tf-invoice",
        "email": "From: ar@vendorco.com\nSubject: Invoice #4471 due in 3 days\n\nWe have not received payment for invoice #4471 ($1,820). Payment is due in 3 days; a 2% late fee applies after that.",
        "output": {"category": "finance", "summary": "A vendor says invoice #4471 for $1,820 is unpaid and due in 3 days.", "key_points": ["Invoice #4471 for $1,820 is unpaid", "Due in 3 days", "2% late fee applies after the due date"], "priority": "high", "spam_level": "not_spam", "reply_needed": False, "reply_type": "no_reply", "risk_flags": ["money", "deadline"], "recommended_action": "ask_user_check", "confidence": 0.9},
    },
    {
        "id": "tf-password-reset",
        "email": "From: security@dropbox.com\nSubject: Password reset requested\n\nSomeone requested a password reset for your account. If this wasn't you, you can safely ignore this email.",
        "output": {"category": "system", "summary": "Dropbox reports a password reset was requested for the account.", "key_points": ["A password reset was requested", "Advises ignoring if not initiated by the user"], "priority": "medium", "spam_level": "not_spam", "reply_needed": False, "reply_type": "no_reply", "risk_flags": ["account"], "recommended_action": "ask_user_check", "confidence": 0.85},
    },
    {
        "id": "tf-benefits",
        "email": "From: people@corp.com\nSubject: Action needed: confirm your benefits elections\n\nPlease confirm your 2026 medical and dental elections in Workday this week.",
        "output": {"category": "work", "summary": "HR asks the employee to confirm 2026 benefits elections in Workday.", "key_points": ["Confirm 2026 medical and dental elections", "Done in Workday this week"], "priority": "medium", "spam_level": "not_spam", "reply_needed": False, "reply_type": "no_reply", "risk_flags": ["hr"], "recommended_action": "ask_user_check", "confidence": 0.88},
    },
    {
        "id": "tf-nda",
        "email": "From: legal@partner.com\nSubject: NDA for signature\n\nAttached is the mutual NDA. Please review and sign before our call on Thursday.",
        "output": {"category": "work", "summary": "A partner sends a mutual NDA to review and sign before Thursday's call.", "key_points": ["Mutual NDA attached", "Review and sign before Thursday's call"], "priority": "high", "spam_level": "not_spam", "reply_needed": True, "reply_type": "must_review", "risk_flags": ["legal", "attachment", "deadline"], "recommended_action": "ask_user_check", "confidence": 0.9},
    },
    {
        "id": "tf-registrar",
        "email": "From: registrar@university.edu\nSubject: Enrollment verification updated\n\nYour enrollment status for the Fall term has been updated. Log in to the student portal for details.",
        "output": {"category": "school", "summary": "The registrar says the student's Fall enrollment status was updated.", "key_points": ["Fall enrollment status updated", "Details in the student portal"], "priority": "medium", "spam_level": "not_spam", "reply_needed": False, "reply_type": "no_reply", "risk_flags": ["school"], "recommended_action": "ask_user_check", "confidence": 0.85},
    },
    {
        "id": "tf-rent",
        "email": "From: billing@property.com\nSubject: Rent due June 1\n\nYour rent of $1,500 is due June 1. A late fee applies after the 5th.",
        "output": {"category": "finance", "summary": "A property manager says $1,500 rent is due June 1 with a late fee after the 5th.", "key_points": ["Rent of $1,500 due June 1", "Late fee after the 5th"], "priority": "high", "spam_level": "not_spam", "reply_needed": False, "reply_type": "no_reply", "risk_flags": ["money", "deadline"], "recommended_action": "ask_user_check", "confidence": 0.9},
    },
    {
        "id": "tf-newsletter",
        "email": "From: news@brandweekly.com\nSubject: Your weekly digest\n\nHere are this week's top articles. Read more on our site: https://brandweekly.com/digest",
        "output": {"category": "promotion", "summary": "A weekly newsletter links to this week's articles.", "key_points": ["Weekly digest of articles", "Links to the sender's site"], "priority": "low", "spam_level": "not_spam", "reply_needed": False, "reply_type": "no_reply", "risk_flags": ["external_link"], "recommended_action": "no_action", "confidence": 0.85},
    },
    {
        "id": "tf-casual",
        "email": "From: mom@gmail.com\nSubject: call me\n\nGive me a call when you get a chance this weekend.",
        "output": {"category": "personal", "summary": "The sender's mother asks for a call this weekend.", "key_points": ["Asks for a call this weekend"], "priority": "low", "spam_level": "not_spam", "reply_needed": True, "reply_type": "draft_later", "risk_flags": [], "recommended_action": "generate_draft", "confidence": 0.9},
    },
    {
        "id": "tf-standup",
        "email": "From: teammate@corp.com\nSubject: Notes from standup\n\nSharing today's standup notes for visibility. No action needed.",
        "output": {"category": "work", "summary": "A teammate shares standup notes for visibility with no action needed.", "key_points": ["Standup notes shared for visibility", "No action needed"], "priority": "low", "spam_level": "not_spam", "reply_needed": False, "reply_type": "no_reply", "risk_flags": [], "recommended_action": "no_action", "confidence": 0.9},
    },
    {
        "id": "tf-suspicious-renew",
        "email": "From: billing@account-update-center.click\nSubject: Confirm your subscription before it renews\n\nYour subscription is set to auto-renew. To review or update your plan, confirm your details here: http://account-update-center.click/confirm",
        "output": {"category": "spam", "summary": "An off-brand sender asks the user to confirm subscription details via a link before renewal.", "key_points": ["Claims a subscription will auto-renew", "Asks to confirm details at an external link"], "priority": "low", "spam_level": "suspicious", "reply_needed": False, "reply_type": "no_reply", "risk_flags": ["external_link"], "recommended_action": "ask_user_check", "confidence": 0.55},
    },
    {
        "id": "tf-spam-replica",
        "email": "From: deals@luxury-replica-outlet.click\nSubject: Replica watches 90% OFF today only\n\nGenuine-look replica watches at unbeatable prices. Shop now before they're gone: http://luxury-replica-outlet.click/shop",
        "output": {"category": "spam", "summary": "A junk sender advertises heavily discounted replica watches via an external link.", "key_points": ["Advertises replica watches at 90% off", "Links to an external shop"], "priority": "low", "spam_level": "definite_spam", "reply_needed": False, "reply_type": "no_reply", "risk_flags": ["external_link"], "recommended_action": "move_to_junk", "confidence": 0.95},
    },
    {
        "id": "tf-promo-sale",
        "email": "From: hello@rei.com\nSubject: Members' weekend: 30% off select gear\n\nOur members' weekend sale is on. Take 30% off select outdoor gear through Sunday: https://rei.com/sale",
        "output": {"category": "promotion", "summary": "A retailer announces a 30%-off members' weekend sale via a link.", "key_points": ["30% off select gear through Sunday", "Links to the sale page"], "priority": "low", "spam_level": "not_spam", "reply_needed": False, "reply_type": "no_reply", "risk_flags": ["external_link"], "recommended_action": "no_action", "confidence": 0.85},
    },
    {
        "id": "tf-system-summary",
        "email": "From: digest@notion.so\nSubject: Your weekly workspace summary\n\nHere's what happened in your workspace this week: 12 pages edited, 3 new comments. No action needed.",
        "output": {"category": "system", "summary": "A SaaS product sends a weekly workspace activity summary with no action needed.", "key_points": ["Weekly workspace activity summary", "No action needed"], "priority": "low", "spam_level": "not_spam", "reply_needed": False, "reply_type": "no_reply", "risk_flags": [], "recommended_action": "no_action", "confidence": 0.9},
    },
    {
        "id": "tf-legal-demand",
        "email": "From: counsel@adverse-party.com\nSubject: Cease and desist — response required by June 30\n\nThis letter serves as formal notice. We require your written response to the attached cease-and-desist by June 30.",
        "output": {"category": "work", "summary": "Opposing counsel sends a cease-and-desist demanding a written response by June 30.", "key_points": ["Formal cease-and-desist notice", "Written response required by June 30", "Document attached"], "priority": "high", "spam_level": "not_spam", "reply_needed": True, "reply_type": "must_review", "risk_flags": ["legal", "attachment", "deadline"], "recommended_action": "ask_user_check", "confidence": 0.9},
    },
]


def _render_block(email: str, output: dict) -> str:
    return f"Input:\n{email}\nOutput:\n{json.dumps(output, ensure_ascii=False)}"


def seed_store(store: ChromaStore) -> None:
    existing = store.existing_ids()
    new = [e for e in SEED_EXAMPLES if e["id"] not in existing]
    if not new:
        return
    store.add(
        ids=[e["id"] for e in new],
        documents=[e["email"] for e in new],
        metadatas=[{"output": json.dumps(e["output"], ensure_ascii=False)} for e in new],
    )


class TriageFewShotRetriever:
    def __init__(self, store: ChromaStore, k: int = 3) -> None:
        self.store = store
        self.k = k

    def render(self, incoming_body: str) -> str:
        hits = self.store.query(text=incoming_body, k=self.k)
        blocks = [
            f"### Retrieved example {i}\n{_render_block(h['document'], json.loads(h['metadata']['output']))}"
            for i, h in enumerate(hits, 1)
        ]
        return "\n\n".join(blocks)


def build_seeded_store(persist_dir: str = "data/triage_fewshot") -> ChromaStore:
    store = ChromaStore(COLLECTION, persist_dir=persist_dir)
    seed_store(store)
    return store


def make_fewshot_triage_runner(llm_client: LLMClient, store: ChromaStore, k: int = 3):
    retriever = TriageFewShotRetriever(store, k)

    async def _runner(email: CleanedEmail) -> TriageOutput:
        examples = retriever.render(email.body)
        return await run_triage(email, llm_client=llm_client, few_shot_examples=examples)

    return _runner
