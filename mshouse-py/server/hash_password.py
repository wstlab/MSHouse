#!/usr/bin/env python3
"""
生成「初始化设置」口令的哈希，供 config.toml 的 security.setup_password_hash 使用。

用法：
    python -m server.hash_password                 # 交互式输入（不回显）
    python -m server.hash_password "我的口令"       # 直接传入（会留在 shell 历史里，不推荐）
    python -m server.hash_password --random        # 生成一个随机口令并打印哈希

为什么要有这个脚本：
口令哈希不能手算，也不该让使用者去碰哈希库。给一个明确入口，
并且默认走「不回显的交互输入」——避免口令落进 shell 历史和屏幕录像。
"""

from __future__ import annotations

import getpass
import sys

from . import security as sec


def main(argv: list[str]) -> int:
    if "--random" in argv:
        password = sec.generate_password()
        print("已生成随机口令：", password)
        print("哈希（填入 config.toml 的 security.setup_password_hash）：")
        print(sec.hash_password(password))
        print("\n请把口令另行保存；本脚本不会把它写入任何文件。")
        return 0

    # 位置参数里带口令（不推荐，但方便脚本化）
    args = [a for a in argv[1:] if not a.startswith("-")]
    if args:
        password = args[0]
        print("提示：口令出现在命令行里会留在 shell 历史中，建议改用交互输入。")
    else:
        password = getpass.getpass("请输入新的初始化口令：")
        again = getpass.getpass("请再输入一次：")
        if password != again:
            print("两次输入不一致，已取消。", file=sys.stderr)
            return 1
        if not password:
            print("口令不能为空，已取消。", file=sys.stderr)
            return 1

    print("\n哈希（填入 config.toml 的 security.setup_password_hash）：")
    print(sec.hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
