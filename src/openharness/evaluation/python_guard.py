"""Build an isolated deterministic Python subprocess without network or gold access."""

import json
from pathlib import Path


def bootstrap(
    workspace: Path, resource_roots: list[Path], args: list[str], *, allow_network: bool = False
) -> str:
    roots = [str(Path(workspace).resolve()), *[str(Path(p).resolve()) for p in resource_roots]]
    package_root = str(Path(__file__).resolve().parents[1])
    # Trusted framework imports are allowed; evaluation modules and datasets remain denied.
    return f"""
import sys, os, runpy, pathlib, json, tempfile
sys.path.insert(0, {str(Path(package_root).parent)!r})
workspace = pathlib.Path({str(workspace)!r}).resolve()
roots = [pathlib.Path(p) for p in {json.dumps(roots)}]
libraries = [pathlib.Path(sys.prefix).resolve(), pathlib.Path(sys.base_prefix).resolve(), pathlib.Path({package_root!r})]
evaluation = pathlib.Path({str(Path(__file__).parent)!r})
tempfile.tempdir = str(workspace)
def allowed(path, writing=False):
    if isinstance(path, int): return True
    try: p = pathlib.Path(path).resolve()
    except (TypeError, ValueError): return False
    if p.is_relative_to(evaluation): return False
    if writing:
        if p.is_relative_to(workspace / "materials") or p.is_relative_to(workspace / ".openharness"):
            return False
        if p.is_relative_to(workspace / "session-state"):
            if any(part in ("sources", "content") for part in p.relative_to(workspace / "session-state").parts) or p.name in ("memory.json", "state.json"):
                return False
        return p.is_relative_to(workspace)
    if any(p.is_relative_to(root) for root in roots): return True
    return not writing and (any(p.is_relative_to(root) for root in libraries) or str(p) in ("/dev/null", "/dev/urandom"))
def audit(event, values):
    if event in ("os.system", "subprocess.Popen", "os.exec", "os.spawn", "ctypes.dlopen") or (
        not {allow_network!r} and event in ("socket.connect", "socket.bind", "socket.getaddrinfo")
    ):
        raise PermissionError("固定评测禁止联网、嵌套进程及动态原生库加载")
    if event == "open":
        mode = values[1] if len(values)>1 else "r"
        flags = values[2] if len(values)>2 else 0
        writing = (isinstance(mode,str) and any(c in mode for c in "wax+")) or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
        if not allowed(values[0], writing): raise PermissionError("评测文件访问越界")
    if event in ("os.listdir", "os.scandir", "os.chdir") and values and not allowed(values[0]):
        raise PermissionError("评测目录访问越界")
    if event in ("os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.symlink", "os.link"):
        for p in values[:2] if event in ("os.rename", "os.symlink", "os.link") else values[:1]:
            if not allowed(p, True): raise PermissionError("评测写入越界")
sys.dont_write_bytecode = True
# Preload binary extensions needed by document and spreadsheet libraries before restricting dlopen.
import pypdf, docx, openpyxl, decimal
sys.addaudithook(audit)
args = {json.dumps(args, ensure_ascii=False)}
if args[0] == "-c":
    exec(compile(args[1], "<evaluation-calculation>", "exec"), {{"__name__":"__main__"}})
elif args[0] == "-m":
    sys.argv = [args[1], *args[2:]]
    runpy.run_module(args[1], run_name="__main__", alter_sys=True)
else:
    sys.argv = args
    runpy.run_path(args[0], run_name="__main__")
"""
