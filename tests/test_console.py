"""Exercise actual UTF-8 terminal editing, rather than mocking input()."""
import errno
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(sys.platform.startswith("linux"), "requires a Linux PTY and readline")
class ConsoleTests(unittest.TestCase):
    def run_terminal(self, code, keystrokes):
        import pty

        master, slave = pty.openpty()
        env = {**os.environ, "INPUTRC": "/dev/null", "TERM": "xterm", "LC_ALL": "C.UTF-8"}
        process = subprocess.Popen([sys.executable, "-c", code], stdin=slave,
                                   stdout=slave, stderr=slave, env=env)
        os.close(slave)
        output = b""
        try:
            deadline = time.monotonic() + 10
            while b" > " not in output:
                self.assertLess(time.monotonic(), deadline, output.decode(errors="replace"))
                if select.select([master], [], [], .1)[0]:
                    output += os.read(master, 4096)
            steps = [(keystrokes, None)] if isinstance(keystrokes, bytes) else keystrokes
            for keys, marker in steps:
                start = len(output)
                os.write(master, keys)
                if marker is not None:
                    deadline = time.monotonic() + 10
                    while marker not in output[start:]:
                        self.assertLess(time.monotonic(), deadline, output.decode(errors="replace"))
                        if select.select([master], [], [], .1)[0]:
                            try:
                                chunk = os.read(master, 4096)
                            except OSError as exc:
                                if exc.errno == errno.EIO:
                                    self.fail(output.decode(errors="replace"))
                                raise
                            self.assertTrue(chunk, output.decode(errors="replace"))
                            output += chunk
            process.wait(timeout=10)
            while select.select([master], [], [], .1)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output += chunk
            self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
            return output.decode("utf-8")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            os.close(master)

    def test_chinese_and_mixed_text_edit_by_character(self):
        code = ('import json; from finagent.console import read_input; '
                'print("RESULT=" + json.dumps(read_input("你 > "), ensure_ascii=False))')
        cases = [
            ("中文测试".encode() + b"\x7f" * 4 + b"OK\n", "OK"),
            ("abc中文".encode() + b"\x7f" * 3 + "新股\n".encode(), "ab新股"),
            ("中A文".encode() + b"\x1b[D\x7f" + "国\n".encode(), "中国文"),
        ]
        for keystrokes, expected in cases:
            with self.subTest(expected=expected):
                output = self.run_terminal(code, keystrokes)
                self.assertIn("RESULT=" + json.dumps(expected, ensure_ascii=False), output)

    def test_chat_can_exit_after_erasing_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "finagent.toml"
            config.write_text("", encoding="utf-8")
            code = ("from finagent.cli import main; "
                    f"raise SystemExit(main(['--config', {str(config)!r}]))")
            output = self.run_terminal(code, "中文测试".encode() + b"\x7f" * 4 + b"/exit\n")
            self.assertIn("已退出 FinSight", output)

    def test_redirected_input_preserves_chinese(self):
        result = subprocess.run(
            [sys.executable, "-c", "from finagent.console import read_input; print(read_input())"],
            input="中文输入\n", text=True, capture_output=True, timeout=10, check=True)
        self.assertEqual(result.stdout, "中文输入\n")

    def test_chat_prompt_follows_reply_and_still_accepts_steering(self):
        with tempfile.TemporaryDirectory() as directory:
            code = f'''
from pathlib import Path
from time import monotonic, sleep
from finagent.config import Settings, HarnessSettings
from finagent.harness.host import SessionHost
from finagent.harness.terminal import run_terminal
from finagent.models import ModelReply

settings = Settings(data_dir=Path({directory!r}) / "data",
                    harness=HarnessSettings(workspace=Path({directory!r}), memory_enabled=False,
                                            auto_memory=False))
class Model:
    def complete(self, messages, tools, timeout):
        print("MODEL_WAITING", flush=True)
        deadline = monotonic() + 5
        while host.steering.empty():
            if monotonic() > deadline:
                raise RuntimeError("steering was not accepted during model execution")
            sleep(.01)
        update = host.steering.get_nowait()
        host._register_user(update)
        return ModelReply({{"role": "assistant", "content": "收到补充：" + update}})
host = SessionHost(settings, model=Model())
raise SystemExit(run_terminal(settings, host=host))
'''
            output = self.run_terminal(code, [
                ("你能做些什么\n".encode(), b"MODEL_WAITING"),
                ("中文测试".encode() + b"\x7f" * 4 + "只看现金流\n".encode(),
                 "收到补充：只看现金流\r\n你 > ".encode()),
                (b"/exit\n", None),
            ])
            self.assertEqual(output.count("你 > "), 2, output)
            self.assertNotIn("你 > FinSight", output)
            self.assertIn("[工具] 本轮未调用工具。\r\nFinSight[default]", output)
            self.assertIn("\r\nFinSight[default]：收到补充：只看现金流\r\n你 > ", output)

    def test_cli_shows_actual_tool_progress_before_final_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "finagent.toml"
            config.write_text('[harness]\nmemory_enabled=false\nauto_memory=false\n', encoding="utf-8")
            (Path(directory) / "note.txt").write_text("private-source-content", encoding="utf-8")
            code = f'''
from unittest.mock import patch
from finagent.cli import main
from finagent.models import ModelReply
from tests.support import call_reply

class Model:
    def __init__(self):
        self.replies = iter((
            call_reply(("plan", "todo_write", {{"todos": [{{"content": "读取资料", "status": "in_progress"}}]}}),
                       ("read", "read_file", {{"path": "note.txt"}})),
            ModelReply({{"role": "assistant", "content": "资料已读取"}})))
    def complete(self, *args):
        return next(self.replies)
    def close(self):
        pass

with patch("finagent.harness.host.CompatibleChatModel", return_value=Model()):
    raise SystemExit(main(["--config", {str(config)!r}]))
'''
            output = self.run_terminal(code, [
                ("读取资料\n".encode(), "资料已读取\r\n你 > ".encode()),
                (b"/exit\n", None),
            ])
            markers = ("[工具] 调用 todo_write", "[Todo]", "[>] 读取资料",
                       "[工具] 调用 read_file（path=note.txt）", "[工具] read_file：已返回",
                       "FinSight[default]：资料已读取")
            positions = [output.index(marker) for marker in markers]
            self.assertEqual(positions, sorted(positions), output)
            self.assertEqual(output.count("你 > "), 2, output)
            self.assertNotIn("private-source-content", output)
            self.assertNotIn("本轮未调用工具", output)
