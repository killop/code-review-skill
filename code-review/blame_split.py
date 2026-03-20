#!/usr/bin/env python3
"""
code-review blame & split tool
从 FULL_REPORT.md 中解析问题，通过 git blame 追溯作者，
生成带作者标注的报告 + 按作者拆分的独立文件。

用法：
  python blame_split.py --root <仓库根目录> --report <FULL_REPORT.md路径> --config <config.json路径>

输出（写入 report 同级目录）：
  - FULL_REPORT.md      原地更新，每个问题标题追加【作者: xxx】+ 末尾统计表
  - SUMMARY.md          末尾追加作者统计表
  - by_author/
    ├── bugs_<author>.md
    ├── bugs_未归属.md
    ├── INDEX.md
    └── VERIFY.md
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ENC = dict(text=True, encoding="utf-8", errors="ignore")


# ── 解析 ──────────────────────────────────────────────

def parse_issues(md_path: Path):
    """从 FULL_REPORT.md 解析所有问题块"""
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    issues = []
    module = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        # 模块标题
        if line.startswith("## 模块") or line.startswith("## Module"):
            module = re.sub(r"^##\s*模块[：:]\s*", "", line).strip()
            module = re.sub(r"^##\s*Module[：:]\s*", "", module).strip()
        # 问题标题
        if re.match(r"^###\s*问题\s*\d+", line):
            start = i
            title_line_idx = i
            i += 1
            while i < len(lines):
                if re.match(r"^###\s*问题\s*\d+", lines[i]):
                    break
                if re.match(r"^##\s", lines[i]):
                    break
                i += 1
            block_lines = lines[start:i]
            block = "\n".join(block_lines).rstrip() + "\n"

            file_field = ""
            line_field = ""
            for bl in block_lines:
                cleaned = bl.replace("**", "").strip()
                # 匹配多种格式: "- 文件：", "- 文件:", "文件：", "文件路径："
                fm = re.match(r"^-?\s*文件(?:路径)?[：:]\s*(.+)", cleaned)
                if fm and not file_field:
                    file_field = fm.group(1).strip().strip("`")
                lm = re.match(r"^-?\s*行号[：:]\s*(.+)", cleaned)
                if lm and not line_field:
                    line_field = lm.group(1).strip().strip("`")

            issues.append({
                "module": module,
                "block": block,
                "title_line_idx": title_line_idx,
                "file_field": file_field,
                "line_field": line_field,
            })
        else:
            i += 1
    return issues, lines


# ── git 索引 ──────────────────────────────────────────

def build_tracked_index(root: Path):
    """构建仓库文件索引，支持多种路径匹配策略"""
    r = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, timeout=30, **ENC
    )
    tracked = [p.strip() for p in r.stdout.splitlines() if p.strip()]
    tracked_set = set(tracked)
    tracked_lower = {p.lower(): p for p in tracked}

    # 后缀索引：从文件名到完整路径的多级匹配
    suffix_index = defaultdict(list)
    for p in tracked:
        parts = p.lower().replace("\\", "/").split("/")
        for k in range(1, min(len(parts), 8) + 1):
            key = "/".join(parts[-k:])
            suffix_index[key].append(p)

    return tracked, tracked_set, tracked_lower, suffix_index


def resolve_file(file_field, root, tracked_set, tracked_lower, suffix_index, tracked):
    """智能解析文件路径，支持完整路径/前缀补全/后缀匹配/basename兜底"""
    candidates = re.findall(r"[A-Za-z0-9_./\\-]+\.\w+", file_field)
    if not candidates:
        return None

    for cand in candidates:
        c = cand.replace("\\", "/").strip("./")
        cl = c.lower()

        # 1. 精确匹配
        if c in tracked_set:
            return c
        if cl in tracked_lower:
            return tracked_lower[cl]

        # 2. 磁盘存在性检查
        direct = root / c
        if direct.exists():
            rp = str(direct.relative_to(root)).replace("\\", "/")
            return tracked_lower.get(rp.lower(), rp)

        # 3. 常见前缀补全
        prefixes = [
            "Assets/Scripts/HotFix/Runtime/",
            "Assets/Scripts/HotFix/Runtime/Lua2CSharpCode/",
            "Assets/Scripts/HotFix/Runtime/Lua2CSharpCode/GameModule/",
            "Assets/Scripts/HotFix/Runtime/Lua2CSharpCode/GameNet/",
            "Assets/Scripts/HotFix/Runtime/Lua2CSharpCode/Common/",
            "Assets/Scripts/HotFix/Runtime/Lua2CSharpCode/GameData/",
            "Assets/Scripts/HotFix/Runtime/Lua2CSharpCode/GameView/",
        ]
        for pre in prefixes:
            full = (pre + c).replace("\\", "/")
            if full.lower() in tracked_lower:
                return tracked_lower[full.lower()]

        # 4. 后缀索引匹配
        matches = suffix_index.get(cl, [])
        if len(matches) == 1:
            return matches[0]

        # 5. basename 兜底
        base = cl.split("/")[-1]
        base_matches = [
            p for p in tracked
            if p.lower().endswith("/" + base) or p.lower() == base
        ]
        if len(base_matches) == 1:
            return base_matches[0]

    return None


# ── blame ─────────────────────────────────────────────

def parse_start_line(line_field):
    """从行号字段提取起始行号，支持范围/中文前缀等格式"""
    m = re.search(r"\d+", line_field)
    return int(m.group(0)) if m else None


def blame_author(root, path, line_no, timeout=10):
    """git blame 单行，返回作者名"""
    try:
        r = subprocess.run(
            ["git", "blame", "-L", f"{line_no},{line_no}",
             "--line-porcelain", "--", path],
            cwd=root, capture_output=True, timeout=timeout, **ENC,
        )
        if r.returncode == 0:
            for ln in r.stdout.splitlines():
                if ln.startswith("author "):
                    return ln[7:].strip()
    except subprocess.TimeoutExpired:
        pass
    return None


def fallback_author(root, path, timeout=10):
    """blame 失败时，用 git log 获取最后修改者"""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%an", "--", path],
            cwd=root, capture_output=True, timeout=timeout, **ENC,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except subprocess.TimeoutExpired:
        pass
    return None


def normalize_author(name, alias_map):
    """作者名归一化"""
    return alias_map.get(name, name)


# ── 主流程 ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Code review blame & split")
    parser.add_argument("--root", required=True, help="仓库根目录")
    parser.add_argument("--report", required=True, help="FULL_REPORT.md 路径")
    parser.add_argument("--config", default="", help="config.json 路径（可选）")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    report_path = Path(args.report).resolve()
    out_dir = report_path.parent
    summary_path = out_dir / "SUMMARY.md"
    by_author_dir = out_dir / "by_author"
    by_author_dir.mkdir(parents=True, exist_ok=True)

    # 加载作者别名
    alias_map = {}
    if args.config and Path(args.config).exists():
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        alias_map = cfg.get("output", {}).get("author_alias", {})

    print(f"[1/5] 解析问题...")
    issues, original_lines = parse_issues(report_path)
    print(f"      找到 {len(issues)} 个问题")

    if not issues:
        print("没有找到问题，退出。")
        return

    print(f"[2/5] 构建文件索引...")
    tracked, tracked_set, tracked_lower, suffix_index = build_tracked_index(root)
    print(f"      索引了 {len(tracked)} 个文件")

    print(f"[3/5] 逐问题 git blame...")
    assignments = []
    author_counter = Counter()
    for idx, it in enumerate(issues, 1):
        start_line = parse_start_line(it["line_field"]) if it["line_field"] else None
        author = None
        resolved = None
        reason = None

        if not it["file_field"]:
            reason = "文件字段为空"
        elif start_line is None:
            # 没有行号，直接用 git log fallback
            resolved = resolve_file(
                it["file_field"], root, tracked_set, tracked_lower, suffix_index, tracked
            )
            if resolved:
                author = fallback_author(root, resolved)
            if not author:
                reason = "行号缺失且fallback失败"
        else:
            resolved = resolve_file(
                it["file_field"], root, tracked_set, tracked_lower, suffix_index, tracked
            )
            if not resolved:
                reason = "文件路径无法解析"
            else:
                author = blame_author(root, resolved, start_line)
                if not author:
                    author = fallback_author(root, resolved)
                if not author:
                    reason = "blame和fallback均失败"

        if author:
            author = normalize_author(author, alias_map)

        display = author if author else "未归属"
        author_counter[display] += 1

        assignments.append({
            "module": it["module"],
            "block": it["block"],
            "title_line_idx": it["title_line_idx"],
            "author": display,
            "resolved_file": resolved,
            "reason": reason,
        })

        if idx % 10 == 0 or idx == len(issues):
            print(f"      {idx}/{len(issues)} done")

    # ── 更新 FULL_REPORT.md：标题追加作者 ──
    print(f"[4/5] 更新报告...")
    # 按行号倒序修改，避免偏移
    for a in sorted(assignments, key=lambda x: x["title_line_idx"], reverse=True):
        li = a["title_line_idx"]
        old_title = original_lines[li]
        # 去掉已有的作者标注（防止重复运行）
        old_title = re.sub(r"\s*【作者[:：]\s*[^】]+】", "", old_title)
        original_lines[li] = f"{old_title} 【作者: {a['author']}】"

    # 追加统计表
    stats_lines = [
        "",
        "---",
        "",
        "## 问题代码作者统计",
        "",
        "| 作者 | 问题数量 | 占比 |",
        "|------|---------|------|",
    ]
    total = sum(author_counter.values())
    for author, count in author_counter.most_common():
        pct = f"{count / total * 100:.1f}%"
        stats_lines.append(f"| {author} | {count} | {pct} |")
    stats_lines.append(f"| **合计** | **{total}** | **100.0%** |")

    # 移除旧的统计表（如果存在）
    text_joined = "\n".join(original_lines)
    text_joined = re.sub(
        r"\n---\s*\n+## 问题代码作者统计.*",
        "",
        text_joined,
        flags=re.DOTALL,
    )
    final_report = text_joined.rstrip() + "\n" + "\n".join(stats_lines) + "\n"
    report_path.write_text(final_report, encoding="utf-8", newline="\n")
    print(f"      FULL_REPORT.md 已更新")

    # 更新 SUMMARY.md
    if summary_path.exists():
        summary_text = summary_path.read_text(encoding="utf-8")
        summary_text = re.sub(
            r"\n---\s*\n+## 问题代码作者统计.*",
            "",
            summary_text,
            flags=re.DOTALL,
        )
        summary_text = summary_text.rstrip() + "\n" + "\n".join(stats_lines) + "\n"
        summary_path.write_text(summary_text, encoding="utf-8", newline="\n")
        print(f"      SUMMARY.md 已更新")

    # ── 按作者拆分文件 ──
    print(f"[5/5] 按作者拆分...")
    by_author = defaultdict(list)
    for a in assignments:
        by_author[a["author"]].append(a)

    # 清理旧文件
    for p in by_author_dir.glob("*.md"):
        p.unlink()

    index_lines = ["# Bug 按作者归档", ""]
    for author in sorted(by_author.keys(), key=lambda x: (x == "未归属", x.lower())):
        safe_name = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", author).strip("_") or "unknown"
        fn = f"bugs_{safe_name}.md" if author != "未归属" else "bugs_未归属.md"
        fp = by_author_dir / fn
        items = by_author[author]

        with fp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(f"# 归属人：{author}\n\n")
            f.write(f"- 问题数：{len(items)}\n\n")
            last_mod = None
            for it in items:
                if it["module"] != last_mod:
                    f.write(f"## 模块：{it['module']}\n\n")
                    last_mod = it["module"]
                if author == "未归属" and it["reason"]:
                    f.write(f"> 归属失败原因：{it['reason']}\n\n")
                elif it["resolved_file"]:
                    f.write(f"> Blame 文件：{it['resolved_file']}\n\n")
                f.write(it["block"] + "\n")

        index_lines.append(f"- [{author}]({fn}) ({len(items)})")

    (by_author_dir / "INDEX.md").write_text(
        "\n".join(index_lines) + "\n", encoding="utf-8", newline="\n"
    )

    # 一致性校验
    src_hashes = Counter(
        hashlib.sha256(it["block"].encode("utf-8")).hexdigest() for it in issues
    )
    dist_hashes = Counter(
        hashlib.sha256(a["block"].encode("utf-8")).hexdigest() for a in assignments
    )
    consistent = src_hashes == dist_hashes

    with (by_author_dir / "VERIFY.md").open("w", encoding="utf-8", newline="\n") as f:
        f.write("# 校验结果\n\n")
        f.write(f"- 原始问题数：{len(issues)}\n")
        f.write(f"- 分发后问题数：{sum(len(v) for v in by_author.values())}\n")
        f.write(f"- 条目数量一致：{'是' if len(issues) == sum(len(v) for v in by_author.values()) else '否'}\n")
        f.write(f"- 条目内容哈希一致：{'是' if consistent else '否'}\n")
        f.write(f"- 未归属数量：{len(by_author.get('未归属', []))}\n")

    unattr = len(by_author.get("未归属", []))
    print(f"\n完成！{len(issues)} 个问题，{len(by_author)} 位作者，{unattr} 个未归属")


if __name__ == "__main__":
    main()
