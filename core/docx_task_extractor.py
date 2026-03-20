import argparse
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _get_text(el):
    parts = []
    for t in el.findall(".//w:t", NS):
        if t.text:
            parts.append(t.text)
    text = "".join(parts)
    return re.sub(r"\s+", " ", text).strip()


def _read_tables(docx_path):
    with zipfile.ZipFile(docx_path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)

    tables = []
    for tbl in root.findall(".//w:tbl", NS):
        rows = []
        for tr in tbl.findall(".//w:tr", NS):
            cells = []
            for tc in tr.findall("./w:tc", NS):
                cells.append(_get_text(tc))
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _normalize_label(label):
    return (
        label.replace("\uff1a", "")
        .replace(":", "")
        .replace(" ", "")
        .strip()
    )


def _coerce_value(val):
    if val is None:
        return None
    text = str(val).strip()
    if text == "":
        return None
    if NUM_RE.match(text):
        return int(text) if "." not in text else float(text)
    return text


def _pad_row(row, size):
    if len(row) >= size:
        return row[:size]
    return row + [""] * (size - len(row))


def _row_text(row):
    return " ".join(c for c in row if c)


def _find_table_by_title(tables, title_keyword):
    for rows in tables:
        if rows and len(rows[0]) == 1 and title_keyword in rows[0][0]:
            return rows
    return None


def _find_table_by_header(tables, required_keywords):
    for rows in tables:
        for idx, row in enumerate(rows):
            text = _row_text(row)
            if all(k in text for k in required_keywords):
                return rows, idx
    return None, None


def _extract_unit(rows):
    for row in rows:
        for cell in row:
            if "金额单位" in cell:
                if "：" in cell:
                    return cell.split("：", 1)[1].strip()
                if ":" in cell:
                    return cell.split(":", 1)[1].strip()
        for i, cell in enumerate(row):
            if "金额单位" in cell and i + 1 < len(row):
                return row[i + 1].strip()
    return None


def _is_empty_row(row):
    return all(c.strip() == "" for c in row)


def _is_summary_row(row):
    if not row:
        return False
    label = row[0]
    return any(k in label for k in ("合计", "累计", "总计"))


def _extract_kv_table(rows):
    data = {}
    for row in rows:
        if len(row) >= 2 and row[0]:
            data[_normalize_label(row[0])] = row[1].strip()
    return data


def _extract_topic_info(rows):
    info = {"title": rows[0][0] if rows and rows[0] else "", "fields": {}, "sections": {}, "raw_rows": rows}
    current_section = None
    for row in rows[1:]:
        if len(row) == 1 and row[0]:
            current_section = _normalize_label(row[0])
            info["sections"].setdefault(current_section, {"fields": {}, "rows": [], "header": []})
            continue

        if len(row) == 2:
            info["fields"][_normalize_label(row[0])] = row[1].strip()
            current_section = None
            continue

        if len(row) == 4:
            info["fields"][_normalize_label(row[0])] = row[1].strip()
            info["fields"][_normalize_label(row[2])] = row[3].strip()
            current_section = None
            continue

        if len(row) >= 3:
            label = _normalize_label(row[0]) if row[0] else ""
            if label:
                current_section = label
                section = info["sections"].setdefault(current_section, {"fields": {}, "rows": [], "header": []})
                if row[1] and row[2] and _normalize_label(row[1]) in ("序号", "单位名称"):
                    section["header"] = [_normalize_label(row[1]), _normalize_label(row[2])]
                else:
                    section["fields"][_normalize_label(row[1])] = row[2].strip()
            elif current_section:
                section = info["sections"][current_section]
                if section.get("header"):
                    data_row = row[1:] if len(row) > 1 else []
                    if any(c.strip() for c in data_row):
                        section["rows"].append(data_row)
                elif len(row) >= 3 and row[1]:
                    section["fields"][_normalize_label(row[1])] = row[2].strip()
    return info


def _extract_simple_table(rows, header_idx):
    header = rows[header_idx]
    data_rows = []
    for row in rows[header_idx + 1 :]:
        if _is_empty_row(row):
            continue
        row = _pad_row(row, len(header))
        data_rows.append({header[i]: _coerce_value(row[i]) for i in range(len(header))})
    return {"header": header, "rows": data_rows}


def _extract_detail_table(rows, header_idx):
    header = rows[header_idx]
    unit = _extract_unit(rows[:header_idx])
    notes = []
    details = []
    summary = []

    for row in rows[:header_idx]:
        if any("填表说明" in c for c in row):
            notes.append(_row_text(row))

    for row in rows[header_idx + 1 :]:
        if _is_empty_row(row):
            continue
        if _is_summary_row(row):
            summary.append({"label": row[0], "values": [_coerce_value(v) for v in row[1:]]})
            continue
        row = _pad_row(row, len(header))
        if all(c.strip() == "" for c in row[1:]):
            continue
        details.append({header[i]: _coerce_value(row[i]) for i in range(len(header))})

    return {"unit": unit, "notes": notes, "header": header, "rows": details, "summary": summary}


def _is_basic_info_table(rows):
    if not rows or not all(len(r) == 2 for r in rows):
        return False
    labels = [_normalize_label(r[0]) for r in rows if r and r[0]]
    if any("\u8bfe\u9898\u7f16\u53f7" in label for label in labels):
        return True
    has_name = any("\u8bfe\u9898\u540d\u79f0" in label for label in labels)
    if not has_name:
        return False
    has_unit = any("\u8bfe\u9898\u627f\u62c5\u5355\u4f4d" in label for label in labels)
    has_unit = has_unit or any("\u8bfe\u9898\u7275\u5934\u627f\u62c5\u5355\u4f4d" in label for label in labels)
    has_lead = any("\u8bfe\u9898\u8d1f\u8d23\u4eba" in label for label in labels)
    has_contact = any("\u8bfe\u9898\u8054\u7cfb\u4eba" in label for label in labels)
    return has_unit or has_lead or has_contact


def extract_task_docx(docx_path):
    tables = _read_tables(docx_path)

    data = {
        "source_file": str(Path(docx_path)),
        "basic_info": {},
        "topic_info": {},
        "assessment_metrics": {},
        "schedule": {},
        "budget_summary": {},
        "equipment_budget_detail": {},
        "material_budget_detail": {},
        "test_processing_detail": {},
        "unit_budget_detail": {},
        "personnel": {},
        "signatures": {},
    }

    basic_rows = None
    for rows in tables:
        if _is_basic_info_table(rows):
            basic_rows = rows
            break
    if basic_rows:
        data["basic_info"] = _extract_kv_table(basic_rows)

    topic_rows = _find_table_by_title(tables, "课题信息表")
    if topic_rows:
        data["topic_info"] = _extract_topic_info(topic_rows)

    assess_rows, assess_header_idx = _find_table_by_header(tables, ["考核指标名称", "数量", "说明"])
    if assess_rows:
        data["assessment_metrics"] = _extract_simple_table(assess_rows, assess_header_idx)

    schedule_rows, schedule_header_idx = _find_table_by_header(tables, ["起止日期", "研究目标", "研究内容", "预期成果"])
    if schedule_rows:
        data["schedule"] = _extract_simple_table(schedule_rows, schedule_header_idx)

    budget_rows, budget_header_idx = _find_table_by_header(tables, ["预算科目名称", "合计"])
    if budget_rows:
        data["budget_summary"] = _extract_simple_table(budget_rows, budget_header_idx)

    equipment_rows, equipment_header_idx = _find_table_by_header(tables, ["设备名称", "设备分类", "金额"])
    if equipment_rows:
        data["equipment_budget_detail"] = _extract_detail_table(equipment_rows, equipment_header_idx)

    material_rows, material_header_idx = _find_table_by_header(tables, ["材料名称", "计量单位", "金额"])
    if material_rows:
        data["material_budget_detail"] = _extract_detail_table(material_rows, material_header_idx)

    test_rows, test_header_idx = _find_table_by_header(tables, ["测试化验加工的内容", "数量", "金额"])
    if test_rows:
        data["test_processing_detail"] = _extract_detail_table(test_rows, test_header_idx)

    unit_rows, unit_header_idx = _find_table_by_header(tables, ["承担单位性质", "专项经费", "自筹经费"])
    if unit_rows:
        data["unit_budget_detail"] = _extract_detail_table(unit_rows, unit_header_idx)

    personnel_rows, personnel_header_idx = _find_table_by_header(tables, ["姓名", "年龄", "学历"])
    if personnel_rows:
        unit = _extract_unit(personnel_rows[:personnel_header_idx])
        header = personnel_rows[personnel_header_idx]
        section = None
        for row in personnel_rows[:personnel_header_idx]:
            if len(row) == 1 and row[0] and "填表说明" not in row[0]:
                section = row[0]
        rows = []
        for row in personnel_rows[personnel_header_idx + 1 :]:
            if _is_empty_row(row):
                continue
            row = _pad_row(row, len(header))
            row_data = {header[i]: _coerce_value(row[i]) for i in range(len(header))}
            if section:
                row_data["人员类别"] = section
            rows.append(row_data)
        data["personnel"] = {"unit": unit, "header": header, "rows": rows}

    signature_rows = None
    for rows in tables:
        if any("课题组织单位" in c for r in rows for c in r):
            signature_rows = rows
            break
    if signature_rows:
        sig = []
        for row in signature_rows:
            if len(row) >= 2 and row[0]:
                sig.append({"label": row[0], "value": row[1], "note": row[2] if len(row) > 2 else ""})
        data["signatures"] = {"rows": sig}

    return data


def main():
    parser = argparse.ArgumentParser(description="Extract task information from a DOCX file to JSON.")
    parser.add_argument("docx_path", help="Path to DOCX file")
    parser.add_argument("-o", "--output", help="Path to output JSON file")
    args = parser.parse_args()

    result = extract_task_docx(args.docx_path)
    output_path = Path(args.output) if args.output else Path(args.docx_path).with_suffix(".json")
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
