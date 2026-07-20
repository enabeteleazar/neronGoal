from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import pathlib
import resource
import socket
import sys
import traceback
from dataclasses import dataclass
from typing import Any


RESULT_MARKER = "__NERON_AGENT_SANDBOX_RESULT__"
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_RDWR
    | os.O_CREAT
    | os.O_TRUNC
    | os.O_APPEND
)
_PATH_WRITE_EVENTS = {
    "os.chdir",
    "os.chmod",
    "os.chown",
    "os.link",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.replace",
    "os.rmdir",
    "os.symlink",
    "os.truncate",
    "os.unlink",
    "os.utime",
}


def _inside(path: Any, workspace: pathlib.Path) -> bool:
    if isinstance(path, int):
        return True
    try:
        resolved = pathlib.Path(os.fsdecode(path)).resolve()
        if resolved == pathlib.Path("/dev/null"):
            return True
        resolved.relative_to(workspace)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _install_audit_guard(
    workspace: pathlib.Path,
    tool_socket: pathlib.Path | None = None,
) -> None:
    def guard(event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            path = args[0] if args else None
            mode = str(args[1] or "") if len(args) > 1 else ""
            flags = int(args[2] or 0) if len(args) > 2 else 0
            if (
                any(character in mode for character in "wax+")
                or flags & _WRITE_FLAGS
            ) and not _inside(path, workspace):
                raise PermissionError(f"sandbox_write_blocked: {path}")
            return
        if event in _PATH_WRITE_EVENTS:
            paths = args[:2] if event in {"os.link", "os.rename", "os.replace"} else args[:1]
            if any(not _inside(path, workspace) for path in paths):
                raise PermissionError(f"sandbox_write_blocked: {paths}")
            return
        if event in {"os.system", "subprocess.Popen"}:
            raise PermissionError("sandbox_subprocess_blocked")
        if event == "socket.connect":
            destination = args[1] if len(args) > 1 else None
            if tool_socket is not None and str(destination) == str(tool_socket):
                return
            raise PermissionError("sandbox_network_blocked")
        if event in {"socket.bind", "socket.getaddrinfo"}:
            raise PermissionError("sandbox_network_blocked")

    sys.addaudithook(guard)


def _apply_limits(config: dict[str, Any]) -> None:
    limits = config.get("limits") or {}
    cpu_seconds = max(1, int(limits.get("cpu_seconds") or 5))
    memory_bytes = max(64 * 1024 * 1024, int(limits.get("memory_bytes") or 0))
    file_bytes = max(1024 * 1024, int(limits.get("file_bytes") or 0))
    open_files = max(16, int(limits.get("open_files") or 64))
    processes = max(1, int(limits.get("processes") or 16))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (open_files, open_files))
    if hasattr(resource, "RLIMIT_NPROC"):
        resource.setrlimit(resource.RLIMIT_NPROC, (processes, processes))


@dataclass
class _SandboxToolResult:
    ok: bool
    response: str = ""
    data: dict[str, Any] | None = None
    error: str | None = None


class _SandboxTool:
    def __init__(self, slug: str, socket_path: str) -> None:
        self.slug = slug
        self.socket_path = socket_path

    async def execute(
        self,
        payload: dict[str, Any] | None = None,
    ) -> _SandboxToolResult:
        request = json.dumps(
            {"slug": self.slug, "payload": dict(payload or {})},
            ensure_ascii=True,
        ).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(self.socket_path)
            client.sendall(request)
            raw = _recv_line(client)
        response = json.loads(raw.decode("utf-8"))
        return _SandboxToolResult(
            ok=bool(response.get("ok")),
            response=str(response.get("response") or ""),
            data=dict(response.get("data") or {}),
            error=str(response["error"]) if response.get("error") else None,
        )


class _SandboxExecutionContext:
    def __init__(
        self,
        request: str,
        context: dict[str, Any],
        metadata: dict[str, Any],
        tools: dict[str, _SandboxTool],
    ) -> None:
        self.request = request
        self.context = context
        self.metadata = metadata
        self.tools = tools

    async def run_tool(
        self,
        slug: str,
        payload: dict[str, Any] | None = None,
    ) -> _SandboxToolResult:
        tool = self.tools.get(slug)
        if tool is None:
            return _SandboxToolResult(
                ok=False,
                error=f"tool_not_bound:{slug}",
            )
        return await tool.execute(payload)


def _recv_line(client: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = client.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


async def _execute_agent(config: dict[str, Any]) -> Any:
    agent_path = pathlib.Path(config["agent_path"])
    module_spec = importlib.util.spec_from_file_location(
        "neron_sandbox_agent",
        agent_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("agent_import_spec_unavailable")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    agent = module.Agent()
    execute = agent.execute
    parameters = inspect.signature(execute).parameters
    prompt = str(config.get("prompt") or "")
    socket_path = str(config.get("tool_socket") or "")
    tools = {
        slug: _SandboxTool(slug, socket_path)
        for slug in config.get("tool_names") or []
    }
    execution_context = _SandboxExecutionContext(
        prompt,
        dict(config.get("context") or {}),
        dict(config.get("metadata") or {}),
        tools,
    )
    available = {
        "text": prompt,
        "query": prompt,
        "request": prompt,
        "execution_context": execution_context,
        "context": execution_context,
        "metadata": execution_context.metadata,
        "tools": tools,
    }
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs = {
        name: value
        for name, value in available.items()
        if accepts_kwargs or name in parameters
    }
    if kwargs:
        result = execute(**kwargs)
    elif parameters:
        first = next(iter(parameters.values()))
        if first.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            result = execute(prompt)
        else:
            result = execute()
    else:
        result = execute()
    if inspect.isawaitable(result):
        result = await result
    return result


def _run(config: dict[str, Any]) -> dict[str, Any]:
    mode = config.get("mode")
    if mode == "agent":
        result = asyncio.run(_execute_agent(config))
        return {"ok": True, "result": result}
    if mode == "pytest":
        import pytest

        returncode = pytest.main(
            [
                "-p",
                "no:cacheprovider",
                *[str(argument) for argument in config.get("arguments") or []],
            ]
        )
        return {"ok": returncode == 0, "returncode": int(returncode)}
    raise ValueError(f"unsupported_sandbox_mode: {mode}")


def main() -> None:
    config = json.loads(sys.argv[1])
    workspace = pathlib.Path(config["workspace"]).resolve()
    project_root = pathlib.Path(config["project_root"]).resolve()
    tool_socket = (
        pathlib.Path(config["tool_socket"]).resolve()
        if config.get("tool_socket")
        else None
    )
    os.chdir(workspace)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(project_root))
    _apply_limits(config)
    _install_audit_guard(workspace, tool_socket)
    try:
        payload = _run(config)
    except BaseException as exc:
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=8),
        }
    print(RESULT_MARKER + json.dumps(payload, ensure_ascii=True, default=str))


if __name__ == "__main__":
    main()
