"""Client wiring: mcp-config snippets and the persistent server token."""

from __future__ import annotations

import argparse
import json
import platform
import shlex
import sys

from ifc_console.cli.common import _MODES, _make_store, _new_store, _port

# Pinned so npx serves its cached install instead of asking the npm registry
# for "latest" on every launch. The uncached first run downloads the package,
# which can exceed Claude Desktop's 60 second startup timeout; docs and
# /connect tell users to warm the cache once with this exact spec (npx caches
# each version spec separately, so the pre-warm must match).
MCP_REMOTE_SPEC = "mcp-remote@0.1.38"


def add_mcp_config_parser(sub: argparse._SubParsersAction) -> None:
    cfg = sub.add_parser("mcp-config", help="Print client wiring snippets.")
    cfg.add_argument(
        "--client",
        choices=["claude-code", "claude-desktop", "cursor", "vscode", "codex"],
        default="claude-code",
    )
    cfg.add_argument("--transport", choices=["bridge", "http", "stdio"], default=None)
    cfg.add_argument("--file", default=None, help="Model path to pin in stdio snippets.")
    cfg.add_argument("--mode", choices=_MODES, default=None)
    cfg.add_argument("--port", type=_port, default=None)
    cfg.add_argument(
        "--tools",
        choices=["full", "lean"],
        default=None,
        help="Tool profile the client should list; lean lists 14 tools plus a search.",
    )
    cfg.set_defaults(func=_cmd_mcp_config)


def add_token_parser(sub: argparse._SubParsersAction) -> None:
    tok = sub.add_parser("token", help="Manage the persistent server token.")
    tok_sub = tok.add_subparsers(dest="token_cmd", required=True)
    tok_show = tok_sub.add_parser("show", help="Print the current token.")
    tok_show.set_defaults(func=_cmd_token_show)
    tok_rotate = tok_sub.add_parser(
        "rotate", help="Generate a new token (existing client configs stop working)."
    )
    tok_rotate.set_defaults(func=_cmd_token_rotate)
    tok_path = tok_sub.add_parser("path", help="Print where the token is stored.")
    tok_path.set_defaults(func=_cmd_token_path)


def _claude_code_http_cmd(port: int, token: str | None, tools: str | None = None) -> str:
    url = _mcp_url(port, tools)
    # User scope makes this a one-time machine setup instead of tying it to
    # whichever project directory the command happened to run from.
    cmd = f"claude mcp add --transport http --scope user ifc-console {url}"
    if token:
        cmd += f' --header "Authorization: Bearer {token}"'
    return cmd


def _mcp_url(port: int, tools: str | None = None) -> str:
    """The MCP endpoint, with a tool profile in the path when one was chosen."""
    return f"http://127.0.0.1:{port}/mcp" + (f"/{tools}" if tools in ("full", "lean") else "")


def _bridge_argv(port: int, token: str | None = None, tools: str | None = None) -> list[str]:
    """How a client should launch the stdio bridge.

    An absolute path when we can find one: GUI clients (Claude Desktop) do not
    inherit the shell PATH, and "command not found" is the most common wiring
    failure. uvx is the fallback for an ephemeral install.
    """
    import shutil

    exe = shutil.which("ifc-console")
    argv = [exe, "bridge"] if exe else ["uvx", "ifc-console", "bridge"]
    if port != 8383:
        argv += ["--port", str(port)]
    if token:
        argv += ["--token", token]
    if tools in ("full", "lean"):
        argv += ["--tools", tools]
    return argv


def _quote_argv(argv: list[str]) -> str:
    if platform.system() == "Windows":
        import subprocess

        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def build_config_snippet(
    client: str,
    transport: str | None,
    *,
    port: int,
    file: str | None,
    mode: str,
    token: str | None,
    bridge_token: str | None = None,
    tools: str | None = None,
) -> str:
    stdio_args = ["ifc-console", "serve", "--stdio", "--mode", mode]
    if file:
        stdio_args += ["--file", file]
    url = _mcp_url(port, tools)
    headers = {"Authorization": f"Bearer {token or '<TOKEN>'}"}

    # Every default snippet attaches to the reusable terminal-owned session.
    # The bridge is the default because it makes start order irrelevant: the
    # client can launch before ifc-console and still connect. A model path
    # belongs only to an explicitly requested standalone stdio process.
    transport = transport or "bridge"
    bridge_argv = _bridge_argv(port, bridge_token, tools)

    if client == "claude-code":
        if transport == "http":
            return _claude_code_http_cmd(port, token or "<TOKEN>", tools)
        argv = bridge_argv if transport == "bridge" else ["uvx", *stdio_args]
        return f"claude mcp add --scope user ifc-console -- {_quote_argv(argv)}"
    if client == "claude-desktop":
        if transport == "bridge":
            snippet = {
                "mcpServers": {"ifc-console": {"command": bridge_argv[0], "args": bridge_argv[1:]}}
            }
        elif transport == "stdio":
            snippet = {"mcpServers": {"ifc-console": {"command": "uvx", "args": stdio_args}}}
        else:
            # Claude Desktop starts local MCP entries over stdio. mcp-remote
            # is the bridge to this terminal's Streamable HTTP endpoint.
            snippet = {
                "mcpServers": {
                    "ifc-console": {
                        "command": "npx",
                        "args": [
                            "-y",
                            MCP_REMOTE_SPEC,
                            url,
                            "--allow-http",
                            "--transport",
                            "http-only",
                            "--header",
                            "Authorization:${IFC_CONSOLE_AUTH_HEADER}",
                        ],
                        "env": {"IFC_CONSOLE_AUTH_HEADER": headers["Authorization"]},
                    }
                }
            }
        return json.dumps(snippet, indent=2)
    if client == "cursor":
        if transport == "bridge":
            snippet = {
                "mcpServers": {"ifc-console": {"command": bridge_argv[0], "args": bridge_argv[1:]}}
            }
        elif transport == "stdio":
            snippet = {"mcpServers": {"ifc-console": {"command": "uvx", "args": stdio_args}}}
        else:
            snippet = {"mcpServers": {"ifc-console": {"url": url, "headers": headers}}}
        return json.dumps(snippet, indent=2)
    if client == "vscode":
        if transport == "bridge":
            snippet = {
                "servers": {
                    "ifc-console": {
                        "type": "stdio",
                        "command": bridge_argv[0],
                        "args": bridge_argv[1:],
                    }
                }
            }
        elif transport == "stdio":
            snippet = {
                "servers": {"ifc-console": {"type": "stdio", "command": "uvx", "args": stdio_args}}
            }
        else:
            snippet = {"servers": {"ifc-console": {"type": "http", "url": url, "headers": headers}}}
        return json.dumps(snippet, indent=2)
    if client == "codex":
        if transport in ("bridge", "stdio"):
            argv = bridge_argv if transport == "bridge" else ["uvx", *stdio_args]
            arg_list = ", ".join(json.dumps(a, ensure_ascii=False) for a in argv[1:])
            return (
                f"[mcp_servers.ifc-console]\n"
                f"command = {json.dumps(argv[0], ensure_ascii=False)}\n"
                f"args = [{arg_list}]"
            )
        authorization = json.dumps(headers["Authorization"])
        return (
            f"[mcp_servers.ifc-console]\n"
            f"url = {json.dumps(url)}\n"
            f"http_headers = {{ Authorization = {authorization} }}"
        )
    raise ValueError(client)


def _cmd_mcp_config(args: argparse.Namespace) -> int:
    store = _make_store(args)
    port = args.port if args.port is not None else store.settings.server.port
    mode = args.mode or store.settings.mode.default
    persistent = store.settings.server.persistent_token
    if args.transport == "bridge" and not persistent:
        print(
            "error: bridge configs require server.persistent_token=true. Use "
            "--transport stdio, or use --transport http with the current run's token.",
            file=sys.stderr,
        )
        return 2
    transport = args.transport or ("bridge" if persistent else "stdio")
    # The default bridge snippet stays valid across restarts without placing
    # the persistent token in a client configuration file.
    token = None
    if persistent and store.settings.server.token_in_config_snippets:
        token = store.load_server_token()
    snippet = build_config_snippet(
        args.client,
        transport,
        port=port,
        file=args.file,
        mode=mode,
        token=token,
        tools=getattr(args, "tools", None),
    )
    print(snippet)
    if "<TOKEN>" in snippet:
        if not store.settings.server.token_in_config_snippets:
            note = (
                "<TOKEN> is hidden by server.token_in_config_snippets; replace it "
                "manually (`ifc-console token show` or /copy token)."
            )
        else:
            note = (
                "<TOKEN> is this run's bearer token; the running ifc-console console "
                "can copy a complete setup with /copy <client>."
            )
        print(f"\nnote: {note}", file=sys.stderr)
    elif transport != "stdio":
        print(
            "note: configure once; this shared-console setup follows whichever model you "
            "open with /file and keeps working across ifc-console restarts (rotate "
            "the token with `ifc-console token rotate`).",
            file=sys.stderr,
        )
    return 0


def _cmd_token_show(_args: argparse.Namespace) -> int:
    store = _new_store()
    if not store.settings.server.persistent_token:
        print(
            "per-run tokens are enabled (server.persistent_token=false); "
            "each run prints its own token at startup.",
            file=sys.stderr,
        )
        return 3
    print(store.load_server_token())
    return 0


def _cmd_token_rotate(_args: argparse.Namespace) -> int:
    store = _new_store()
    token = store.rotate_server_token()
    print(token)
    print(
        "token rotated; update your MCP client configs "
        "(`ifc-console mcp-config` or /connect in the console) and restart ifc-console.",
        file=sys.stderr,
    )
    return 0


def _cmd_token_path(_args: argparse.Namespace) -> int:
    print(_new_store().token_file)
    return 0
