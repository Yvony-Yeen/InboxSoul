# Known Issues

Open problems found during the evaluation work. None of them is fixed yet. The
action-routing issues (1–3) are planned to be fixed together in one change.

**Data source.** Unless stated otherwise, numbers come from the baseline evaluation run on
2026-11-20 at commit `928778c8` (`llama3.1:8b`, digest `46e0c10c039e`, Q4_K_M,
temperature 0, seed 42, triage prompt sha `2b60d0786797`, 47 golden emails, 34 of them
with at least one high-risk flag in the gold labels). Report directory:
`evaluation/reports/2026-11-20T09-42-55-267632+00-00-baseline/` (git-ignored; local only).
Line numbers refer to commit `cbf7a52f`.

"Not routed to human review" means: the gold labels contain a high-risk flag, but the
email was not sent to review. It is counted two ways: by `final_action`
(≠ `ask_user_check`) and by the executor decision (anything other than the
`InboxSoul/NeedsReview` label).

---

## Action routing

### 1. The executor ignores `final_action`

**Description.** `GmailExecutor._decide` chooses labels from the predicted risk flags,
Agent 2's decision and the safety override only. It never reads `final_action` or
`recommended_action`. When the model misses every high-risk flag, an email whose
`final_action` is `ask_user_check` is still labeled by category and archived.

**Location.** `src/review/executor.py:70-80` (`_decide`).

**Observed impact.** By `final_action`, 11 of 34 high-risk emails were not routed to
review. By executor decision, 25 of 34 were not (24 archived under a category label, 1
quarantined). The 14 emails that the pipeline sent to `ask_user_check` but the executor
archived are: g-002, g-003, g-007, g-008, g-012, g-015, s-004, s-005, s-007, s-008,
s-013, s-017, s-020, s-022.

**Planned fix.** Make `_decide` respect `final_action`: `ask_user_check` → keep in inbox
with `InboxSoul/NeedsReview`. Quarantine only when Agent 2 decided `move_to_junk`.

### 2. Two independent spam judgments (Agent 2 and the executor)

**Description.** `CATEGORY_LABELS` maps `Category.SPAM` to `InboxSoul/Quarantine`. On the
pass path, `_decide` labels and archives by category. So an email that Agent 2 passed,
but that the model labeled `category: spam`, is quarantined without Agent 2's
"`definite_spam` and confidence ≥ 0.90" rule.

**Location.** `src/review/executor.py:43` (mapping) and `src/review/executor.py:80`
(pass branch).

**Observed impact.** 0 emails in the baseline. The only two emails predicted as
`category: spam` (g-005, g-011) were also `definite_spam` at confidence 0.99, so Agent 2
had already decided `move_to_junk`. The path was confirmed by reading the code and by an
offline check with a fake LLM; it was not exercised in the baseline.

**Planned fix.** Leave junk decisions to Agent 2 alone. On the pass path, a `spam`
category should not map to the quarantine label (for example, map it to review instead).

### 3. `_resolve_action` lets the model bypass Agent 2's junk rule

**Description.** When Agent 2 returns `pass` and Agent 3 returns `no_reply`,
`Orchestrator._resolve_action` keeps the model's `recommended_action`. If the model
recommends `move_to_junk`, `final_action` becomes `move_to_junk` even though Agent 2 did
not decide junk. The safety override only prevents this when a high-risk flag was
predicted.

**Location.** `src/pipeline/orchestrator.py:97-120` (`_resolve_action`, the
`ReplyPolicy.NO_REPLY` fall-through at the end).

**Observed impact.** 10 emails got `final_action = move_to_junk` while Agent 2 did not
decide junk. All 10 were predicted `not_spam` at confidence 0.95.
- 4 have high-risk gold flags: s-001, s-009, s-011, s-026.
- 6 do not: g-009, g-013, g-016, s-024, s-028, s-030.

For 5 of the 6 (all except s-024), the gold `final_action` is `no_action`. Because of
issue 1, the executor did not quarantine any of these 10; it archived them under their
category label.

**Planned fix.** On the Agent 2 = `pass` path, never let `final_action` become
`move_to_junk`. Map the model's `move_to_junk` to `no_action` or `ask_user_check`, so that
only Agent 2 can produce a junk decision.

---

## Safety and robustness

### 4. Safety rules read only the LLM's `risk_flags`, never the raw email

**Description.** `safety.scan` takes only the `TriageOutput`. Like Agent 2, it decides
based solely on the risk flags the model predicted. No check runs on the raw subject or
body, so a flag the model misses cannot be caught later in the pipeline.

**Location.** `src/safety/scanner.py:19-37` (`scan(triage)`); Agent 2 in
`src/agents/spam_decision.py`.

**Observed impact.** High-risk flag recall was 19% (12/62 gold flag instances), and
macro recall over the 6 high-risk flags was 16.1%. Every high-risk email that was not
routed to review (11 by `final_action`, 25 by executor) had no predicted high-risk flag.

**Planned fix.** Add an independent, deterministic risk signal computed from the raw
email text (for example, keyword or pattern rules for the 6 high-risk categories) and
combine it with the model's flags. This touches `src/safety/`, which needs a written
plan first. It also has to fit CLAUDE.md's rule that Agents 2 and 3 do not re-read the
raw email.

### 5. No fail-closed handling when Pydantic validation fails

**Description.** `OllamaClient.chat_json` calls `model_validate_json` on the model reply.
A reply that fails validation raises an exception, and no caller turns it into a safe
outcome:
- In the poller, the exception leaves `run_once`. The failing email gets no label and
  stays in the inbox untriaged, and the remaining emails in that batch are not processed
  in that cycle.
- In the API, the exception becomes an HTTP 500.

**Location.** `src/llm/ollama_client.py:60-79` (`chat_json`); `src/review/poller.py:25-27`
(no per-email error handling); `src/ui/api.py:116-118`.

**Observed impact.** 0 parse failures in the baseline. With `format=schema`, Ollama
constrains the output to the schema. The risk remains if the format constraint is
removed, the model is changed, or the reply is truncated.

**Planned fix.** Catch triage failures per email and fail closed. Label the email
`InboxSoul/NeedsReview`, keep it in the inbox, log the error, and continue with the rest
of the batch.

### 6. Subject decoding bug: raw UTF-8 headers become U+FFFD

**Description.** `parse_raw_message` parses with the default `compat32` policy. Subject
headers that contain raw (unencoded) 8-bit UTF-8 come back as `Header` objects with an
unknown-8bit charset, and `_decoded_header` turns each non-ASCII byte into U+FFFD
(for example, "Performance review ��� please confirm date"). RFC 2047 encoded-words
(`=?utf-8?b?...?=`) decode correctly; this was checked with a synthetic header. The
affected headers are not RFC 2047-encoded.

**Location.** `src/ingestion/raw_parser.py:33-43` (`email.message_from_bytes` without a
policy) and `src/ingestion/raw_parser.py:57-60` (`_decoded_header`).

**Observed impact.** 8 legacy golden emails reach the model with a garbled subject:
g-002, g-005, g-006, g-008, g-010, g-011, g-012, g-016. Five of them have high-risk gold
flags (g-002, g-006, g-008, g-010, g-012), and none of those five received a high-risk
flag in the baseline. Whether the garbled subject causes the misses has not been
measured. Only the `.eml` path (`parse_raw_message`) was checked; `GmailSource` has not
been checked.

**Planned fix.** Parse with `email.policy.default` (or decode raw header bytes as UTF-8),
add a unit test with a raw UTF-8 subject, and re-run the evaluation as a separate change.

### 7. RAG uses ChromaDB's default L2 distance and no nomic-embed-text task prefixes

**Description.** `ChromaStore` creates collections without `hnsw:space`, so ChromaDB uses
its default L2 distance. `OllamaEmbedder` sends text to `nomic-embed-text` without the
task prefixes the model expects (`search_document:` for stored documents,
`search_query:` for queries). Documents and queries are embedded the same way.

**Location.** `src/rag/chroma_store.py:41-44` (collection creation) and
`src/rag/chroma_store.py:24-26` (embedding call). Used by the triage few-shot retriever
(`src/agents/triage_fewshot.py:132`) and by the style and Hermes stores
(`src/ui/api.py:49-50`).

**Observed impact.** Not measured. Retrieval quality has not been evaluated on its own.
If the embeddings are unit-normalized, L2 ranking equals cosine ranking; this has not
been verified. No code reads the distance values, so the metric affects ranking only.

**Planned fix.** Add the task prefixes, using separate query and document paths. Set
`hnsw:space` to `cosine`. Existing collections keep their original metric, so they must
be rebuilt. Measure few-shot retrieval before and after the change.
