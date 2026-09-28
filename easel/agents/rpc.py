"""Bounded, full-duplex JSONL RPC over a child process; no shell invocation."""
from __future__ import annotations

import asyncio
import json
import os
import signal
from collections import deque

from .base import AgentError


class RpcProcess:
    def __init__(self, command, cwd, env, *, jsonrpc=True):
        self.command, self.cwd, self.env = command, cwd, env
        self.jsonrpc = jsonrpc
        self.process = None
        self.pending = {}
        self.sequence = 0
        self.tasks = set()
        self.stderr = deque(maxlen=20)
        self.on_notification = None
        self.on_request = None

    async def start(self):
        self.closed = asyncio.get_running_loop().create_future()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command, cwd=self.cwd, env=self.env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024,
                start_new_session=os.name != "nt")
        except OSError as exc:
            raise AgentError("无法启动 Agent CLI，请检查可执行文件及运行环境") from exc
        self.reader = asyncio.create_task(self._read())
        self.err_reader = asyncio.create_task(self._read_stderr())

    async def send(self, message):
        if self.jsonrpc:
            message = {"jsonrpc": "2.0", **message}
        if not self.process or self.process.returncode is not None:
            raise AgentError("Agent 进程已退出")
        try:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AgentError("Agent 连接中断；本轮不会自动重发") from exc

    async def request(self, method, params, timeout=30):
        self.sequence += 1
        rid = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.send({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise AgentError(f"Agent {method} 超时；请检查 CLI 登录和网络") from exc
        finally:
            self.pending.pop(rid, None)

    async def notify(self, method, params=None):
        await self.send({"method": method, "params": params or {}})

    async def _answer(self, message):
        try:
            if not self.on_request:
                raise AgentError("客户端不支持此请求")
            result = await self.on_request(message["method"], message.get("params", {}))
            await self.send({"id": message["id"], "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await self.send({"id": message["id"], "error": {"code": -32601, "message": str(exc)}})
            except AgentError:
                pass

    async def _read(self):
        error = AgentError("Agent 进程提前退出；本轮未确认完成，请检查 CLI 登录或网络后继续会话")
        try:
            while line := await self.process.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # some CLIs print startup banners before JSONL
                if not isinstance(msg, dict):
                    continue
                if "method" in msg:
                    if "id" in msg:
                        task = asyncio.create_task(self._answer(msg))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                    elif self.on_notification:
                        self.on_notification(msg["method"], msg.get("params", {}))
                elif msg.get("id") in self.pending:
                    fut = self.pending[msg["id"]]
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(AgentError(str(msg["error"].get("message", "Agent 请求失败"))))
                        else:
                            fut.set_result(msg.get("result", {}))
        except asyncio.CancelledError:
            raise
        except Exception:
            error = AgentError("Agent 事件流读取失败；本轮不会自动重发")
        finally:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(error)
            if not self.closed.done():
                self.closed.set_result(error)

    async def _read_stderr(self):
        try:
            while line := await self.process.stderr.readline():
                self.stderr.append(line.decode("utf-8", "replace")[:2000])
        except (ValueError, asyncio.CancelledError):
            pass  # never relay raw stderr or CLI credentials to a browser

    async def wait_for(self, future):
        done, _ = await asyncio.wait([future, self.closed], return_when=asyncio.FIRST_COMPLETED)
        if future in done:
            return future.result()
        raise self.closed.result()

    async def close(self):
        proc = self.process
        if not proc:
            return
        if proc.returncode is None:
            try:
                if os.name == "nt":
                    proc.terminate()
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            except ProcessLookupError:
                pass
        if os.name != "nt":
            # A CLI can exit before a tool child does. Reap the process group
            # we created even if the parent has already terminated.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        tasks = [self.reader, self.err_reader, *self.tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
