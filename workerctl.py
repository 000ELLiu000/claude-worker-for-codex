#!/usr/bin/env python3
"""Durable workers backed by the native `claude` CLI; stdlib only, macOS/Linux.

Each worker has one Claude Code session and at most one in-flight job. CLI and
MCP share the same state. Jobs run detached and keep complete logs on disk.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CLAUDE_WORKER_STATE", str(ROOT / ".worker-state"))).resolve()
CLAUDE = os.environ.get("CLAUDE_WORKER_BIN", "claude")
# Workers run unattended: prompts that would need an interactive answer are
# denied ("none"), and the mode decides what is auto-allowed. Default matches
# the original autonomous controller; set acceptEdits for a stricter mode.
PERMISSION_MODE = os.environ.get("CLAUDE_WORKER_PERMISSION_MODE", "bypassPermissions")
POLICY = ROOT / "worker-policy.json"
EXPERIENCE = ROOT / "experience.jsonl"
TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted"}


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(path.read_text())


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def guidance():
    policy = read(POLICY) if POLICY.exists() else {"revision": "legacy", "worker_instructions": []}
    latest = {}
    if EXPERIENCE.exists():
        with EXPERIENCE.open() as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            for line in handle:
                if line.strip():
                    item = json.loads(line)
                    latest[item["key"]] = item
    verified = [item for item in latest.values() if item.get("scope") == "worker" and item.get("status") == "verified"]
    return {"policy": policy, "worker_experience": verified}


def goal_action(prompt):
    value = prompt.strip()
    if value == "/goal":
        return "status"
    if not value.startswith("/goal "):
        return None
    args = value[6:].strip()
    return "clear" if args in {"clear", "stop", "off", "reset", "none", "cancel"} else "set"


def task_input(prompt, snapshot):
    rules = snapshot["policy"].get("worker_instructions", [])
    rules = [*rules, *(entry["rule"] for entry in snapshot["worker_experience"])]
    context = "本次执行约定（本次任务中的明确要求优先）：\n" + "\n".join("- " + rule for rule in rules)
    action = goal_action(prompt)
    if action in {"status", "clear"}:
        return prompt.strip()
    if action == "set":
        # Slash commands must be first; /goal treats the rest as its condition.
        value = prompt.lstrip() + "\n\n" + context
        if len(value.removeprefix("/goal ")) > 4000:
            raise ValueError("/goal condition plus guidance exceeds 4000 characters; put task details in a file and reference its path")
        return value
    return context + "\n\n本次任务：\n" + prompt


def task_runtime(prompt, snapshot):
    action = goal_action(prompt)
    goal = action is not None
    # /goal is a native session Stop hook: goal tasks must keep hooks enabled,
    # ordinary jobs disable hooks so user/project hooks cannot interfere with
    # an unattended run. Both isolate settings-defined hooks; the session-level
    # goal hook stays available.
    settings = {"disableAllHooks": not goal}
    extra = ["--setting-sources", ""]
    return {"goal": goal, "goal_action": action, "settings": settings, "extra_args": extra}


def learn(key, observation, rule, evidence=None, scope="controller", status="candidate", source="actual_task"):
    if not key.strip() or not observation.strip() or not rule.strip():
        raise ValueError("key, observation and rule are required")
    if status == "verified" and not evidence:
        raise ValueError("verified experience requires evidence")
    record = {"key": key, "recorded_at": now(), "source": source, "scope": scope,
              "status": status, "observation": observation, "rule": rule, "evidence": evidence or []}
    with EXPERIENCE.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
    return record


def finish_hook(name, item):
    """Runs inside the worker lock on every terminal transition, without an LLM."""
    path = job_dir(name, item["job_id"]) / "review.json"
    if not path.exists():
        write(path, {"worker": name, "job_id": item["job_id"], "status": "pending",
                     "created_at": now(), "job_state": item["state"], "model": item["model"],
                     "subtype": item.get("subtype"), "result_preview": item.get("result_preview"),
                     "log_dir": item["log_dir"], "notes": None, "lesson_key": None})
    item["review_path"] = str(path)
    item["review_required"] = read(path)["status"] == "pending"


def review(name, job=None, outcome="none", notes=None, key=None, observation=None, rule=None,
           scope="controller", evidence=None):
    if outcome not in {"none", "candidate", "verified"} or scope not in {"controller", "worker"}:
        raise ValueError("invalid review outcome or scope")
    if not isinstance(notes, str) or not notes.strip():
        raise ValueError("review requires notes about verification or why no new lesson is needed")
    if outcome != "none" and not all(isinstance(v, str) and v.strip() for v in [key, observation, rule]):
        raise ValueError("a lesson requires key, observation and rule")
    with lock(name):
        manifest = read(worker_dir(name) / "worker.json")
        job = job or manifest.get("active_job") or manifest.get("last_job")
        if not job:
            raise ValueError("worker has no job to review")
        directory = job_dir(name, job)
        item = read(directory / "job.json")
        if item["state"] not in TERMINAL:
            raise ValueError("job is still running")
        finish_hook(name, item)
        path = directory / "review.json"
        record = read(path)
        if record["status"] == "reviewed":
            return record
        if outcome != "none":
            evidence = evidence or [item.get("result_path") or str(directory / "job.json")]
            learn(key, observation, rule, evidence, scope, outcome, "actual_task")
        record.update(status="reviewed", reviewed_at=now(), outcome=outcome, notes=notes,
                      lesson_key=key if outcome != "none" else None)
        write(path, record)
        item["review_required"] = False
        write(directory / "job.json", item)
    return record


def reviews(name=None):
    paths = worker_dir(name).glob("jobs/*/review.json") if name else STATE.glob("*/jobs/*/review.json")
    pending = []
    for path in sorted(paths):
        item = read(path)
        if item["status"] == "pending":
            pending.append({**{key: item[key] for key in ["worker", "job_id", "job_state", "model", "created_at"]},
                            "review_path": str(path)})
    return pending


def worker_dir(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("worker name must use 1-64 ASCII letters, digits, _ or -")
    return STATE / name


def job_dir(name, job):
    uuid.UUID(job)
    return worker_dir(name) / "jobs" / job


@contextmanager
def lock(name):
    directory = worker_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        # A zombie has no executable work left, even before its parent reaps it.
        ps = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        return bool(ps.stdout.strip()) and not ps.stdout.strip().startswith("Z")
    except ProcessLookupError:
        return False


def current_job(name, job=None):
    manifest = read(worker_dir(name) / "worker.json")
    job = job or manifest.get("active_job") or manifest.get("last_job")
    if not job:
        return {"worker": name, "state": "idle", "session_id": manifest["session_id"]}
    directory = job_dir(name, job)
    item = read(directory / "job.json")
    if item["state"] not in TERMINAL and item.get("runner_pid") and not alive(item["runner_pid"]):
        with lock(name):
            item = read(directory / "job.json")
            if item["state"] not in TERMINAL and not alive(item.get("runner_pid")):
                child = item.get("process_pid")
                if child and alive(child):
                    args = subprocess.run(["/bin/ps", "-o", "args=", "-p", str(child)], capture_output=True, text=True).stdout
                    try:
                        owned_group = (CLAUDE in args or os.path.basename(CLAUDE) in args) and manifest["session_id"] in args and os.getpgid(child) == child
                    except ProcessLookupError:
                        owned_group = False
                    if owned_group:
                        kill_group(child)
                        deadline = time.monotonic() + 3
                        while alive(child) and time.monotonic() < deadline:
                            time.sleep(0.1)
                        if alive(child):
                            kill_group(child, signal.SIGKILL)
                log = directory / "events.jsonl"
                if not manifest["session_started"] and log.exists():
                    for line in log.read_text().splitlines():
                        try:
                            if json.loads(line).get("type") in {"assistant", "user", "result"}:
                                manifest["session_started"] = True
                                write(worker_dir(name) / "worker.json", manifest)
                                break
                        except ValueError:
                            pass
                item.update(state="interrupted", finished_at=now(), error="runner exited without recording a final state; inspect files before retry")
                finish_hook(name, item)
                write(directory / "job.json", item)
    return item


def create(name, cwd, model=None, tools="Read,Edit,Write,Bash,Glob,Grep"):
    model = model or guidance()["policy"].get("models", {}).get("default_worker", "sonnet")
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("cwd must be an existing directory")
    with lock(name) as directory:
        if (directory / "worker.json").exists():
            raise ValueError("worker already exists")
        item = {"worker": name, "cwd": str(path), "model": model, "tools": tools,
                "session_id": str(uuid.uuid4()), "session_started": False,
                "created_at": now(), "active_job": None, "last_job": None}
        write(directory / "worker.json", item)
    return item


def submit(name, prompt, model=None, timeout=600, max_turns=24):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is empty")
    if not 1 <= timeout <= 86400 or not 1 <= max_turns <= 10000:
        raise ValueError("timeout or max_turns outside supported range")
    # Refresh abandoned-run state before acquiring the submission lock.
    current_job(name)
    with lock(name) as directory:
        manifest = read(directory / "worker.json")
        if manifest.get("active_job"):
            active = read(job_dir(name, manifest["active_job"]) / "job.json")
            if active["state"] not in TERMINAL:
                raise ValueError("worker busy: " + active["job_id"])
            if active.get("review_required"):
                raise ValueError("previous job requires worker_review before the next task: " + active["job_id"])
        job = str(uuid.uuid4())
        target = job_dir(name, job)
        target.mkdir(parents=True)
        target.joinpath("task.txt").write_text(prompt)
        snapshot = guidance()
        write(target / "guidance.json", snapshot)
        effective_input = task_input(prompt, snapshot)
        target.joinpath("input.txt").write_text(effective_input)
        item = {"worker": name, "job_id": job, "session_id": manifest["session_id"],
                "state": "queued", "created_at": now(), "model": model or manifest["model"],
                "timeout_s": timeout, "max_turns": max_turns,
                "task_path": str(target / "task.txt"), "log_dir": str(target),
                "input_path": str(target / "input.txt"), "guidance_path": str(target / "guidance.json"),
                "guidance_revision": snapshot["policy"].get("revision")}
        write(target / "job.json", item)
        manifest.update(active_job=job, last_job=job)
        write(directory / "worker.json", manifest)
        with (target / "runner.log").open("ab") as out:
            runner = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_run", name, job],
                                      stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                                      env={**os.environ, "CLAUDE_WORKER_STATE": str(STATE), "CLAUDE_WORKER_BIN": CLAUDE})
        item["runner_pid"] = runner.pid
        write(target / "job.json", item)
    return item


def kill_group(pid, sig=signal.SIGTERM):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def run_job(name, job):
    directory = job_dir(name, job)
    proc = None
    final = None
    failed = None
    output_seen = False
    try:
        with lock(name) as worker:
            manifest = read(worker / "worker.json")
            item = read(directory / "job.json")
            if (directory / "cancel").exists():
                item.update(state="cancelled", finished_at=now())
                finish_hook(name, item)
                write(directory / "job.json", item)
                return
            item.update(state="running", runner_pid=os.getpid(), started_at=now())
            write(directory / "job.json", item)
        cmd = [CLAUDE, "-p", "--permission-prompts", "none", "--permission-mode", PERMISSION_MODE,
               "--model", item["model"], "--output-format", "stream-json", "--verbose",
               "--max-turns", str(item["max_turns"]),
               "--tools", *(name.strip() for name in manifest["tools"].split(",") if name.strip()),
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--append-system-prompt", "Complete the assigned task in this workspace. Use subagents only when this task explicitly permits them; follow its agent count, model, depth and budget limits, supervise their results, and wait for them before reporting completion. Otherwise do not create subagents. Keep commands in the foreground unless the task explicitly requires a persistent service; report its PID when it does. Return concrete changes, output paths, and verification results. If required information is missing, explain it in your final response."]
        prompt = Path(item["task_path"]).read_text()
        snapshot = read(Path(item["guidance_path"])) if item.get("guidance_path") else guidance()
        runtime = task_runtime(prompt, snapshot)
        input_path = Path(item.get("input_path", str(directory / "input.txt")))
        # Also normalize jobs submitted by an already-running older MCP server.
        input_path.write_text(task_input(prompt, snapshot))
        item.update(input_path=str(input_path), goal_requested=runtime["goal"], goal_action=runtime["goal_action"])
        cmd.extend(["--settings", json.dumps(runtime["settings"]), *runtime["extra_args"]])
        cmd.extend(["--resume" if manifest["session_started"] else "--session-id", manifest["session_id"]])
        with Path(item.get("input_path", str(directory / "task.txt"))).open("rb") as task, (directory / "stderr.log").open("wb") as err, (directory / "events.jsonl").open("wb") as log:
            proc = subprocess.Popen(cmd, cwd=manifest["cwd"], stdin=task, stdout=subprocess.PIPE,
                                    stderr=err, start_new_session=True, env={**os.environ})
            with lock(name):
                item["process_pid"] = proc.pid
                write(directory / "job.json", item)
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            buffer = b""
            started = time.monotonic()
            while True:
                if (directory / "cancel").exists():
                    failed = "cancelled"
                    break
                if time.monotonic() - started >= item["timeout_s"]:
                    failed = "timed_out"
                    break
                for key, _ in selector.select(0.25):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    log.write(chunk); log.flush(); buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get("type") in {"assistant", "user"}:
                            output_seen = True
                        if event.get("type") == "result":
                            final = event
                if proc.poll() is not None and not selector.get_map():
                    break
            selector.close()
            if failed:
                kill_group(proc.pid)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    kill_group(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
            else:
                proc.wait(timeout=5)
        if final:
            write(directory / "result.json", final)
            directory.joinpath("result.txt").write_text(final.get("result") or "\n".join(final.get("errors") or []))
        success = bool(final and final.get("subtype") == "success" and not final.get("is_error") and proc.returncode == 0)
        item.update(state=failed or ("completed" if success else "failed"), finished_at=now(),
                    exit_code=proc.returncode, subtype=final.get("subtype") if final else None,
                    is_error=final.get("is_error") if final else None,
                    result_path=str(directory / "result.json") if final else None,
                    result_preview=(final.get("result") or "")[:2000] if final else None)
        if not final and not failed:
            item["error"] = "process ended without a result event; inspect stderr.log"
        with lock(name) as worker:
            manifest = read(worker / "worker.json")
            manifest["session_started"] = manifest["session_started"] or bool(final) or output_seen
            write(worker / "worker.json", manifest)
            finish_hook(name, item)
            write(directory / "job.json", item)
    except BaseException as exc:
        if proc and proc.poll() is None:
            kill_group(proc.pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_group(proc.pid, signal.SIGKILL)
        with lock(name):
            item = read(directory / "job.json")
            item.update(state="failed", finished_at=now(), error=repr(exc))
            finish_hook(name, item)
            write(directory / "job.json", item)


def cancel(name, job=None):
    with lock(name):
        manifest = read(worker_dir(name) / "worker.json")
        job = job or manifest.get("active_job")
        if not job:
            raise ValueError("no active job")
        directory = job_dir(name, job)
        item = read(directory / "job.json")
        if item["state"] not in TERMINAL:
            directory.joinpath("cancel").touch()
    return {"worker": name, "job_id": job, "cancel_requested": item["state"] not in TERMINAL, "state": item["state"]}


def result(name, job=None):
    item = current_job(name, job)
    path = item.get("result_path")
    review_path = item.get("review_path")
    return {"job": item, "result": read(Path(path)) if path and Path(path).exists() else None,
            "review": read(Path(review_path)) if review_path and Path(review_path).exists() else None}


def workers():
    return [read(p) for p in sorted(STATE.glob("*/worker.json"))]


def wait(name, job=None, seconds=55):
    deadline = time.monotonic() + min(max(seconds, 0), 55)
    while True:
        item = current_job(name, job)
        if item["state"] in TERMINAL or time.monotonic() >= deadline:
            return item
        time.sleep(0.5)


def mcp():
    specs = [
        ("worker_create", "Create one durable Claude Code worker in an existing workspace.", {"name": {"type": "string"}, "cwd": {"type": "string"}, "model": {"type": "string"}, "tools": {"type": "string"}}, ["name", "cwd"]),
        ("worker_submit", "Submit a task; returns a job ID immediately. One active task per worker. Explicitly define any permitted worker subagents and their budgets. Does not retry or queue busy workers. For tasks with clear acceptance criteria, start the prompt with /goal plus a short verifiable condition to use the CLI's native non-interactive goal loop — a completion check, not a substitute for your own acceptance review.", {"name": {"type": "string"}, "prompt": {"type": "string"}, "model": {"type": "string"}, "timeout": {"type": "integer"}, "max_turns": {"type": "integer"}}, ["name", "prompt"]),
        ("worker_status", "Read task status. Use job_id to select an earlier task.", {"name": {"type": "string"}, "job_id": {"type": "string"}}, ["name"]),
        ("worker_result", "Read task final JSON and log paths; does not independently verify the worker's claims.", {"name": {"type": "string"}, "job_id": {"type": "string"}}, ["name"]),
        ("worker_cancel", "Request cancellation of this worker's task; preserves session and logs.", {"name": {"type": "string"}, "job_id": {"type": "string"}}, ["name"]),
        ("worker_list", "List durable workers registered by this server.", {}, []),
        ("worker_reviews", "List pending post-task reviews created automatically by the completion hook.", {"name": {"type": "string"}}, []),
        ("worker_review", "Close a completed task's review after checking results. Record a candidate or verified lesson, or explain why there is no new lesson. Required before the next task on that worker.",
         {"name": {"type": "string"}, "job_id": {"type": "string"}, "outcome": {"type": "string", "enum": ["none", "candidate", "verified"]},
          "notes": {"type": "string"}, "key": {"type": "string"}, "observation": {"type": "string"}, "rule": {"type": "string"},
          "scope": {"type": "string", "enum": ["controller", "worker"]}, "evidence": {"type": "array", "items": {"type": "string"}}}, ["name", "notes"]),
    ]
    definitions = [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": req, "additionalProperties": False}} for n, d, p, req in specs]
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                value = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                         "serverInfo": {"name": "claude-worker-for-codex", "version": "1.0.0"},
                         "instructions": "Create or reuse a durable Claude Code worker, submit a task, then read status/result. Verify actual outputs instead of inferring success from process exit: match verification to the task — light source checks for exploration, careful sampling for agent-as-judge, actual visual/interaction checks for frontend work. Compose independent parallel tasks, serial dependencies or independent judge votes as needed; keep judges independent and preserve reasons/disagreement. Worker subagents require explicit task permission and count/model/depth/budget limits. The completion hook creates a pending review; call worker_review to record an evidenced lesson or explain why none is needed before submitting the next task on that worker. Explicit user model choices take precedence. Rules and verified experience are injected on every submit. A prompt starting with /goal uses the CLI's native non-interactive goal loop; it is a completion check, not a quality guarantee."}
            elif method == "ping":
                value = {}
            elif method == "tools/list":
                value = {"tools": definitions}
            elif method == "tools/call":
                params = request.get("params", {})
                name = params["name"]
                args = dict(params.get("arguments", {}))
                spec = next((spec for spec in specs if spec[0] == name), None)
                if not spec or set(args) - set(spec[2]) or set(spec[3]) - set(args):
                    raise ValueError("unknown tool or invalid arguments")
                if "job_id" in args:
                    args["job"] = args.pop("job_id")
                handlers = {"worker_create": create, "worker_submit": submit, "worker_status": current_job,
                            "worker_result": result, "worker_cancel": cancel, "worker_list": workers,
                            "worker_reviews": reviews, "worker_review": review}
                try:
                    answer = handlers[name](**args)
                    value = {"content": [{"type": "text", "text": json.dumps(answer, ensure_ascii=False)}]}
                except Exception as exc:
                    value = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": value}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                              "error": {"code": -32602, "message": str(exc)}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create"); p.add_argument("name"); p.add_argument("--cwd", required=True); p.add_argument("--model"); p.add_argument("--tools", default="Read,Edit,Write,Bash,Glob,Grep")
    p = sub.add_parser("submit"); p.add_argument("name"); p.add_argument("--task-file", required=True); p.add_argument("--model"); p.add_argument("--timeout", type=int, default=600); p.add_argument("--max-turns", type=int, default=24)
    for name in ["status", "result", "cancel", "wait"]:
        p = sub.add_parser(name); p.add_argument("name"); p.add_argument("--job-id", dest="job")
        if name == "wait": p.add_argument("--seconds", type=int, default=55)
    sub.add_parser("list"); sub.add_parser("mcp")
    p = sub.add_parser("reviews"); p.add_argument("--name")
    p = sub.add_parser("review"); p.add_argument("name"); p.add_argument("--job-id", dest="job"); p.add_argument("--outcome", choices=["none", "candidate", "verified"], default="none"); p.add_argument("--notes", required=True); p.add_argument("--key"); p.add_argument("--observation"); p.add_argument("--rule"); p.add_argument("--scope", choices=["controller", "worker"], default="controller"); p.add_argument("--evidence", action="append")
    p = sub.add_parser("learn"); p.add_argument("--key", required=True); p.add_argument("--observation", required=True); p.add_argument("--rule", required=True); p.add_argument("--evidence", action="append"); p.add_argument("--scope", choices=["controller", "worker"], default="controller"); p.add_argument("--status", choices=["candidate", "verified", "retired"], default="candidate"); p.add_argument("--source", choices=["actual_task", "self_test", "user"], default="actual_task")
    p = sub.add_parser("_run"); p.add_argument("name"); p.add_argument("job")
    args = vars(parser.parse_args()); command = args.pop("command")
    if command == "mcp": return mcp()
    if command == "_run": return run_job(**args)
    if command == "submit": args["prompt"] = Path(args.pop("task_file")).read_text()
    actions = {"create": create, "submit": submit, "status": current_job, "result": result,
               "cancel": cancel, "wait": wait, "list": workers, "learn": learn, "review": review, "reviews": reviews}
    try:
        print(json.dumps(actions[command](**args), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
