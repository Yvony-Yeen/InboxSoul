# Golden Set Changelog

Every change to golden-set content (`evaluation/golden_emails/`) or labels
(`evaluation/golden_set.jsonl`). The rules behind each label change are in
`evaluation/LABELING_GUIDE.md`. Results from before a change are not comparable with
results after it — re-run the evaluation.

## 2026-11-20

### Content
| Scope | Change | Reason |
|---|---|---|
| g-001 … g-017 (all `.eml`) | Real-looking recipient address in `To:` → `user@example.edu` | Public repo; remove a real-looking address. Brands and domains unchanged (legacy emails keep them by decision). |
| `tests/fixtures/raw_emails/*.eml` (5 files) | Same `To:` replacement | Same reason. No test asserted the address. |
| g-001, g-002, g-007, g-008, g-012, g-014 | Recipient's real first name in the greeting → fictional "Jordan" | Public repo; remove a real person's name. Only the greeting changes. |
| `tests/fixtures/mock_data.py` | Same greeting-name replacement in a mock email body | Same reason. Not part of the golden set; recorded here to keep all privacy edits in one place. |

### Labels
| id | Field | Before | After | Reason |
|---|---|---|---|---|
| g-006 | `category` | `finance` | `spam` | Impersonation emails are `spam`; their topic is carried by `risk_flags`. |
| g-007 | `risk_flags` | `account` | `account, external_link` | Labeling error: the body contains a link. `external_link` = any http/https link in the body (Q1). |
| g-010 | `risk_flags` | `external_link` | `external_link, deadline, credential_request` | "Within 48 hours the parcel will be returned" is a deadline (flags describe content, not authenticity; Q4). "Confirm your details" is a credential request (Q6). |
| g-012 | `risk_flags` | `legal, attachment, deadline` | `legal, deadline` | "Attached is…" with no real MIME attachment does not count as `attachment`. |
| g-015 | `risk_flags` | `school, deadline` | `money, school, deadline` | Financial aid counts as `money`. |

## 2026-11-20 (category rules)

### Labels
| id | Field | Before | After | Reason |
|---|---|---|---|---|
| g-005 | `category` | `promotion` | `spam` | Category rule 1: `definite_spam` → `spam`, even for marketing. |
| g-010 | `category` | `spam` | `system` | Only `definite_spam` is `spam`; this `suspicious` delivery notice fits no topic, so it is an automated institutional notice. |
| g-017 | `category` | `spam` | `work` | Only `definite_spam` is `spam`; this `suspicious` business proposal is categorized by its topic. |

### Structure
| Scope | Change | Reason |
|---|---|---|
| `golden_set.jsonl` (all 17 rows) | Added `"group": "legacy"` | Reports break results down by group; the synthetic set uses the other groups. |

`recommended_action` of all 17 legacy emails was re-checked against the §4a principle in
`LABELING_GUIDE.md`; none needed a change.
