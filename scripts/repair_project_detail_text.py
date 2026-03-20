from __future__ import annotations

import re
from pathlib import Path


TARGET = Path("core/templates/core/project_detail.html")

# 常见乱码字符集合（用于评分）
MARKER_CHARS = set("璇鏂椤绯鍒杩浠鎵缁鍏鎴娴璁鍙寮璐矗浜閰嶇疆")

# 常见业务字符集合（用于评分，命中越多越像正常文本）
COMMON_CHARS = set(
    "项目课题文件管理系统打开返回设置上传下载删除创建分析负责人编号列表保存刷新目录日期状态开始结束"
    "网络配置功能当前路径按钮编辑取消成功失败提示请先输入选择查看详情研究内容输出指标统计监控经费"
    "时间联系人归属级别类型角色预算计划实际完成根目录文件夹上传到刷新文件树"
)

# 二次修复：自动转码后仍会残留少量问号/错词，做定向替换
POST_REPLACEMENTS: list[tuple[str, str]] = [
    ("打开文件?", "打开文件夹"),
    ("打开项目文件?", "打开项目文件夹"),
    ("刷新文件?", "刷新文件树"),
    ("负责?", "负责人"),
    ("联系?", "联系人"),
    ("状?", "状态"),
    ("弢始日?", "开始日期"),
    ("总预?", "总预算"),
    ("院专?", "院专项"),
    ("归属与角?", "归属与角色"),
    ("创建文件?", "创建文件夹"),
    ("根目?", "根目录"),
    ("当前?>", "当前值\">"),
    ("输入文件夹名?", "输入文件夹名称"),
    ("创?", "创建"),
    ("任何文件?", "任何文件。"),
    ("提示?", "提示："),
    ("详细信?", "详细信息"),
    ("分析结果?", "分析结果。"),
    ("配置API瀵嗛挜", "配置API密钥"),
    (" 鍜?", " 和 "),
]


def score_text(segment: str) -> int:
    bad = sum(ch in MARKER_CHARS for ch in segment)
    question = segment.count("?") + segment.count("�")
    good = sum(ch in COMMON_CHARS for ch in segment)
    return bad * 10 + question * 5 - good * 3


def convert_segment(segment: str) -> str:
    candidates = [segment]
    try:
        candidates.append(segment.encode("gb18030").decode("utf-8"))
    except Exception:
        pass
    try:
        candidates.append(segment.encode("gb18030", "ignore").decode("utf-8", "ignore"))
    except Exception:
        pass

    # 去重并按评分排序
    uniq: list[str] = []
    for cand in candidates:
        if cand not in uniq:
            uniq.append(cand)
    uniq.sort(key=lambda s: (score_text(s), abs(len(s) - len(segment))))
    best = uniq[0]
    return best if score_text(best) < score_text(segment) else segment


def main() -> None:
    if not TARGET.exists():
        raise FileNotFoundError(f"File not found: {TARGET}")

    text = TARGET.read_text(encoding="utf-8-sig")
    non_ascii_pattern = re.compile(r"[^\x00-\x7F]{2,}")

    # 先做片段级自动修复
    segments = sorted(set(non_ascii_pattern.findall(text)), key=len, reverse=True)
    fixed = text
    for seg in segments:
        new_seg = convert_segment(seg)
        if new_seg != seg:
            fixed = fixed.replace(seg, new_seg)

    # 再做定向替换
    for old, new in POST_REPLACEMENTS:
        fixed = fixed.replace(old, new)

    TARGET.write_text(fixed, encoding="utf-8")

    before_bad = sum(ch in MARKER_CHARS for ch in text)
    after_bad = sum(ch in MARKER_CHARS for ch in fixed)
    print(f"Repaired: {TARGET}")
    print(f"Marker chars: {before_bad} -> {after_bad}")


if __name__ == "__main__":
    main()

