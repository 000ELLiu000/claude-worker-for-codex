# A complete brief for a single handoff

The main agent fills this out before the first submission. Provide established facts and existing checks so the worker can start without rebuilding the toolchain. Keep the brief focused on what the task needs.

- **Goal and audience:** What should the deliverable achieve, and what should its audience understand?
- **Deliverables:** Exact output paths, formats, and the permitted scope of changes.
- **Required facts:** Relevant facts and sources. Point to precise code locations or excerpts when needed; read supporting material on demand.
- **Acceptance criteria:** Content, behavior, visual requirements, and failure conditions. Include them all in the initial brief.
- **Check entry points:** Existing commands, environment details, and screenshot or log locations. The worker uses these checks rather than inventing another verification toolchain.
- **Budget:** A wall-clock limit and a generous turn limit, with room for self-checks, repairs, and a final report.
- **Verification level:** Exploration, agent-as-judge, frontend, or code. Specify the checks this task actually needs.
- **Workflow:** Single worker, independent parallel tasks, sequential dependencies, independent judges, or a mixture. Define inputs, outputs, and dependencies.
- **Subagents:** Either prohibit them for this task or explicitly define permitted roles, count, models, maximum depth, and shared budget.

Within one job, the worker should implement, run the required checks, inspect the actual outputs, fix issues together, and return a short handoff. Self-repair stays within the task budget. If the same obstacle repeats or further attempts offer little benefit, report the blocker, current artifacts, and evidence so the main agent can decide what to do next.

## Use goal for verifiable acceptance criteria

Native Claude Code supports non-interactive `/goal`. Start the `worker_submit` prompt with the actual command, for example:

```text
/goal Complete the deliverables described in task.md. The specified checks actually exit with code 0, all changes stay within the assigned scope, and the final response includes verification results. Finish within this job's budget.
```

The controller preserves the slash command at the start of the input. Ordinary prompts continue to work normally. A goal loop checks completion; issuing the command does not establish that the goal was met. The main agent still reviews the deliverables at the appropriate level. A successful process exit alone is not proof of acceptance.

The goal condition plus injected rules must fit within the native 4,000-character limit. Put detailed requirements in a file and reference its path.

## Handoffs between workflows

- **Independent research:** Split the topic into independent slices and search in parallel. Each worker returns sources and short conclusions; the main agent performs a lightweight check and synthesis.
- **Sequential tasks:** Worker A delivers its output. After checking that it is usable, pass the output file and necessary facts to worker B. Avoid making B repeat A's work.
- **Independent judges:** Give each judge the same rubric and request an independent decision with reasons. Aggregate votes and disagreements, then carefully spot-check the evidence. A majority vote does not establish correctness.
- **Worker-owned subagents:** When explicitly permitted, expose the required tools with `tools=default`, or an appropriate tool list for the installed CLI. The worker decomposes the task, collects results, and checks them before handing off. All subagents share the task's budget; the brief defines their count, roles, models, and depth.

For an ordinary HTML explainer, prefer self-contained HTML, CSS, and SVG, with a complete desktop layout and a readable vertical layout on narrow screens. Add a drawing toolchain only when the task requires it or the user requests it. Use existing project checks when available, then inspect screenshots to confirm the actual result.

## Final handoff and review

The worker returns only artifact paths, checks actually performed with evidence, and unresolved issues. The main agent performs an independent review proportional to the task. If revisions are needed, send all findings together while maintaining the acceptance standard. Close the job's review after inspecting the result.
