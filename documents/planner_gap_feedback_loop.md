# Phase 2 — Planner → Analyst requirement-gap feedback loop

**Goal:** when the Planner emits a genuine "this requirement is too thin to build" question,
route it back to the Analyst, which resolves it (refine/author/needs_input/dismiss/**wont_do**),
auto-applies by default, and the Planner re-plans ONLY the affected requirements — closing the
Analyst→Architect→Planner chain into a loop instead of dead-ending questions in `plan.json`.

Restaurant project id: `185d83e85fc84e15ab77796c40e22eb4`. Current committed plan `04731fc`:
59 feasible / 27 questions / 8 flagged / 74 coverage gaps. The 27 questions are genuine
product-content gaps (schema fields, business/authorization rules) — 0 tooling. Those 27 are the
inputs this loop consumes.

## User decisions (authoritative)
1. **A dedicated impasse-resolver PROMPT/agent** decides the disposition. Dispositions:
   - `refine` — existing requirement incomplete → rewrite it to include the missing detail (from problem statement/docs).
   - `author` — a new derived requirement is needed → draft one.
   - `needs_input` — genuinely needs a human VALUE → one consolidated clarification question.
   - `dismiss` — out of scope for this system.
   - `wont_do` (**NEW**) — the capability simply is NOT part of the product because it was never requested (e.g. no backup requirement → no backups, by design). Record the decision, DON'T build, DON'T block, DON'T ask. This is the key lever that stops false questions from stalling the pipeline.
2. **Trigger = Automatic by default**, per-project setting (Auto/Manual). Configured in a new **Settings panel** opened from the user-photo popup menu.
3. **Apply = Auto by default**, per-project setting (Auto/Manual) — same Settings panel.
4. **Re-plan only the AFFECTED requirements** (not a full re-run).
5. **Full progress visibility in the Overview page**: where we are + # requirements processed/total.

## Existing machinery to REUSE (do not reinvent)
- Analyst `gaps:assess` (gap_assessor.py) → author/needs_input/dismiss triage; `author:run` (authoring.py) drafts reqs (flagged `analyst_authored`, blocks release); `refine:run` (refine.py) improves an existing req (INCOSE loop, escalates `needs_human`). Endpoints in analyst api.py ~1182 refine:run, 1390 author:run, 1475 gaps:assess, 1576 gaps:apply.
- Planner already emits `questions:[{task_title, question, gap, traces_to:[req_id]}]` in plan.json.
- Planner can plan a SUBSET of reqs (plan_project takes a req list) → affected-only re-plan.
- Planner service (:7805) job pattern; FACTORY nav user menu in reqoach `js/nav.js`; per-project state.

## Naming
- Agent/prompt: `analyst_gap_resolver` (prompt file `analyst_agent/prompts/analyst_gap_resolver.txt`, registered in agent_server as TEXT).
- Analyst module: `analyst_agent/src/analyst_agent/gap_resolver.py`.
- Analyst endpoints: `POST /projects/{pid}/planner-gaps:resolve` (job), `GET /projects/{pid}/planner-gaps` (results), `GET/PUT /projects/{pid}/settings`.
- Settings stored in project meta: `settings.gap_loop = {trigger:"auto"|"manual", apply:"auto"|"manual"}` (default auto/auto).

## TASK BREAKDOWN (check off as done; update STATUS section)

### A. Impasse-resolver agent
- [ ] A1. Write `analyst_gap_resolver.txt` prompt: input = {req_id, requirement_text, problem_statement, gap, question}; output JSON = {disposition: refine|author|needs_input|dismiss|wont_do, rationale, refined_text?, authored_requirement?, question?}. Emphasise `wont_do` for absence-of-requirement.
- [ ] A2. Register it in agent_server (register script or a small POST; TEXT not path).

### B. Analyst resolve logic + endpoints
- [ ] B1. `gap_resolver.py`: `resolve_gap(pid, gap, client)` → call agent → return disposition record. `resolve_planner_gaps(pid, gaps, apply)` → iterate (progress), APPLY when apply=auto (refine→store rewritten req; author→add authored req; needs_input/dismiss/wont_do→record only). Return affected req_ids + per-gap records.
- [ ] B2. store.py: persist resolutions (`store/projects/<pid>/planner_gaps/*.json`) + settings get/set in project meta.
- [ ] B3. api.py: `POST /planner-gaps:resolve` (JobManager job, progress {done,total,stage}), `GET /planner-gaps`, `GET/PUT /settings`.

### C. Planner gap export + affected-only re-plan + orchestration
- [ ] C1. Planner: export genuine questions as gaps (enrich plan.json questions with requirement_text) — a helper + `GET /planner/.../gaps` OR read plan.json.
- [ ] C2. Planner service: after a planner:run, if trigger=auto → POST analyst planner-gaps:resolve → on done, re-plan ONLY affected req_ids → merge into plan → re-commit. Progress surfaced on the job.
- [ ] C3. Affected-only re-plan: plan_project on the affected req subset; merge with the unaffected tasks; re-dedup; commit.

### D. FACTORY UI (reqoach)
- [ ] D1. Settings panel: in `js/nav.js` user-photo popup add "Settings" → panel with per-project toggles (trigger Auto/Manual, apply Auto/Manual) → PUT /analyst/projects/{pid}/settings.
- [ ] D2. Overview: progress readout for the gap-loop (stage + N/total) — a lane or a status line, polled from the resolve job.
- [ ] D3. Planning tab: show per-question disposition outcomes; manual "Run refinement" button when trigger=manual.
- [ ] D4. reqoach edge/proxy + lifecycle passthrough for the new analyst endpoints (via existing /analyst/ route — likely no nginx change; verify).

### E. Verify end-to-end on Restaurant
- [ ] E1. Run the loop on the 27 questions; confirm dispositions sane (wont_do used for absence gaps), auto-apply updates reqs, affected-only re-plan drops question count, progress visible.

## EXECUTION ORDER
A1→A2 (prompt+register) → B1→B2→B3 (analyst core+endpoints) → C1→C3→C2 (planner export+re-plan+orchestration) → D1→D4 (UI) → E1 (verify).

## STATUS (update frequently — resume point after compaction)
- 2026-08-05: plan written.
- [x] A1 DONE: `analyst_agent/prompts/analyst_gap_resolver.txt` written (5 dispositions, wont_do biased).
- [x] A2 DONE + VERIFIED: registered in agent_server (POST /admin/api/agents, params temp0/max_tokens1024/enable_thinking false). Smoke test on a backups gap correctly returned `wont_do`.
- [x] B1 DONE: `gap_resolver.py` — resolve_gap() + resolve_planner_gaps(pid,gaps,apply,progress). refine → upsert_req_review final_text (reaches the package the Planner reads). author → recorded (heavier scorecard insert deferred). Uses compact_problem_statement + AgentServerClient.complete_json.
- [x] B2 DONE: store.py — save/get_planner_gap_resolution + get/set_settings (settings.gap_loop.trigger/apply in project meta, default auto/auto).
- [x] B3 DONE + VERIFIED: analyst api.py — GapJob (lightweight threaded, `_gap_jobs`), POST /projects/{pid}/planner-gaps:resolve (body {gaps,apply?}, apply defaults to settings), GET /gap-jobs/{id}, GET /projects/{pid}/planner-gaps, GET/PUT /projects/{pid}/settings. Analyst rebuilt+up. Verified: settings get/put (put owner-gated, works with admin header), 2-gap resolve job → both needs_input with sane rationales.
- **A + B COMPLETE.** Analyst side of the loop is live on :7803.
- NEXT = C1: Planner exports its genuine questions as gaps. In planner_agent, add a helper/endpoint that returns [{req_id, requirement_text?, gap, question}] from the latest plan.json questions (skip flagged; questions already have traces_to+gap). Then C3 (affected-only re-plan in pipeline) then C2 (planner service orchestration: after planner:run, if settings.trigger=auto → POST analyst planner-gaps:resolve, poll /gap-jobs, then re-plan affected req_ids, merge+commit; surface progress on the planner job). Analyst base url in planner container = http://localhost:7803 (ANALYST_URL). reqoach reads /repos/{pid}/plan.
- [x] C1 DONE: `planner_agent/src/planner/api.py` — `plan_to_gaps(plan)` returns [{req_id, gap, question}] from plan.questions (requirement_text filled by Analyst).
- [x] C2+C3 DONE + DEPLOYED (not yet exercised end-to-end — that's E): planner api.py refactored into `_plan_once(job,client,cache,phase)` / `_publish` / `_summ` / `_resolve_gaps`. `_run_planner` now: cold `_plan_once(phase="plan")` w/ per-project **Cache** (`data/cache/<pid>.jsonl`) → `_publish` → fetch `_analyst_settings(pid)` (GET, ungated) → if `gap_loop.trigger=="auto"` and questions and `job.actor`: `plan_to_gaps` → `_resolve_gaps(gaps, apply=(gap_loop.apply=="auto"))` (POST analyst planner-gaps:resolve with `X-Auth-Request-Email: <actor>`, polls /gap-jobs, mirrors progress) → if affected_req_ids: `_plan_once(phase="replan")` (refined reqs cache-MISS+recompute, rest HIT) → `_publish`. No actor → skip resolve (records `{skipped:"no authenticated actor"}`). `planner_run(pid, request: Request)` reads `x-auth-request-email` → `create_planner_run(pid, actor=email)` → Job.actor. Verified: package `text`=`final_text or req.text` ([package.py:47](../src/analyst_agent/package.py#L47)) so refine changes package text → cache invalidates that req only. Planner rebuilt+up on :7805, /health ok, all 4 helpers present in image. `result` carries `gap_resolution{total,counts,affected_req_ids,applied}` + `reqs_refined`; job.progress stages plan|resolve|replan|done with done/total.
- [x] D DONE + DEPLOYED (served-content verified): (D1) `js/nav.js` — user-photo menu gains "⚙ Project settings" (only when a project is open) → modal with two Auto/Manual segmented toggles (Resolve gaps automatically = trigger, Apply resolutions automatically = apply), loads GET /analyst/projects/{pid}/settings, PUTs `{gap_loop:{trigger,apply}}` on change (owner-gated, needAuth-handled), injects own CSS. (D2) `overview.html` Planner node maps stages plan|resolve|replan→friendly labels + shows N/total requirements (job.stage now set to "resolve" during resolution in planner api.py). (D3) `planning.html` — poll status shows friendly stage; new "Requirement gap resolutions" section reads GET /analyst/projects/{pid}/planner-gaps → count pills per disposition + per-record rows (disposition badge, req_id, question/rationale, refined/authored/needs_input text, applied flag). (D4) NO nginx change: `/analyst/` gates non-GET→@analyst_write forwards X-Auth-Request-Email (PUT settings owner-gated); `/planner/` POST→@planner_write forwards it → planner_run captures job.actor; GET settings+planner-gaps are public reads. All rebuilt: planner :7805 (health ok), reqoach/FACTORY :7802 (nav.js/planning.html/overview.html new code confirmed in served responses). node --check clean on all three.
- [x] E DONE + VERIFIED (mechanism), content inspected. Ran planner:run on Restaurant with `X-Auth-Request-Email: logus2k@gmail.com` (admin; project owner is None). Job 24a4f156, 385s, 616 LLM calls, committed sha 229bbea. Stages executed cleanly: plan(60, cold→cache populated) → resolve(31 gaps) → replan(affected-only, unaffected reqs logged "(cached)") → done. gap_resolution: applied=true, affected=[REQ-0008,0018,0062], counts **{needs_input:28, refine:3}**. Final plan 65F/28Q/9Fl/74CG (was 59/27/8/74).
  - **OUTCOME: loop works but did NOT cut the backlog on THIS dataset (27→28 Q).** Read all 31 rationales. Verdict: 0 wont_do is *correct here* — Restaurant's questions are genuine product-content gaps (entity schemas, OAuth flow, validation rules), NOT absence-of-requirement false stalls, so wont_do had nothing to catch. 3 refines sound (768px breakpoint; OAuth token-validation boundary; find-or-create identity) and correctly drove the affected-only re-plan.
  - **CALIBRATION SIGNALS (open decision, do NOT retune without user):** (1) resolver is *conservative* — some needs_input are refinable with engineering defaults; **REQ-0054 sent a TOOLING question ("PostgreSQL vs SQLite") to needs_input** (should be Planner-resolved, not asked). (2) many needs_input are *duplicates of one underlying gap* (~6× "Reservation entity fields", ~4× "TenantConfiguration fields") — one answer clears several; consolidation is the real lever to shrink the 28. Trade-off: more aggression fabricates product decisions. Flagged for the user to weigh.
- **PHASE 2 COMPLETE (A–E).** Loop is live end-to-end. Remaining is prompt calibration (user decision) + consolidating duplicate questions, not mechanism.

## PHASE 3 — human resolution surface for the two dead-end buckets (IN PROGRESS)
**Why:** Phase 2 auto-handles machine-decidable dispositions (refine/wont_do/dismiss/author) but leaves TWO buckets as read-only dead-ends with no action path: (a) `needs_input` questions (28 on Restaurant), (b) `flagged` "did not converge within refine_budget" tasks (8–9). User: "no way/UI/UX to resolve those."
**KEY FINDING (evidence, not theory):** the traced reqs behind the needs_input are ALL already good (REQ-0025=5.0, 0011=4.67, 0045=4.67, 0055=4.56, 0012=4.89…). The questions are NOT defects in them — they ask for **requirements that were never written** (Reservation schema, Menu schema, contact-form behavior), blamed on whatever req the planner was decomposing. ⇒ these are mostly **missing requirements (author)**, overlapping the 74 coverage gaps, not thin requirements (refine).
**USER DECISIONS (authoritative):** (1) answering runs the human's answer **through the refiner**, shows the **INCOSE score**, and the **user picks/edits the final text** (human-in-the-loop, NOT auto-apply). (2) Support **BOTH create-new AND refine-existing, possibly simultaneously** for one question. (3) **Bundle** the needs_input-answer flow WITH flagged→resolver routing, **clearly labeled** so each bucket is visually distinct.
**REUSE (verified):** `assess.assess_requirement(text, review=True)`→{overall, review.rewrites}; `refine._refine_one(seed, None, threshold, client)`→{final_text, final_score, history} (non-committing); authoring `author_for_gap`/`_scorecard_record`/`_next_gap_seq`/`_persist`/`is_duplicate` for inserting an `origin:"analyst_authored"` req; `store.upsert_req_review(final_text, status)`; single-req score via `rescore.rescore_requirement`. Threshold = review.threshold.value (default 4.3).
**DESIGN / TASKS:**
- P3-1 Analyst `answer.py`: `preview(pid, run, req_id, question, answer, client)` → returns BOTH candidates non-committing: `refine`={candidate_text (refiner over traced_text+answer), score_before, score_after}, `author`={candidate_text (refiner over answer-as-standalone-req), score}. 
- P3-2 Analyst endpoints (owner-gated, mirror rescore_one shape): `POST /projects/{pid}/reviews/{run}/requirements/{req_id}/answer:preview` {question,answer} → both candidates+scores; `POST …/answer:apply` {refine_text?, author_texts?[]} → if refine_text: upsert_req_review(final_text, status="gap_answered")→affected+=req_id; for each author_text: insert analyst_authored req (reuse authoring persist), score it → affected+=new_id; return {affected_req_ids}. Update the planner-gaps record status→"answered".
- P3-3 Planner flagged→resolver + FIX diagnostic loss (3 drop points): `checkpoint._res_to_dict/_res_from_dict` must keep flagged `feasibility`(verdict/reasoning/missing)+`gap`; `pipeline.assemble_plan` flagged must emit reasoning+missing; `plan_to_gaps` also iterate `plan["flagged"]` → gaps {req_id, gap: missing/reason, question: synth}. Dedup flagged vs same-req questions.
- P3-4 FACTORY Planning tab: replace read-only gap section with interactive **Resolution** surface, 3 labeled groups: **Auto-resolved** (refine/wont_do/dismiss/author, read-only), **Needs your answer** (needs_input+flagged), **What changed** (applied). Per needs-answer item: Answer box → preview → shows refine candidate (before→after score) AND author candidate (score) → user checks refine and/or author, edits text → Apply → then Re-plan (affected-only). Group by req_id to collapse dupes (cross-req semantic merge = LATER via reranker, NOT regex).
- P3-5 Re-run Restaurant: verify flagged routed (count drops), answer→author/refine→affected-only re-plan works E2E, UI groups clearly.
**EXEC ORDER:** P3-1→P3-2 (backend answer, verify curl) → P3-3 (flagged routing, verify plan_to_gaps) → P3-4 (UI) → P3-5 (verify).
**STATUS:**
- [x] P3-1+P3-2 DONE+VERIFIED: `answer.py` (preview/apply) + endpoints `POST /reviews/{run}/requirements/{req_id}/answer:preview|:apply`. Analyst rebuilt. Curl on REQ-0025 ("prevent anonymous reservations", 5.0): preview returned refine 5.0→**4.56** (folding schema in HURTS — non-singular, as predicted) + author **4.89** (but the refiner compressed the field list to one atom — hence human-edits-final-text matters). apply(author_texts=[edited full-field text]) → inserted **GAP-0001** scored 4.67, affected_req_ids=[GAP-0001]. (GAP-0001 left in the set — legit reservation-schema req; user may delete.)
- [x] P3-3 DONE+VERIFIED: checkpoint `_res_to_dict/_from_dict` keep flagged feasibility+gap; cache `_CACHE_SCHEMA` v1→**v2** (invalidates old cache so flagged feasibility gets captured); `assemble_plan` flagged emits detail+missing; `plan_to_gaps` also routes flagged (tagged origin). Planner rebuilt; in-container `plan_to_gaps(committed plan)` → **37 gaps = 28 question + 9 flagged**.
- [x] P3-4 DONE+VERIFIED (headless-rendered + viewed): `planning.html` Resolution surface — 3 labeled groups (Applied this session / **Needs your answer** / **Auto-resolved**). Needs group = interactive, GROUPED BY req_id (REQ-0010's 2 Qs → one card), each w/ answer textarea + "Preview resolution" → answer:preview → shows Refine candidate (before→after score, editable) + Author candidate (score, editable, "+ add another") w/ checkboxes → "Apply selected" → answer:apply → moves to "Applied this session" + "Re-plan now". reqoach rebuilt; google-chrome-stable render confirms grouping + controls display.
- [x] P3-5 DONE+VERIFIED (data + headless render), job 2bddd8e8 sha 80fd264, 6min, 667 calls. plan(61)→resolve(28)→replan→done. **flagged 9→2, questions 27→21**, feasible 59. gap_resolution total=28 {needs_input:22, refine:6}, 6 affected→re-plan. **Flagged tasks routed through the resolver**: 5 flagged-origin gaps → 3 refine (auto-applied) + 2 needs_input. Render confirms: "Needs your answer" shows the 2 residual flagged (REQ-0015, REQ-0049) with a red **"DIDN'T CONVERGE"** tag AND a REAL diagnostic ("Specification of the database connection mechanism…", "A concrete definition or schema for the User model fields…") — NOT the opaque "refine_budget (3)" (proves the checkpoint/cache-v2/assemble diagnostic-preservation fix). Grouped-by-req cards + answer/Preview controls all present; Auto-resolved group shows the 6 refines.
- **PHASE 3 COMPLETE (P3-1..P3-5).** Both dead-end buckets (needs_input + flagged) now have a human resolution surface: answer → refiner → scored refine+author candidates → user edits/picks → apply (refine and/or author-new, both allowed) → affected-only re-plan. Flagged no longer dead-ends. NOTE: cross-req duplicate questions still not merged (grouped per-req only) — future work via reranker, NOT regex.
  1. Enable the Planner **Cache** (`from .cache import Cache`, `Cache(DATA_DIR/cache/<pid>.jsonl)`) on the plan_project call. This gives **affected-only re-plan for free**: after refine changes a requirement's text, that req cache-MISSES (recomputes); unaffected reqs HIT (reuse). No manual merge.
  2. Refactor into helpers: `_plan_once(job, cache, phase)` (fetch pkg → plan_project(cache) → assemble → return plan), `_publish(job, plan)` (write data/plans + publish_plan_to_repo), `_summ(plan)` (result dict).
  3. Flow: `plan=_plan_once(cache)` → `_publish` → fetch settings `GET {ANALYST_URL}/projects/{pid}/settings`. If `trigger=="auto"` and plan.questions: `gaps=plan_to_gaps(plan)` → `_resolve_gaps(job,gaps,apply=settings.apply=="auto")` → if affected_req_ids: `plan=_plan_once(cache, phase="replan")` (only refined reqs recompute) → `_publish`. Update job.result with gap_resolution counts + reqs_refined.
  4. `_resolve_gaps(job,gaps,apply)`: POST `{ANALYST_URL}/projects/{pid}/planner-gaps:resolve` body {gaps,apply}, poll `{ANALYST_URL}/gap-jobs/{id}` until done, mirror its progress into job.progress. **AUTH:** the Analyst resolve endpoint is owner-gated (authz middleware). Propagate identity: `planner_run(pid, request: Request)` reads `x-auth-request-email` → `create_planner_run(pid, actor=email)` → Job.actor → `_resolve_gaps` sends header `X-Auth-Request-Email: <actor>`. If no actor (anon), skip the resolve (can't apply). (The edge `@planner_write` already forwards the email on the gated POST.)
- THEN D (FACTORY UI: Settings panel in nav.js user popup; Overview progress from the planner job's resolve/replan stages; Planning tab outcomes from GET /analyst/projects/{pid}/planner-gaps) + E (verify on the 27: expect wont_do/refine to cut the 27 substantially, remaining are true needs_input).
