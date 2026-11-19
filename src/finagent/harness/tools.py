"""Instance-owned tools and real execution permissions, adapted from s02-s04/s15."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
from threading import RLock, Thread
from time import monotonic

from ..errors import AgentError
from ..tools.registry import ToolRegistry as ToolPool, runtime_tool as RuntimeTool, check_schema, validate


class Permissions:
    def __init__(self, workspace, protected=(), approval=None):
        self.workspace = Path(workspace).resolve()
        self.protected = {Path(p).resolve() for p in protected}
        self.approval = approval
        self.grants = set()

    def path(self, raw, cwd=None, write=False):
        base = Path(cwd or self.workspace).resolve()
        path = (base / raw).resolve()
        if not path.is_relative_to(self.workspace):
            raise AgentError("PERMISSION_DENIED", "路径超出工作区")
        if any(path == p or path.is_relative_to(p) for p in self.protected):
            raise AgentError("PERMISSION_DENIED", "宿主存储和凭证不向文件/Shell工具开放")
        if any(part in {".git", ".env", ".aws", ".agents", ".codex"} for part in path.relative_to(self.workspace).parts):
            raise AgentError("PERMISSION_DENIED", "路径包含受保护内容")
        return path

    def check(self, name, args, kind, foreground=True):
        if kind in {"read", "workspace", "internal"}:
            return
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key in self.grants:
            return
        if not foreground or self.approval is None or not self.approval(name, args):
            raise AgentError("PERMISSION_REQUIRED", "该操作需要前台授权：" + name)
        self.grants.add(key)


class WorkspaceTools:
    """Bwrap executes arbitrary shell only inside a networkless, credentialless sandbox.

    On kernels forbidding namespaces, only a tiny non-shell read-only argv set is
    available. Arbitrary shell fails closed; it is never silently run on the host.
    """
    SAFE = {"pwd", "echo", "printf", "ls", "cat", "head", "tail", "wc", "rg"}

    def __init__(self, permissions):
        self.permissions = permissions
        self.processes = set()
        self.lock = RLock()
        self.sandbox_available = None

    def read(self, args, ctx=None, ledger=None):
        path = self.permissions.path(args["path"], getattr(ctx, "cwd", None))
        if path.stat().st_size>8_000_000:
            raise ValueError("文件超过8MB，请使用相应资料导入或分块工具")
        lines = path.read_text(encoding="utf-8").splitlines()
        offset, limit = max(0, args.get("offset", 0)), max(1, min(args.get("limit", 500), 5000))
        return {"path": str(path), "text": "\n".join(lines[offset:offset+limit]), "total_lines": len(lines)}

    def write(self, args, ctx=None, ledger=None):
        path = self.permissions.path(args["path"], getattr(ctx, "cwd", None), write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return {"path": str(path), "bytes": path.stat().st_size}

    def edit(self, args, ctx=None, ledger=None):
        path = self.permissions.path(args["path"], getattr(ctx, "cwd", None), write=True)
        text = path.read_text(encoding="utf-8")
        if not args["old_text"] or text.count(args["old_text"]) != 1:
            raise ValueError("old_text 必须恰好匹配一次")
        return self.write({"path": str(path), "content": text.replace(args["old_text"], args["new_text"], 1)}, ctx)

    def glob(self, args, ctx=None, ledger=None):
        base = Path(getattr(ctx, "cwd", None) or self.permissions.workspace)
        pattern = args["pattern"]
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("glob必须是工作区内相对模式")
        result = []
        for p in base.glob(pattern):
            try:
                self.permissions.path(str(p))
                result.append(str(p.relative_to(base)))
            except AgentError:
                continue
            if len(result) >= 200:
                break
        return {"matches": sorted(result)}

    def sandbox_args(self, command, cwd):
        root = self.permissions.workspace
        args = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
        for directory in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(directory).exists():
                args += ["--ro-bind", directory, directory]
        args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                 "--bind", str(root), "/workspace"]
        hidden = set(self.permissions.protected)
        hidden.update(root / name for name in (".env", ".git", ".aws", ".agents", ".codex"))
        for p in sorted(hidden):
            if p.exists() and p.is_relative_to(root):
                dest = "/workspace/" + str(p.relative_to(root))
                args += (["--tmpfs", dest] if p.is_dir() else ["--ro-bind", "/dev/null", dest])
        args += ["--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8",
                 "--chdir", "/workspace/" + str(cwd.relative_to(root)), "--", "/bin/sh", "-c", command]
        return args

    def shell(self, args, ctx, ledger):
        cwd = Path(getattr(ctx, "cwd", None) or self.permissions.workspace).resolve()
        self.permissions.path(str(cwd))
        if self.sandbox_available is None:
            self.sandbox_available = bool(shutil.which("bwrap"))
            if self.sandbox_available:
                probe = subprocess.run(self.sandbox_args("true", cwd), capture_output=True, timeout=5)
                self.sandbox_available = probe.returncode == 0
        command = args["command"]
        if self.sandbox_available:
            argv = self.sandbox_args(command, cwd)
        else:
            argv = shlex.split(command)
            if not argv or argv[0] not in self.SAFE or any(c in command for c in "|;&`$><\n"):
                raise AgentError("SANDBOX_UNAVAILABLE", "当前内核不支持Shell沙箱；仅可使用受限只读命令，未执行任意Shell")
            if argv[0] not in {"echo", "printf", "pwd"}:
                # Without an OS sandbox, recursive directory reads and arbitrary
                # command options could expose protected files or run a preprocessor.
                allowed_flags = {"cat": {"-n"}, "head": {"-n"}, "tail": {"-n"},
                                 "wc": {"-l", "-w", "-c"}, "ls": {"-l"}, "rg": {"-n", "-i"}}
                positional = []
                skip_count = False
                for word in argv[1:]:
                    if skip_count:
                        if not word.isdecimal():
                            raise ValueError("行数必须为整数")
                        skip_count = False
                    elif word.startswith("-"):
                        if word not in allowed_flags[argv[0]]:
                            raise AgentError("PERMISSION_DENIED", "无沙箱模式不支持该命令选项")
                        skip_count = argv[0] in {"head", "tail"} and word == "-n"
                    else:
                        positional.append(word)
                if argv[0] == "rg":
                    positional = positional[1:]  # first positional is the pattern
                if not positional:
                    raise AgentError("PERMISSION_DENIED", "无沙箱模式需要明确指定文件")
                for word in positional:
                    path = self.permissions.path(word, cwd)
                    if argv[0] != "ls" and not path.is_file():
                        raise AgentError("PERMISSION_DENIED", "无沙箱模式不递归读取目录")
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, errors="replace", start_new_session=True,
                                   env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        with self.lock:
            self.processes.add(process)
        start = monotonic()
        chunks=[[],[]]
        totals=[0,0]
        retained=[0,0]
        def drain(index,stream,cap):
            while True:
                chunk=stream.read(8192)
                if not chunk:
                    return
                totals[index]+=len(chunk)
                remaining=cap-retained[index]
                if remaining>0:
                    selected=chunk[:remaining]
                    chunks[index].append(selected)
                    retained[index]+=len(selected)
        readers=[Thread(target=drain,args=(0,process.stdout,50000),daemon=True),
                 Thread(target=drain,args=(1,process.stderr,10000),daemon=True)]
        for reader in readers:
            reader.start()
        try:
            while process.poll() is None:
                ctx.check()
                ledger.remaining()
                if monotonic() - start > 120:
                    raise AgentError("TIME_BUDGET", "Shell执行超时")
                ctx.cancelled.wait(.05)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for reader in readers:
                reader.join(timeout=2)
            process.stdout.close()
            process.stderr.close()
            with self.lock:
                self.processes.discard(process)
        return {"stdout":"".join(chunks[0]),"stderr":"".join(chunks[1]),"exit_code":process.returncode,
                "stdout_truncated":totals[0]>retained[0],"stderr_truncated":totals[1]>retained[1],
                "sandbox":"bwrap" if self.sandbox_available else "restricted_readonly_argv"}

    def close(self):
        with self.lock:
            for process in list(self.processes):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
