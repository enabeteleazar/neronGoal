from __future__ import annotations

# Rapatrié (point 3, phase 8). Frontière de sécurité réelle : isolation
# via systemd-run + utilisateur dédié neron-agent, avec repli process
# Python si systemd est indisponible. Voir _runner.py (point d'entrée
# exécuté DANS le sandbox, 100% stdlib, aucune dépendance goal/monolithe).

import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.common.paths import NERON_ROOT

_RESULT_MARKER = "__NERON_AGENT_SANDBOX_RESULT__"
_SYSTEMD_USER = "neron-agent"
_VALID_BACKENDS = {"auto", "python", "systemd"}
_VALID_SYSTEMD_SUDO_MODES = {"auto", "false", "true"}


class AgentSandbox:
    def __init__(
        self,
        *,
        project_root: Path | str = NERON_ROOT,
        workspace: Path | str | None = None,
        python_executable: str | None = None,
        timeout: int = 30,
        memory_bytes: int = 512 * 1024 * 1024,
        backend: str | None = None,
        systemd_use_sudo: str | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.workspace = Path(workspace or self.project_root / "workspace").resolve()
        self.python_executable = python_executable or sys.executable
        self.timeout = timeout
        self.memory_bytes = memory_bytes
        self.backend = (backend or os.getenv("NERON_SANDBOX_BACKEND", "auto")).lower()
        if self.backend not in _VALID_BACKENDS:
            raise ValueError(
                "NERON_SANDBOX_BACKEND must be one of: auto, python, systemd"
            )
        self.systemd_use_sudo = (
            systemd_use_sudo
            or os.getenv("NERON_SANDBOX_SYSTEMD_USE_SUDO", "auto")
        ).lower()
        if self.systemd_use_sudo not in _VALID_SYSTEMD_SUDO_MODES:
            raise ValueError(
                "NERON_SANDBOX_SYSTEMD_USE_SUDO must be one of: "
                "auto, false, true"
            )
        self._runner = Path(__file__).with_name("_runner.py").resolve()
        self._bwrap: str | None = None
        self._bwrap_available = False
        self._systemd_run: str | None = None
        self._systemd_available = False
        self._user_available = False
        self._sudo: str | None = None
        self._sudo_used = False
        self._sudo_available = False
        self._sudo_error: str | None = None

        if self.backend in {"auto", "systemd"}:
            self._systemd_run = shutil.which("systemd-run")
            self._systemd_available = bool(self._systemd_run)
            self._user_available = self._probe_system_user()
            self._sudo = shutil.which("sudo")
            self._sudo_used = self._should_use_sudo()
            self._sudo_available, self._sudo_error = self._probe_sudo()
        if self.backend == "auto":
            self._bwrap = shutil.which("bwrap")
            self._bwrap_available = self._probe_bwrap()

        self._backend_used, self._fallback_reason = self._select_backend()

    def diagnostics(self) -> dict[str, Any]:
        isolation = self._python_isolation()
        if self._backend_used == "systemd":
            isolation = "systemd"
        return {
            "backend_selected": self._backend_used,
            "backend_used": self._backend_used,
            "isolation_level": self._isolation_level(isolation),
            "systemd_available": self._systemd_available,
            "user_available": self._user_available,
            "sudo_used": self._sudo_used and self._backend_used == "systemd",
            "sudo_available": self._sudo_available,
            "sudo_error": self._sudo_error,
            "systemd_run_path": self._systemd_run,
            "fallback_reason": self._fallback_reason,
        }

    def run_pytest(
        self,
        test_file: Path | str,
        *,
        timeout: int | None = None,
        name: str = "pytest_agent",
    ) -> dict[str, Any]:
        return self._run(
            {
                "mode": "pytest",
                "arguments": ["-q", str(Path(test_file).resolve())],
            },
            timeout=timeout,
            name=name,
        )

    def execute_agent(
        self,
        agent_path: Path | str,
        prompt: str,
        *,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        tools: dict[str, Any] | None = None,
        timeout: int | None = None,
        name: str = "agent_execution",
    ) -> dict[str, Any]:
        staged_agent, stage_dir = self._stage_agent(agent_path)
        try:
            result = self._run(
                {
                    "mode": "agent",
                    "agent_path": str(staged_agent),
                    "prompt": prompt,
                    "context": dict(context or {}),
                    "metadata": dict(metadata or {}),
                    "tool_names": sorted(tools or {}),
                },
                timeout=timeout,
                name=name,
                tool_bindings=dict(tools or {}),
            )
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)
        payload = result.get("payload") or {}
        if result["returncode"] != 0 or not payload.get("ok"):
            return {
                "ok": False,
                "error": (
                    payload.get("error")
                    or result.get("error")
                    or result.get("stderr_tail")
                    or "agent_sandbox_failed"
                ),
                "sandbox": result,
            }
        return {
            "ok": True,
            "result": payload.get("result"),
            "sandbox": result,
        }

    def _stage_agent(
        self,
        agent_path: Path | str,
    ) -> tuple[Path, Path]:
        source = Path(agent_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        sandbox_tmp = self.workspace / ".sandbox_tmp"
        sandbox_tmp.mkdir(parents=True, exist_ok=True)
        sandbox_tmp.chmod(0o1777)
        stage_dir = sandbox_tmp / f"agent-{uuid.uuid4().hex}"
        stage_dir.mkdir(mode=0o755)
        stage_dir.chmod(0o755)
        staged = stage_dir / source.name
        shutil.copyfile(source, staged)
        staged.chmod(0o444)
        return staged, stage_dir

    def _run(
        self,
        config: dict[str, Any],
        *,
        timeout: int | None,
        name: str,
        tool_bindings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        sandbox_tmp = self.workspace / ".sandbox_tmp"
        sandbox_tmp.mkdir(parents=True, exist_ok=True)
        sandbox_tmp.chmod(0o1777)
        effective_timeout = max(1, int(timeout or self.timeout))
        payload = {
            **config,
            "project_root": str(self.project_root),
            "workspace": str(self.workspace),
            "limits": {
                "cpu_seconds": effective_timeout + 1,
                "memory_bytes": self.memory_bytes,
                "file_bytes": 16 * 1024 * 1024,
                "open_files": 64,
                "processes": 16,
            },
        }
        tool_server = self._start_tool_server(
            sandbox_tmp,
            tool_bindings or {},
        )
        if tool_server is not None:
            payload["tool_socket"] = str(tool_server["path"])
        command = [
            self.python_executable,
            "-I",
            str(self._runner),
            json.dumps(payload, ensure_ascii=True),
        ]
        isolation = self._python_isolation()
        if self._backend_used == "systemd":
            command = self._systemd_command(command, effective_timeout)
            isolation = "systemd"
        elif isolation == "bubblewrap":
            command = self._bwrap_command(command, sandbox_tmp)

        environment = {
            "HOME": str(self.workspace),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(sandbox_tmp),
        }
        started_at = datetime.now(timezone.utc).isoformat()
        preflight_error = self._systemd_preflight_error()
        if preflight_error:
            self._stop_tool_server(tool_server)
            return {
                "name": name,
                "command": self._redacted_command(command),
                "returncode": 1,
                "stdout_tail": "",
                "stderr_tail": "",
                "error": preflight_error,
                "timed_out": False,
                "isolation": isolation,
                "backend_error": True,
                **self._result_diagnostics(isolation),
                "ran_at": started_at,
            }
        try:
            process = subprocess.Popen(
                command,
                cwd=self.workspace,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                return {
                    "name": name,
                    "command": self._redacted_command(command),
                    "returncode": -signal.SIGKILL,
                    "stdout_tail": stdout[-4000:],
                    "stderr_tail": stderr[-4000:],
                    "error": "agent_sandbox_timeout",
                    "timed_out": True,
                    "isolation": isolation,
                    **self._result_diagnostics(isolation),
                    "ran_at": started_at,
                }
        except Exception as exc:
            return {
                "name": name,
                "command": self._redacted_command(command),
                "returncode": 1,
                "stdout_tail": "",
                "stderr_tail": "",
                "error": f"agent_sandbox_error: {exc}",
                "timed_out": False,
                "isolation": isolation,
                "backend_error": self._backend_used == "systemd",
                **self._result_diagnostics(isolation),
                "ran_at": started_at,
            }
        finally:
            self._stop_tool_server(tool_server)

        result = {
            "name": name,
            "command": self._redacted_command(command),
            "returncode": process.returncode,
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-4000:],
            "timed_out": False,
            "isolation": isolation,
            **self._result_diagnostics(isolation),
            "ran_at": started_at,
        }
        result["payload"] = self._extract_payload(stdout)
        if "returncode" in result["payload"]:
            result["returncode"] = int(result["payload"]["returncode"])
        if not result["payload"].get("ok"):
            result["error"] = result["payload"].get("error") or "agent_sandbox_failed"
            if self._backend_used == "systemd" and result["payload"].get(
                "error"
            ) == "agent_sandbox_no_result":
                result["backend_error"] = True
        return result

    def _start_tool_server(
        self,
        sandbox_tmp: Path,
        bindings: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not bindings:
            return None
        socket_path = sandbox_tmp / f"tools-{uuid.uuid4().hex}.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        socket_path.chmod(0o666)
        server.listen(8)
        server.settimeout(0.2)
        stop = threading.Event()

        def serve() -> None:
            while not stop.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with connection:
                    try:
                        request = json.loads(
                            self._recv_line(connection).decode("utf-8")
                        )
                        slug = str(request.get("slug") or "")
                        binding = bindings.get(slug)
                        if binding is None:
                            response = {
                                "ok": False,
                                "error": f"tool_not_bound:{slug}",
                            }
                        else:
                            value = binding.execute(
                                dict(request.get("payload") or {})
                            )
                            if hasattr(value, "__await__"):
                                import asyncio

                                value = asyncio.run(value)
                            response = (
                                value.to_dict()
                                if hasattr(value, "to_dict")
                                else dict(value or {})
                            )
                    except Exception as exc:
                        response = {
                            "ok": False,
                            "error": f"tool_bridge_error:{exc}",
                        }
                    connection.sendall(
                        json.dumps(
                            response,
                            ensure_ascii=True,
                            default=str,
                        ).encode("utf-8")
                        + b"\n"
                    )

        thread = threading.Thread(
            target=serve,
            name="neron-agent-tool-bridge",
            daemon=True,
        )
        thread.start()
        return {
            "server": server,
            "stop": stop,
            "thread": thread,
            "path": socket_path,
        }

    @staticmethod
    def _recv_line(connection: socket.socket) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return b"".join(chunks).split(b"\n", 1)[0]

    @staticmethod
    def _stop_tool_server(server_state: dict[str, Any] | None) -> None:
        if server_state is None:
            return
        server_state["stop"].set()
        server_state["server"].close()
        server_state["thread"].join(timeout=1)
        Path(server_state["path"]).unlink(missing_ok=True)

    def _select_backend(self) -> tuple[str, str | None]:
        if self.backend == "python":
            return "python", None
        if self.backend == "systemd":
            return "systemd", None
        if not self._systemd_available:
            return "python", "systemd_run_unavailable"
        if not self._user_available:
            return "python", "neron_agent_user_unavailable"
        if self._sudo_used and not self._sudo_available:
            return "python", "systemd_sudo_unavailable"
        return "systemd", None

    def _systemd_preflight_error(self) -> str | None:
        if self._backend_used != "systemd":
            return None
        if not self._systemd_available:
            return "agent_sandbox_systemd_unavailable"
        if not self._user_available:
            return "agent_sandbox_user_unavailable"
        if self._sudo_used and not self._sudo_available:
            return "agent_sandbox_sudo_unavailable"
        return None

    def _probe_system_user(self) -> bool:
        try:
            pwd.getpwnam(_SYSTEMD_USER)
        except KeyError:
            return False
        return True

    def _should_use_sudo(self) -> bool:
        if self.systemd_use_sudo == "true":
            return True
        if self.systemd_use_sudo == "false":
            return False
        return os.geteuid() != 0

    def _probe_sudo(self) -> tuple[bool, str | None]:
        if not self._sudo_used:
            return bool(self._sudo), None
        if (
            self.backend == "python"
            or not self._systemd_available
            or not self._user_available
        ):
            return bool(self._sudo), None
        if not self._sudo:
            return False, "sudo_not_found"
        try:
            completed = subprocess.run(
                [self._sudo, "-n", self._systemd_run, "--version"],
                text=True,
                capture_output=True,
                timeout=3,
            )
        except Exception as exc:
            return False, f"sudo_probe_failed: {exc}"
        if completed.returncode == 0:
            return True, None
        detail = (completed.stderr or completed.stdout or "").strip()
        error = f"sudo_exit_{completed.returncode}"
        if detail:
            error = f"{error}: {detail[-500:]}"
        return False, error

    def _python_isolation(self) -> str:
        if self.backend == "auto" and self._bwrap_available and self._bwrap:
            return "bubblewrap"
        return "python_audit"

    def _isolation_level(self, isolation: str) -> str:
        return {
            "systemd": "kernel_systemd",
            "bubblewrap": "kernel_bubblewrap",
            "python_audit": "process_python_audit",
        }[isolation]

    def _result_diagnostics(self, isolation: str) -> dict[str, Any]:
        return {
            "backend_selected": self._backend_used,
            "backend_used": self._backend_used,
            "isolation_level": self._isolation_level(isolation),
            "systemd_available": self._systemd_available,
            "user_available": self._user_available,
            "sudo_used": self._sudo_used and self._backend_used == "systemd",
            "sudo_available": self._sudo_available,
            "sudo_error": self._sudo_error,
            "systemd_run_path": self._systemd_run,
            "fallback_reason": self._fallback_reason,
        }

    def _probe_bwrap(self) -> bool:
        if not self._bwrap:
            return False
        try:
            completed = subprocess.run(
                [
                    self._bwrap,
                    "--die-with-parent",
                    "--new-session",
                    "--unshare-net",
                    "--ro-bind",
                    "/",
                    "/",
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                    "/usr/bin/true",
                ],
                text=True,
                capture_output=True,
                timeout=3,
            )
        except Exception:
            return False
        return completed.returncode == 0

    def _bwrap_command(self, command: list[str], sandbox_tmp: Path) -> list[str]:
        return [
            str(self._bwrap),
            "--die-with-parent",
            "--new-session",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(self.workspace),
            str(self.workspace),
            "--tmpfs",
            "/tmp",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--chdir",
            str(self.workspace),
            "--clearenv",
            "--setenv",
            "HOME",
            str(self.workspace),
            "--setenv",
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
            "--setenv",
            "TMPDIR",
            str(sandbox_tmp),
            *command,
        ]

    def _systemd_command(
        self,
        command: list[str],
        effective_timeout: int,
    ) -> list[str]:
        executable = self._systemd_run or "systemd-run"
        systemd_command = [
            executable,
            f"--uid={_SYSTEMD_USER}",
            "--property=DynamicUser=no",
            "--property=NoNewPrivileges=yes",
            "--property=PrivateTmp=yes",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=yes",
            f"--property=ReadWritePaths={self.workspace}",
            f"--property=WorkingDirectory={self.workspace}",
            f"--setenv=HOME={self.workspace}",
            f"--setenv=TMPDIR={self.workspace / '.sandbox_tmp'}",
            "--setenv=PYTHONDONTWRITEBYTECODE=1",
            "--property=MemoryMax=256M",
            "--property=CPUQuota=50%",
            f"--property=RuntimeMaxSec={min(effective_timeout, 30)}",
            "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=SystemCallFilter=@system-service",
            "--wait",
            "--pipe",
            "--",
            *command,
        ]
        if self._sudo_used:
            return [self._sudo or "sudo", "-n", *systemd_command]
        return systemd_command

    def _extract_payload(self, stdout: str) -> dict[str, Any]:
        line = next(
            (
                item[len(_RESULT_MARKER) :]
                for item in reversed(stdout.splitlines())
                if item.startswith(_RESULT_MARKER)
            ),
            None,
        )
        if line is None:
            return {"ok": False, "error": "agent_sandbox_no_result"}
        try:
            return dict(json.loads(line))
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"agent_sandbox_invalid_result: {exc}"}

    def _redacted_command(self, command: list[str]) -> list[str]:
        redacted = list(command)
        if redacted:
            redacted[-1] = "[SANDBOX_CONFIG]"
        return redacted
