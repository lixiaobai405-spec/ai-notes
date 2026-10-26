"""Bounded POSIX operator processes. Windows execution fails before spawning."""

from contextlib import suppress
import json
import os
import signal
import subprocess
import threading

from site_backup_state import ScheduleError

MAX_OUTPUT = 64 * 1024


def kill_group(process):
    # A reaped leader can leave descendants holding the stdout pipe open.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def execute(command, *, cwd, timeout, payload=None):
    if os.name != "posix":
        raise ScheduleError("schedule_platform_unsupported")
    process = timer = None
    expired = threading.Event()
    try:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["DEEPSEEK_API_KEY"] = ""
        process = subprocess.Popen([str(arg) for arg in command], cwd=cwd, env=env, shell=False,
                                   stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE if payload is None else subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)

        def expire():
            expired.set()
            with suppress(Exception):
                kill_group(process)

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        if payload is not None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if len(encoded) > MAX_OUTPUT:
                raise ScheduleError("schedule_notification_invalid")
            process.stdin.write(encoded)
            process.stdin.close()
            output = b""
        else:
            output = process.stdout.read(MAX_OUTPUT + 1)
            if len(output) > MAX_OUTPUT:
                raise ScheduleError("schedule_process_output_limit")
        code = process.wait(timeout=timeout + 10)
        if expired.is_set():
            raise ScheduleError("schedule_process_timeout")
        if code:
            raise ScheduleError("schedule_process_failed")
        return output
    except ScheduleError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ScheduleError("schedule_process_timeout" if expired.is_set() else "schedule_process_failed") from None
    finally:
        if timer:
            timer.cancel()
            timer.join(timeout=2)
        if process:
            with suppress(Exception):
                kill_group(process)
            with suppress(Exception):
                process.wait(timeout=10)
            for stream in (process.stdout, process.stdin):
                if stream:
                    with suppress(Exception):
                        stream.close()
