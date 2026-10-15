"""Check slash-command framing and runtime isolation without an LLM.

Fully synthetic: temporary directories only, no claude invocation, no network.
Run with:  python3 scripts/verify_task_modes.py
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

def manifest_session_id(module, name):
    return module.read(module.worker_dir(name) / "worker.json")["session_id"]

def read_json(*parts):
    return json.loads(Path(*parts).read_text())

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workerctl", root / "workerctl.py")
workerctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workerctl)
snapshot = {"policy": {"worker_instructions": ["Return evidence."]}, "worker_experience": []}
checks = []

def check(name, condition):
    checks.append({"check": name, "passed": bool(condition)})
    assert condition, name

normal = workerctl.task_input("Do the task.", snapshot)
check("ordinary tasks retain the guidance prefix", normal.startswith("Execution rules for this task") and normal.endswith("Do the task."))
goal = workerctl.task_input("  /goal proof.txt passes its check", snapshot)
check("goal command stays first when enabled framing is computed", goal.startswith("/goal proof.txt"))
check("goal retains current task guidance", "Return evidence." in goal)
check("goal status remains an actual command", workerctl.task_input("/goal", snapshot) == "/goal")
check("goal status accepts a trailing newline", workerctl.task_input("/goal\n", snapshot) == "/goal")
check("goal clear is not turned into a new condition", workerctl.task_input("/goal clear", snapshot) == "/goal clear")
try:
    workerctl.task_input("/goal " + "x" * 4000, snapshot)
except ValueError:
    too_long = True
else:
    too_long = False
check("oversized goal conditions fail explicitly", too_long)

runtime = workerctl.task_runtime(normal, snapshot)
check("ordinary runtime disables hooks", runtime["settings"]["disableAllHooks"] is True)
check("ordinary runtime isolates setting sources", runtime["extra_args"] == ["--setting-sources", ""])
runtime = workerctl.task_runtime("/goal finish the task", snapshot)
check("goal prompts activate goal mode without any opt-in switch", runtime["goal"] is True)
check("goal runtime keeps session hooks enabled", runtime["settings"] == {"disableAllHooks": False})
check("goal runtime still isolates setting sources", runtime["extra_args"] == ["--setting-sources", ""])

with TemporaryDirectory() as temporary:
    state = Path(temporary) / "state"
    os.environ["CLAUDE_WORKER_STATE"] = str(state)
    workerctl.STATE = state.resolve()
    workerctl.CLAUDE = "claude"
    item = workerctl.create("synthetic", str(temporary))
    check("worker created in the temporary state dir", (state / "synthetic" / "worker.json").exists() and item["model"] == "sonnet")
    check("worker state uses the selected directory", workerctl.worker_dir("synthetic").parent == state.resolve())

with TemporaryDirectory() as temporary:
    # Assemble the native command against a fake CLI and inspect the argv it got.
    state = Path(temporary) / "state"
    fake = Path(temporary) / "fake-claude"
    argv_path = Path(temporary) / "argv.json"
    fake.write_text(
        "#!" + sys.executable + "\nimport json, sys\nfrom pathlib import Path\n"
        + "Path(" + repr(str(argv_path)) + ").write_text(json.dumps(sys.argv[1:]))\n"
        + "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok'}))\n"
    )
    fake.chmod(0o755)
    workerctl.STATE = state.resolve()
    workerctl.CLAUDE = str(fake)
    os.environ["CLAUDE_WORKER_PERMISSION_MODE"] = "bypassPermissions"
    item = workerctl.create("cmdcheck", str(temporary))
    job = workerctl.submit("cmdcheck", "Run the check.", timeout=30, max_turns=64)
    finished = workerctl.wait("cmdcheck", seconds=10)
    argv = json.loads(argv_path.read_text())
    check("fake CLI job completes successfully", finished["state"] == "completed")
    check("fake CLI received --max-turns 64", "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "64")
    check("fake CLI received the default permission mode", "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "bypassPermissions")
    check("fake CLI received non-interactive prompts", "--permission-prompts" in argv and argv[argv.index("--permission-prompts") + 1] == "none")
    check("fake CLI received setting-source isolation", argv[argv.index("--setting-sources") + 1] == "")
    check("fake CLI received the persisted session id", manifest_session_id(workerctl, "cmdcheck") in argv)
    check("no max_turns is lost in the job record", read_json(state, "cmdcheck", "jobs", job["job_id"], "job.json")["max_turns"] == 64)


print(json.dumps({"passed": len(checks), "failed": sum(1 for c in checks if not c["passed"])}, ensure_ascii=False))
