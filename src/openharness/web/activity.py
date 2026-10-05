"""Small public projections of tool operations; never expose raw results."""

from urllib.parse import urlsplit, urlunsplit


def describe_tool(name: str, arguments: dict) -> dict:
    category, label, keys = {
        "read_file": ("read", "读取文件", ("path", "file_path")),
        "grep": ("search", "搜索文件", ("pattern",)),
        "glob": ("search", "查找文件", ("pattern",)),
        "bash": ("command", "执行命令", ("command",)),
        "web_search": ("search", "搜索网页", ("query",)),
        "web_fetch": ("fetch", "读取网页", ("url",)),
        "write_file": ("other", "写入文件", ("path",)),
        "edit_file": ("other", "编辑文件", ("path",)),
        "research_memory": ("other", "更新研究记录", ()),
        "ask_user_question": ("other", "补充研究信息", ()),
        "notebook_edit": ("other", "编辑笔记本", ("notebook_path",)),
        "image_generation": ("other", "生成图像", ()),
        "image_to_text": ("read", "读取图像", ()),
        "config": ("other", "管理配置", ()),
        "mcp_auth": ("other", "外部服务认证", ()),
        "skill": ("other", "使用研究技能", ()),
        "tool_search": ("search", "查找工具", ()),
        "sleep": ("other", "等待", ()),
    }.get(name, ("other", "执行工具操作", ()))
    if name.startswith("mcp__"):
        label = "外部工具操作"
    target = next((arguments[key] for key in keys if isinstance(arguments.get(key), str)), "")
    if category == "fetch" and target:
        try:
            url = urlsplit(target)
            target = urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
        except ValueError:
            target = "网页"
    return {"category": category, "label": label, "target": target[:240]}
