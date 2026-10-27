#!/usr/bin/env python3
"""Fake `dsh` CLI for integration tests of wechatbridge.dsh.

Simulates ``dsh --profile headless <task>`` — prints the final assistant
message on stdout and exits 0 only for a completed turn. Behaviour is
controlled by FAKE_DSH_MODE (read from the environment each invocation):

  ok                  (default) reply "first: <prompt>", exit 0.
  artifact_link       reply with a markdown file:/// link, create the file.
  artifact_relative   reply with a relative ./doc.md link, create the file.
  internal_metadata   reply with links to a real file AND a .dsh/internal file
                      (tests that run_dsh does not treat DSH_HOME internals as
                      workspace artifacts is out of scope here; the link is
                      still under cwd so send_artifacts_back decides).
  empty               no stdout, exit 0.
  fail                stderr "boom: something went wrong", exit 1.
  dsh_error           stderr "dsh: LLMError: model is currently overloaded",
                      exit 1.
  not_logged_in       stderr "dsh: AuthError: please log in first", exit 1.
  timeout             sleep far longer than the bridge timeout.

If FAKE_DSH_LOG is set (path), every invocation appends one line so tests can
assert exactly how many times (and with what args) the CLI was launched.
"""

import os
import sys
import time


def _reply(prompt):
    return "first(%s)" % prompt


def main():
    args = sys.argv[1:]
    mode = os.environ.get("FAKE_DSH_MODE", "ok")

    profile = ""
    task_args = []
    i = 0
    in_options = True

    while i < len(args):
        arg = args[i]
        if in_options:
            if arg == "--":
                in_options = False
                i += 1
                continue
            if arg == "--profile":
                if i + 1 < len(args):
                    profile = args[i + 1]
                    i += 2
                    continue
                else:
                    sys.stderr.write("dsh: error: option --profile requires an argument\n")
                    sys.stderr.flush()
                    return 1
            if arg.startswith("-"):
                sys.stderr.write("dsh: error: unrecognized option: %s\n" % arg)
                sys.stderr.flush()
                return 1
            task_args.append(arg)
            i += 1
        else:
            task_args.append(arg)
            i += 1

    # env 任务优先（常驻会话模式），否则取 argv 位置参数
    env_task = os.environ.get("DSH_BRIDGE_TASK", "")
    prompt = env_task if env_task else " ".join(task_args)

    log_path = os.environ.get("FAKE_DSH_LOG")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write("invoked mode=%s profile=%s session=%s task=%s args=%s\n" % (mode, profile, os.environ.get("DSH_BRIDGE_SESSION_ID", ""), prompt, " ".join(args)))
        except OSError:
            pass

    if mode == "timeout":
        time.sleep(60)
        return 0
    if mode == "empty":
        return 0
    if mode == "fail":
        sys.stderr.write("boom: something went wrong\n")
        sys.stderr.flush()
        return 1
    if mode == "dsh_error":
        sys.stderr.write("dsh: LLMError: model is currently overloaded\n")
        sys.stderr.flush()
        return 1
    if mode == "not_logged_in":
        sys.stderr.write("dsh: AuthError: please log in first\n")
        sys.stderr.flush()
        return 1

    if mode == "artifact_link":
        path = os.path.join(os.getcwd(), "result.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("generated content")
        sys.stdout.write("[result.txt](file://%s)\n%s\n" % (path, _reply(prompt)))
        sys.stdout.flush()
        return 0

    if mode == "artifact_relative":
        path = os.path.join(os.getcwd(), "doc.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# doc")
        sys.stdout.write("see [doc.md](./doc.md) for details\n%s\n" % _reply(prompt))
        sys.stdout.flush()
        return 0

    if mode == "internal_metadata":
        real = os.path.join(os.getcwd(), "report.pdf")
        with open(real, "wb") as f:
            f.write(b"%PDF-1.4")
        internal = os.path.join(os.getcwd(), ".dsh", "internal", "meta.json")
        os.makedirs(os.path.dirname(internal), exist_ok=True)
        with open(internal, "w", encoding="utf-8") as f:
            f.write("{}")
        sys.stdout.write(
            "[report.pdf](file://%s) and [meta](file://%s)\n%s\n"
            % (real, internal, _reply(prompt))
        )
        sys.stdout.flush()
        return 0

    sys.stdout.write("%s\n" % _reply(prompt))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
