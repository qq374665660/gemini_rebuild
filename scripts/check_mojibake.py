from __future__ import annotations

import argparse
from pathlib import Path


DEFAULT_GLOBS = [
    "core/templates/**/*.html",
]

MOJIBAKE_TOKENS = [
    "鏈嶅姟",
    "鏀寔",
    "鍗曚綅",
    "鎿嶄綔",
    "弢始",
    "朢近",
    "鏉ユ簮",
    "鐢虫姤",
    "地坢",
    "打弢",
    "霢要",
    "涓殑",
    "閿欒",
    "鍝嶅簲",
    "馃",
    "鈭",
    "鉁",
    "鉂",
    "?/a>",
    "?/option>",
]

def iter_targets(root: Path, patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pat in patterns:
        files.extend(root.glob(pat))
    uniq = sorted({p.resolve() for p in files if p.is_file()})
    return [Path(p) for p in uniq]


def scan_file(path: Path) -> list[str]:
    findings: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        if "\ufffd" in line:
            findings.append(f"{path}:{lineno}: 包含替换字符 U+FFFD")
        if any(0xE000 <= ord(ch) <= 0xF8FF for ch in line):
            findings.append(f"{path}:{lineno}: 包含私有区字符(PUA)")
        for token in MOJIBAKE_TOKENS:
            if token in line:
                findings.append(f"{path}:{lineno}: 命中可疑片段: {token}")
                break
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="检查模板中的乱码/坏标签片段")
    parser.add_argument(
        "--glob",
        action="append",
        dest="globs",
        help="扫描路径通配符（可多次传入）",
    )
    args = parser.parse_args()

    root = Path(".").resolve()
    patterns = args.globs or DEFAULT_GLOBS
    targets = iter_targets(root, patterns)
    if not targets:
        print("未找到待扫描文件。")
        return 0

    all_findings: list[str] = []
    for fp in targets:
        all_findings.extend(scan_file(fp))

    if all_findings:
        print("发现疑似乱码或损坏片段：")
        for item in all_findings:
            print(item)
        return 1

    print(f"扫描完成，未发现异常。文件数: {len(targets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
