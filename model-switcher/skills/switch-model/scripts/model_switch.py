#!/usr/bin/env python3
"""切换 Codex 的模型配置：DeepSeek 覆盖层 <-> 内置 OpenAI 默认。

只动 config.toml 里的一组托管键，其余内容（plugins / mcp_servers / projects /
desktop / notify ...）原样保留。改完需要重启 Codex 生效。

用法:
    python model_switch.py status
    python model_switch.py capture
    python model_switch.py use deepseek|chatgpt
    python model_switch.py selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台可能是 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# --- 托管项：脚本只碰这些，别的配置一律原样搬运 -----------------------------
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"

# DeepSeek 覆盖层的固定键。model 不在这里 —— 它从快照读，以保留用户在 UI 里选过的模型
DEEPSEEK_SCALARS = {
    "model_provider": '"deepseek"',
    "preferred_auth_method": '"apikey"',
    "forced_login_method": '"api"',
    "model_reasoning_effort": '"high"',
    "web_search": '"disabled"',
}
# 切回内置默认时保留的键，取自切换前的原始配置
CHATGPT_SCALARS = {"web_search": '"live"'}

# 每次切换都先摘除的托管顶层键；model_catalog_json 由脚本按 CODEX_HOME 生成
MANAGED_SCALARS = ("model",) + tuple(DEEPSEEK_SCALARS) + ("model_catalog_json",)

PROVIDER_TABLE = "model_providers.deepseek"
CATALOG_REL = "models.json"
SNAPSHOT_REL = Path("model-switch") / "deepseek.toml"
BACKUP_DIR_REL = "backup-model-switch"
BACKUP_KEEP = 10

TABLE_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*(?:#.*)?$")
# ponytail: 托管键都是单行标量；跨行的值不在托管集合里，所以按行删除是安全的
SCALAR_RE = re.compile(r"^(?P<key>[A-Za-z0-9_-]+)\s*=")


def codex_home(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def config_path(home: Path) -> Path:
    return home / "config.toml"


def snapshot_path(home: Path) -> Path:
    return home / SNAPSHOT_REL


# --- 文本手术 ---------------------------------------------------------------
def split_head_body(lines: list[str]) -> tuple[list[str], list[str]]:
    """裸键区 = 第一个表头之前的部分。TOML 要求顶层键写在这里。"""
    for i, line in enumerate(lines):
        if TABLE_RE.match(line):
            return lines[:i], lines[i:]
    return lines, []


def strip_managed_scalars(lines: list[str]) -> list[str]:
    out: list[str] = []
    skip_blank = False
    for line in lines:
        if skip_blank and line.strip() == "":
            skip_blank = False
            continue
        m = SCALAR_RE.match(line)
        if m and m.group("key") in MANAGED_SCALARS:
            skip_blank = True
            continue
        skip_blank = False
        out.append(line)
    return out


def strip_provider_table(lines: list[str]) -> list[str]:
    out: list[str] = []
    dropping = False
    for line in lines:
        m = TABLE_RE.match(line)
        if m:
            dropping = m.group("name").strip() == PROVIDER_TABLE
            if dropping:
                continue
        if not dropping:
            out.append(line)
    return out


def extract_provider_table(text: str) -> str | None:
    lines = text.splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        m = TABLE_RE.match(line)
        if not m:
            continue
        if start is None:
            if m.group("name").strip() == PROVIDER_TABLE:
                start = i
        else:
            end = i
            break
    if start is None:
        return None
    return "\n".join(lines[start:end]).strip("\n") + "\n"


def snapshot_model(snapshot_text: str) -> str | None:
    """快照里记录的 DeepSeek 模型名。旧格式快照没有这一项，返回 None 走默认值。"""
    return tomllib.loads(snapshot_text).get("model")


def make_snapshot(model: str, provider_table: str) -> str:
    return f"model = {json.dumps(model)}\n\n{provider_table}"


def build_config(
    original: str, target: str, home: Path, snippet: str | None, model: str | None = None
) -> str:
    lines = original.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    head, body = split_head_body(lines)
    head = strip_managed_scalars(head)
    while head and head[0].strip() == "":
        head.pop(0)
    body = strip_provider_table(body)

    if target == "deepseek":
        scalars = {"model": json.dumps(model or DEFAULT_DEEPSEEK_MODEL), **DEEPSEEK_SCALARS}
    else:
        scalars = CHATGPT_SCALARS
    managed_head = [f"{key} = {value}\n" for key, value in scalars.items()]
    if target == "deepseek":
        catalog = (home / CATALOG_REL).as_posix()
        managed_head.append(f'model_catalog_json = "{catalog}"\n')
    elif snippet is not None and PROVIDER_TABLE in snippet:
        raise RuntimeError("切换到 ChatGPT 时不应提供 DeepSeek provider 快照")

    text = "".join(managed_head + head + body).rstrip("\n") + "\n"
    if target == "deepseek":
        if not snippet:
            raise RuntimeError(
                "缺少 DeepSeek provider 快照（含 [model_providers.deepseek]），无法切换"
            )
        # 只取快照里的 provider 表：model 值已经在顶部写过了，顶层裸键不能再出现在表后面
        table = extract_provider_table(snippet)
        if table is None:
            raise RuntimeError("快照里找不到 [model_providers.deepseek]")
        text = text.rstrip("\n") + "\n\n" + table.strip("\n") + "\n"
    return text


# --- 校验 / 写盘 ------------------------------------------------------------
def validate(original: str, new_text: str, target: str, home: Path) -> None:
    old = tomllib.loads(original)
    new = tomllib.loads(new_text)

    if old.get("plugins") != new.get("plugins"):
        raise RuntimeError("plugins 段被改动了，中止")

    providers = new.get("model_providers", {})
    if target == "deepseek":
        if not new.get("model"):
            raise RuntimeError("model 未设置")
        if new.get("model_provider") != "deepseek":
            raise RuntimeError("model_provider 未指向 deepseek")
        if new.get("model_catalog_json") != (home / CATALOG_REL).as_posix():
            raise RuntimeError("model_catalog_json 不正确")
        if "deepseek" not in providers:
            raise RuntimeError("缺少 [model_providers.deepseek]")
    else:
        for key in MANAGED_SCALARS:
            if key in CHATGPT_SCALARS:  # 该目标下本就保留的键
                continue
            if key in new:
                raise RuntimeError(f"托管键 {key} 未被清除")
        if "deepseek" in providers:
            raise RuntimeError("[model_providers.deepseek] 未被清除")


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def backup(path: Path, home: Path) -> Path:
    folder = home / BACKUP_DIR_REL
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"config-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.toml"
    shutil.copy2(path, dest)
    for old in sorted(folder.glob("config-*.toml"))[:-BACKUP_KEEP]:
        old.unlink()
    return dest


# --- 子命令 -----------------------------------------------------------------
def cmd_status(args) -> int:
    home = codex_home(args.codex_home)
    path = config_path(home)
    if not path.exists():
        print(f"找不到 {path}")
        return 1
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    provider = data.get("model_provider")
    kind = "DeepSeek" if provider == "deepseek" else "ChatGPT（内置 OpenAI 默认）"
    snap = snapshot_path(home)
    if snap.exists():
        snap_model = snapshot_model(snap.read_text(encoding="utf-8")) or DEFAULT_DEEPSEEK_MODEL
        snap_desc = f"{snap}  (model = {snap_model})"
    else:
        snap_desc = "(缺失)"
    print(f"config: {path}")
    print(f"当前配置: {kind}")
    print(f"  model              = {data.get('model', '(内置默认)')}")
    print(f"  model_provider     = {provider or '(内置默认)'}")
    print(f"  model_catalog_json = {data.get('model_catalog_json', '(内置默认)')}")
    print(f"  web_search         = {data.get('web_search', '(未设置)')}")
    print(f"  provider 表已定义  = {'deepseek' in data.get('model_providers', {})}")
    print(f"  DeepSeek 快照      = {snap_desc}")
    return 0


def cmd_capture(args) -> int:
    home = codex_home(args.codex_home)
    path = config_path(home)
    text = path.read_text(encoding="utf-8")
    table = extract_provider_table(text)
    if table is None:
        print("当前 config.toml 里没有 [model_providers.deepseek]，无法抓取快照。")
        return 1
    model = tomllib.loads(text).get("model")
    if not model:
        print("当前 config.toml 里没有 model 键，无法记录 DeepSeek 模型名。")
        return 1
    snap = snapshot_path(home)
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(make_snapshot(model, table), encoding="utf-8")
    print(f"已写入快照: {snap}  (model = {model})")
    return 0


def cmd_use(args) -> int:
    home = codex_home(args.codex_home)
    path = config_path(home)
    target = args.target
    original = path.read_text(encoding="utf-8")

    # 切走前刷新快照：把当前 DeepSeek 覆盖（model + provider 表含 key）记下来，切回时原样
    # 恢复。这样用户在 UI 里换过的模型也会被记住，而不是被重置成默认值。
    snap = snapshot_path(home)
    table_now = extract_provider_table(original)
    model_now = tomllib.loads(original).get("model")
    if table_now is not None and model_now:
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(make_snapshot(model_now, table_now), encoding="utf-8")
        if target != "deepseek":
            print(f"已保存当前 DeepSeek 快照: {snap}  (model = {model_now})")

    snippet = None
    model = None
    if target == "deepseek":
        if not snap.exists():
            print("缺少 DeepSeek provider 快照，且当前配置里也没有该表。")
            print("请先把 DeepSeek 配置恢复一次并跑 capture，或手动创建快照。")
            return 1
        snippet = snap.read_text(encoding="utf-8")
        model = snapshot_model(snippet)

    new_text = build_config(original, target, home, snippet, model)
    validate(original, new_text, target, home)
    # 语义比较：TOML 里键顺序没有意义，等价就不动盘
    if tomllib.loads(new_text) == tomllib.loads(original):
        print(f"已经是 {target} 配置，无需改动。")
        return 0

    bkp = backup(path, home)
    try:
        write_atomic(path, new_text)
        validate(original, path.read_text(encoding="utf-8"), target, home)
    except Exception as exc:
        shutil.copy2(bkp, path)
        print(f"写入失败，已回滚到备份: {bkp}\n原因: {exc}")
        return 1

    print(f"已切换到 {target} 配置。备份: {bkp}")
    print("请重启 Codex 使配置生效。")
    return 0


SAMPLE_CONFIG = '''sandbox_mode = "workspace-write"
web_search = "disabled"
model = "deepseek-flash"   # 模拟用户在 UI 里换过模型
model_provider = "deepseek"
preferred_auth_method = "apikey"
forced_login_method = "api"
model_reasoning_effort = "high"
model_catalog_json = "C:/tmp/models.json"
notify = [ "x", "turn-ended" ]

[desktop]
appearanceTheme = "dark"

[plugins."codex-app-tools@openai-bundled"]
enabled = true

[model_providers.deepseek]
name = "deepseek"
base_url = "https://api.deepseek.com/"
wire_api = "responses"
experimental_bearer_token = "sk-test"

[projects.'e:\\demo']
trust_level = "trusted"
'''


def cmd_selftest(_args) -> int:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        cfg = config_path(home)
        (home / CATALOG_REL).write_text("{}", encoding="utf-8")
        cfg.write_text(SAMPLE_CONFIG, encoding="utf-8")

        # 1) 切到 chatgpt：清掉托管键与 provider 表，保留其余段
        assert cmd_use(argparse.Namespace(codex_home=td, target="chatgpt")) == 0
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
        assert "model_provider" not in data
        assert "model" not in data
        assert "model_catalog_json" not in data
        assert "deepseek" not in data.get("model_providers", {})
        assert data["web_search"] == "live"
        assert data["plugins"] == {"codex-app-tools@openai-bundled": {"enabled": True}}
        assert data["desktop"]["appearanceTheme"] == "dark"
        assert data["projects"]["e:\\demo"]["trust_level"] == "trusted"
        assert data["notify"] == ["x", "turn-ended"]

        # 2) 切回 deepseek：快照已生成，provider 表与托管键都回来
        assert snapshot_path(home).exists()
        assert snapshot_model(snapshot_path(home).read_text(encoding="utf-8")) == "deepseek-flash"
        assert cmd_use(argparse.Namespace(codex_home=td, target="deepseek")) == 0
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
        assert data["model_provider"] == "deepseek"
        # 方案 A：用户在 UI 里选过的模型要被保留，而不是被重置成默认值
        assert data["model"] == "deepseek-flash"
        assert data["model_catalog_json"] == (home / CATALOG_REL).as_posix()
        assert data["model_providers"]["deepseek"]["experimental_bearer_token"] == "sk-test"
        assert data["web_search"] == "disabled"
        assert data["plugins"] == {"codex-app-tools@openai-bundled": {"enabled": True}}

        # 3) 幂等：再切一次同一目标，内容不变
        before = cfg.read_text(encoding="utf-8")
        assert cmd_use(argparse.Namespace(codex_home=td, target="deepseek")) == 0
        assert cfg.read_text(encoding="utf-8") == before

    print("selftest ok")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="切换 Codex 的 DeepSeek / ChatGPT 模型配置")
    parser.add_argument("--codex-home", help="覆盖 CODEX_HOME（默认读环境变量或 ~/.codex）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="只读查看当前配置")
    sub.add_parser("capture", help="从当前 config.toml 抓取 DeepSeek provider 快照")
    use = sub.add_parser("use", help="切换到指定配置")
    use.add_argument("target", choices=["deepseek", "chatgpt"])
    sub.add_parser("selftest", help="用临时目录自检切换逻辑")

    args = parser.parse_args(argv)
    return {
        "status": cmd_status,
        "capture": cmd_capture,
        "use": cmd_use,
        "selftest": cmd_selftest,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
