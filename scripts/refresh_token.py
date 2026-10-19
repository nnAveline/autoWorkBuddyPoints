#!/usr/bin/env python3
"""读取 WorkBuddy 本机登录态，导出 token 并写入 GitHub Actions secrets。

用法：
  python3 scripts/refresh_token.py --repo owner/name
  python3 scripts/refresh_token.py                 # 只生成本地 secrets.env
  python3 scripts/refresh_token.py --check         # 只验证能否解密

脚本不会把 token 打印到终端；有 gh 时直接调用 `gh secret set`，
没有 gh 时写到 ~/.workbuddy/cache/workbuddy-daily/secrets.env（0600）。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECRYPT_JS = HERE / "decrypt_token.js"
CACHE_DIR = Path.home() / ".workbuddy" / "cache" / "workbuddy-daily"
SECRETS_FILE = CACHE_DIR / "secrets.env"


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def command_path(name: str) -> str:
    return shutil.which(name) or ""


def workbuddy_binary_candidates() -> list[str]:
    home = Path.home()
    if sys.platform == "darwin":
        return [
            "/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy",
            str(home / "Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy"),
        ]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", "")
        return [
            str(Path(local) / "Programs" / "WorkBuddy" / "WorkBuddy.exe"),
            str(Path(program_files) / "WorkBuddy" / "WorkBuddy.exe"),
        ]
    return [
        "/opt/workbuddy/workbuddy",
        str(home / ".local" / "share" / "workbuddy" / "workbuddy"),
        str(home / ".local" / "share" / "workbuddy" / "resources" / "workbuddy"),
    ]


def find_workbuddy_binary() -> str:
    explicit = os.environ.get("WB_BIN", "").strip()
    if explicit:
        if not Path(explicit).exists():
            fail(f"WB_BIN 不存在: {explicit}")
        return explicit

    from_path = command_path("workbuddy")
    if from_path:
        return from_path

    for candidate in workbuddy_binary_candidates():
        if Path(candidate).exists():
            return candidate

    patterns = [
        "/var/lib/apm/**/com.xydw.workbuddy/**/opt/workbuddy/workbuddy",
        str(Path.home() / "**" / "WorkBuddy.app/Contents/MacOS/WorkBuddy"),
    ]
    for pattern in patterns:
        for match in glob.glob(pattern, recursive=True):
            if Path(match).is_file():
                return match

    fail("找不到 WorkBuddy 可执行文件，请设置 WB_BIN 指向 WorkBuddy 二进制")
    return ""


def find_asar(binary: str) -> str:
    explicit = os.environ.get("WB_ASAR", "").strip()
    if explicit:
        if not Path(explicit).exists():
            fail(f"WB_ASAR 不存在: {explicit}")
        return explicit

    binary_path = Path(binary).resolve()
    candidates = [
        binary_path.parent / "resources" / "app.asar",
        binary_path.parent.parent / "Resources" / "app.asar",
        binary_path.parent.parent.parent / "Resources" / "app.asar",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    fail("找不到 WorkBuddy 的 app.asar，请设置 WB_ASAR")
    return ""


def run_decrypt(binary: str, asar: str) -> dict:
    env = os.environ.copy()
    env["ELECTRON_RUN_AS_NODE"] = "1"
    env["WB_ASAR"] = asar
    try:
        completed = subprocess.run(
            [binary, str(DECRYPT_JS), "--json"],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
    except subprocess.TimeoutExpired:
        fail("读取登录态超时（WorkBuddy 可能正在启动，请重试）")

    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or not output:
        detail = (completed.stderr or completed.stdout or "").strip()
        fail(f"解密登录态失败: {detail[:800]}")

    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("accessToken"):
            return payload

    fail("解密脚本没有返回 accessToken")


def mask(value: str, keep: int = 4) -> str:
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def build_env_file(payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"WB_ACCESS_TOKEN={shlex.quote(payload['accessToken'])}",
        f"WB_USER_ID={shlex.quote(payload.get('uid', ''))}",
    ]
    for key, env_key in (("domain", "WB_DOMAIN"), ("enterpriseId", "WB_ENTERPRISE_ID")):
        value = payload.get(key) or ""
        if value:
            lines.append(f"{env_key}={shlex.quote(value)}")
    SECRETS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    SECRETS_FILE.chmod(0o600)


def gh_set_secret(gh: str, repo: str, name: str, value: str) -> bool:
    if not value:
        return True
    completed = subprocess.run(
        [gh, "secret", "set", name, "--repo", repo],
        input=value,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        print(f"  {name}: 失败 {completed.stderr.strip()[:200]}", file=sys.stderr)
        return False
    print(f"  {name}: 已写入 {repo}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("GH_REPO", ""), help="owner/name")
    parser.add_argument("--check", action="store_true", help="只验证解密，不写 secret")
    parser.add_argument("--print-path", action="store_true", help="只打印 secrets.env 路径")
    args = parser.parse_args()

    binary = find_workbuddy_binary()
    asar = find_asar(binary)
    print(f"WorkBuddy: {binary}")
    print(f"app.asar : {asar}")

    payload = run_decrypt(binary, asar)
    token = payload["accessToken"]
    uid = payload.get("uid", "")
    print(f"登录态读取成功: uid={mask(uid)} token={mask(token)}")

    if args.check:
        return 0

    build_env_file(payload)
    print(f"本地 secrets 文件: {SECRETS_FILE} (0600)")

    if args.print_path:
        print(SECRETS_FILE)
        return 0

    gh = command_path("gh")
    if not gh:
        print("", file=sys.stderr)
        print("未找到 gh，无法自动写入仓库 secret。请任选一种：", file=sys.stderr)
        print("  1. 安装 GitHub CLI 后重新运行: python3 scripts/refresh_token.py --repo owner/name", file=sys.stderr)
        print(f"  2. 打开仓库 Settings → Secrets and variables → Actions，按 {SECRETS_FILE} 中的值创建：", file=sys.stderr)
        print("     WB_ACCESS_TOKEN / WB_USER_ID / WB_DOMAIN / WB_ENTERPRISE_ID（后两项有值才需要）", file=sys.stderr)
        return 0

    if not args.repo:
        print("已找到 gh，但未指定 --repo（或 GH_REPO），跳过自动写入", file=sys.stderr)
        print(f"可执行: {gh} secret set WB_ACCESS_TOKEN --repo owner/name < <(grep ...)", file=sys.stderr)
        return 0

    print(f"写入 GitHub Actions secrets → {args.repo}")
    all_ok = True
    all_ok &= gh_set_secret(gh, args.repo, "WB_ACCESS_TOKEN", token)
    all_ok &= gh_set_secret(gh, args.repo, "WB_USER_ID", uid)
    all_ok &= gh_set_secret(gh, args.repo, "WB_DOMAIN", payload.get("domain", ""))
    all_ok &= gh_set_secret(gh, args.repo, "WB_ENTERPRISE_ID", payload.get("enterpriseId", ""))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
