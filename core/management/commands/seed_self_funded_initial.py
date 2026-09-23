"""以《课题清单_归集结果_已回填.xlsx》建立企业全自筹课题台账，并把已归集的任务书复制到课题目录。

用法：
    python manage.py seed_self_funded_initial                      # 干跑，只预览
    python manage.py seed_self_funded_initial --yes                # 写库并建目录骨架
    python manage.py seed_self_funded_initial --yes --place-files  # 再复制任务书
    python manage.py seed_self_funded_initial --yes --refresh-expense  # 再刷新支出快照

不动专项经费课题：撞号那条只往现有专项课题的备注追加清单名称，其余专项档案一律不碰。
支出快照刷新有硬闸——专项侧逐课题金额必须与刷新前完全一致，否则整体回滚。
"""

import csv
import re
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.expense_analysis import (
    EXPENSE_FORMAT_VERSION,
    analyze_expense_workbook,
    normalized_expense_description,
    similarity_score,
)
from core.models import ExpenseImport, ExpenseMapping, ExpenseSnapshot, Project
from core.views import create_project_directory_structure

DEFAULT_SOURCE = Path(r'D:\03科信部\13各类迎检\归集课题整理')
SOURCE_SHEET = '课题清单_归集结果_已回填.xlsx'
TASK_CSV = '任务书来源核对表.csv'
TASK_FILE_DIR = '搜索任务书'
EXPENSE_TARGET_NAME = '支出监控_当前月.xlsx'

# 清单把 217 条全登记成"应用发展"，模型没有这一档；入库归到试验发展并留原值备查。
PROJECT_TYPE_FALLBACK = '试验发展'
MERGE_NOTE_MARK = '全自筹清单登记名称'


class Command(BaseCommand):
    help = '导入企业全自筹课题清单、复制归集任务书，并刷新支出快照（默认干跑）。'

    def add_arguments(self, parser):
        parser.add_argument('--source-dir', default=str(DEFAULT_SOURCE), help='清单与任务书所在目录。')
        parser.add_argument('--source-file', help='课题清单文件名或完整路径。')
        parser.add_argument('--place-files', action='store_true', help='把归集到的任务书复制进课题目录。')
        parser.add_argument('--refresh-expense', action='store_true', help='按新课题池刷新支出快照当前批次。')
        parser.add_argument('--no-backup', action='store_true', help='跳过写库前的数据库备份。')
        parser.add_argument('--yes', action='store_true', help='确认写库。')

    def handle(self, *args, **options):
        source_dir = Path(options['source_dir'])
        source_file = self._resolve(source_dir, options['source_file'], SOURCE_SHEET)
        rows = self.read_source(source_file)
        existing = {project.project_id: project for project in Project.objects.all()}

        plan, report = self.build_plan(rows, existing)
        self.print_report(plan, report)

        if not options['yes']:
            self.stdout.write(self.style.WARNING('未加 --yes，仅预览，不做任何修改。'))
            return

        if not options['no_backup']:
            self.backup_database()

        with transaction.atomic():
            created, updated, merged = self.apply_plan(plan)
        self.stdout.write(self.style.SUCCESS(f'课题写入完成：新建 {created}，更新 {updated}，并备注 {merged}'))

        if options['place_files']:
            self.place_files(source_dir)

        if options['refresh_expense']:
            self.refresh_expense_snapshots()

    def _resolve(self, source_dir, explicit, needle):
        if explicit:
            path = Path(explicit)
            if not path.is_absolute():
                path = source_dir / explicit
        else:
            path = source_dir / needle
        if not path.exists():
            raise CommandError(f'文件不存在：{path}')
        return path

    def read_source(self, path):
        worksheet = openpyxl.load_workbook(path, data_only=True)['课题清单']
        headers = [str(cell.value).strip() if cell.value is not None else '' for cell in worksheet[1]]
        index = {header: position for position, header in enumerate(headers)}
        required = ('课题编号', '课题名称', '经费管理类别')
        missing = [header for header in required if header not in index]
        if missing:
            raise CommandError(f'清单缺少列：{"、".join(missing)}')

        rows = []
        for position, values in enumerate(worksheet.iter_rows(min_row=2, values_only=True), start=2):
            def text(field):
                if field not in index:
                    return ''
                value = values[index[field]]
                if value is None:
                    return ''
                if isinstance(value, (int, float)):
                    return str(value).strip()
                return str(value).strip()

            def number(field):
                raw = text(field)
                if not raw:
                    return None
                try:
                    return float(raw.replace(',', '').replace('，', ''))
                except ValueError:
                    return None

            def date_value(field):
                if field not in index:
                    return None
                value = values[index[field]]
                if isinstance(value, datetime):
                    return value.date()
                return None

            project_id = text('课题编号')
            name = text('课题名称')
            if not project_id and not name:
                continue
            rows.append({
                'row': position,
                'project_id': project_id,
                'name': name,
                'managing_unit': text('归口单位'),
                'level': text('课题级别'),
                'project_type': text('课题类型'),
                'role': text('参与角色'),
                'start_year': int(number('开始年份')) if number('开始年份') else None,
                'status': text('课题状态'),
                'contact_person': text('课题联系人'),
                'project_lead': text('课题负责人'),
                'start_date': date_value('开始日期'),
                'planned_end_date': date_value('计划结束日期'),
                'extension_date': date_value('延期时间'),
                'actual_completion_date': date_value('实际结题时间'),
                'total_budget': number('总预算(万元)'),
                'external_funding': number('外部专项经费(万元)'),
                'institute_funding': number('院自筹经费(万元)'),
                'unit_funding': number('所属单位自筹经费(万元)'),
                'funding_category': text('经费管理类别'),
                'research_content': text('主要研究内容'),
                'remarks': text('备注'),
            })
        if not rows:
            raise CommandError(f'清单没有数据行：{path}')
        return rows

    def build_plan(self, rows, existing):
        """把清单行分成：新建自筹课题 / 更新已有自筹课题 / 与专项课题并备注 / 编号重复派生。"""
        plan = {'create': [], 'update': [], 'merge': [], 'derived': []}
        report = {'source_rows': len(rows), 'blank_year': 0, 'blank_status': 0, 'blank_lead': 0,
                  'blank_unit': 0, 'blank_funding': 0, 'type_remapped': 0, 'ownership_guess': {}}
        claimed = set()
        # 清单内课题名称不重复，用它当重复编号的落位键，重跑时能认领上次派生出的编号。
        by_name = {project.name: project for project in existing.values()
                   if project.funding_category == 'self_funded'}

        for row in rows:
            record = self.to_record(row)
            if not row['start_year']:
                report['blank_year'] += 1
            if not row['status']:
                report['blank_status'] += 1
            if not record['project_lead']:
                report['blank_lead'] += 1
            if not record['managing_unit']:
                report['blank_unit'] += 1
            if record['unit_funding'] is None:
                report['blank_funding'] += 1
            if row['project_type'] and row['project_type'] != record['project_type']:
                report['type_remapped'] += 1
            report['ownership_guess'][record['ownership']] = report['ownership_guess'].get(record['ownership'], 0) + 1

            project_id = record['project_id']
            if not project_id:
                raise CommandError(f"第 {row['row']} 行缺课题编号：{row['name']}")

            hit = existing.get(project_id)
            if hit is not None and hit.funding_category == 'special':
                # 撞号：专项档案优先，清单名称只并进备注，绝不改名也不改类别。
                plan['merge'].append({'project': hit, 'source_name': record['name'], 'row': row['row']})
                continue
            if project_id in claimed:
                renamed = by_name.get(record['name'])
                if renamed is not None and renamed.project_id != project_id:
                    derived = renamed.project_id  # 上轮已派生过，认领回来，重跑不另起编号
                else:
                    derived = f'{project_id}-2'
                    if derived in existing or derived in claimed:
                        raise CommandError(
                            f"第 {row['row']} 行编号 {project_id} 重复，且派生编号 {derived} 已被占用，请人工改名。"
                        )
                record['project_id'] = derived
                record['remarks'] = self.append_note(
                    record['remarks'],
                    f'【编号重复待确认】清单第 {row["row"]} 行与另一课题同为 {project_id}，'
                    f'本条暂用派生编号 {derived}，请核实真实课题编号。',
                )
                plan['derived'].append(record)
                plan['create'].append(record)
                claimed.add(derived)
                continue
            claimed.add(project_id)
            if hit is not None:
                plan['update'].append(record)
            else:
                plan['create'].append(record)
        return plan, report

    def to_record(self, row):
        managing_unit = row['managing_unit']
        ownership = '地下空间' if '地下空间' in managing_unit else '西勘院'
        remarks = row['remarks']
        if row['project_type'] and row['project_type'] not in dict(Project.TYPE_CHOICES):
            remarks = self.append_note(
                remarks,
                f'原登记课题类型：{row["project_type"]}（系统无此档，暂按 {PROJECT_TYPE_FALLBACK} 入库）',
            )
            project_type = PROJECT_TYPE_FALLBACK
        else:
            project_type = row['project_type'] or PROJECT_TYPE_FALLBACK
        return {
            'row': row['row'],
            'project_id': row['project_id'],
            'name': row['name'] or f"待补全-{row['project_id']}",
            'ownership': ownership,
            'funding_category': 'self_funded',
            'managing_unit': managing_unit,
            'level': row['level'] if row['level'] in dict(Project.LEVEL_CHOICES) else '公司级',
            'project_type': project_type,
            'role': row['role'] if row['role'] in dict(Project.ROLE_CHOICES) else '牵头',
            'start_year': row['start_year'] if row['start_year'] else 0,
            'status': Project.normalize_status(row['status']) if row['status'] else '',
            'contact_person': row['contact_person'],
            'project_lead': row['project_lead'],
            'start_date': row['start_date'],
            'planned_end_date': row['planned_end_date'],
            'extension_date': row['extension_date'],
            'actual_completion_date': row['actual_completion_date'],
            'total_budget': row['total_budget'],
            'external_funding': row['external_funding'],
            'institute_funding': row['institute_funding'],
            'unit_funding': row['unit_funding'],
            'research_content': row['research_content'],
            'remarks': remarks,
            'directory_path': '',
        }

    @staticmethod
    def append_note(remarks, note):
        remarks = (remarks or '').strip()
        if note in remarks:
            return remarks
        return f'{remarks}\n{note}'.strip()

    def print_report(self, plan, report):
        self.stdout.write(f'清单数据行：{report["source_rows"]}')
        self.stdout.write(
            '缺失统计：'
            f'年份 {report["blank_year"]}、状态 {report["blank_status"]}、负责人 {report["blank_lead"]}、'
            f'归口单位 {report["blank_unit"]}、自筹经费 {report["blank_funding"]}'
        )
        self.stdout.write(f'课题类型改档：{report["type_remapped"]} 条 -> {PROJECT_TYPE_FALLBACK}')
        self.stdout.write(f'课题归属推断：{report["ownership_guess"]}')
        self.stdout.write(
            f'计划：新建 {len(plan["create"])}、更新 {len(plan["update"])}、'
            f'撞号并备注 {len(plan["merge"])}、编号重复派生 {len(plan["derived"])}'
        )
        for item in plan['merge']:
            self.stdout.write(
                self.style.WARNING(
                    f'  撞号(清单第 {item["row"]} 行) {item["project"].project_id}：'
                    f'保留专项档案「{item["project"].name}」，仅追加清单名称「{item["source_name"]}」'
                )
            )
        for record in plan['derived']:
            self.stdout.write(self.style.WARNING(f'  编号重复 -> {record["project_id"]}｜{record["name"][:30]}'))

    def apply_plan(self, plan):
        created = updated = merged = 0
        for record in plan['create'] + plan['update']:
            fields = {key: value for key, value in record.items() if key != 'row'}
            project, was_created = Project.objects.update_or_create(
                project_id=record['project_id'],
                defaults=fields,
            )
            create_project_directory_structure(project)
            if was_created:
                created += 1
            else:
                updated += 1
        for item in plan['merge']:
            project = item['project']
            note = f'{MERGE_NOTE_MARK}：{item["source_name"]}'
            if note not in (project.remarks or ''):
                project.remarks = self.append_note(project.remarks, note)
                project.save(update_fields=['remarks'])
                merged += 1
        return created, updated, merged

    def place_files(self, source_dir):
        """按核对表把归集到的任务书复制进课题目录；只复制，源目录不动。"""
        csv_path = source_dir / TASK_CSV
        file_dir = source_dir / TASK_FILE_DIR
        if not csv_path.exists() or not file_dir.exists():
            raise CommandError(f'找不到核对表或任务书目录：{csv_path} / {file_dir}')

        projects = {project.name: project for project in Project.objects.filter(funding_category='self_funded')}
        disk_files = [name for name in file_dir.iterdir() if name.is_file()]
        # 核对表的"、"是文件名自带的顿号，不能当分隔符，只能反查磁盘文件名是否是单元格子串。
        rows = list(csv.DictReader(csv_path.open(encoding='utf-8-sig')))
        placed = skipped = unresolved = 0
        by_target = {}
        missing = []
        for row in rows:
            name = (row.get('课题名称') or '').strip()
            cell = (row.get('来源任务书') or '').strip()
            if name not in projects:
                continue
            project = projects[name]
            if not project.directory_path or not Path(project.directory_path).exists():
                missing.append(f'{name}（课题目录不存在）')
                continue
            hits = [f for f in disk_files if f.name in cell] if cell and '未找到' not in cell else []
            if not hits:
                unresolved += 1
                missing.append(f'{name}（核对表无可用源文件）')
                continue
            for handle in hits:
                target_dir = Path(project.directory_path) / self.folder_for(handle.name)
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / handle.name
                if target.exists() and target.stat().st_size == handle.stat().st_size:
                    skipped += 1
                    continue
                shutil.copy2(handle, target)
                placed += 1
                by_target[self.folder_for(handle.name)] = by_target.get(self.folder_for(handle.name), 0) + 1
        self.stdout.write(self.style.SUCCESS(
            f'任务书复制：新增 {placed}，已存在跳过 {skipped}；落位分布 {by_target}'
        ))
        self.stdout.write(f'未取到文件：{unresolved} 条')
        extra, review, orphan = self.place_by_name(file_dir, list(projects.values()))
        self.stdout.write(self.style.SUCCESS(
            f'按课题名补投：新增 {extra}（其中 {review} 条建议复核），无归属 {orphan}'
        ))

    def place_by_name(self, file_dir, projects):
        """核对表没覆盖到的归集文件，按课题名归位。

        核对表只填了 109 条来源，磁盘上还躺着 73 个同名材料；
        名称归一化后完全相等直接投，仅相似的要在次优分差距足够大时才投，并列一律不投。
        """
        placed_names = set()
        for project in projects:
            base = Path(project.directory_path or '')
            for sub in ('01_申报', '02_立项', '03_开题及任务书', '04_中期', '06_结题'):
                folder = base / sub
                if folder.is_dir():
                    placed_names.update(handle.name for handle in folder.iterdir())

        named = [(normalized_expense_description(project.name), project) for project in projects]
        placed = review = orphan = 0
        for handle in sorted(file_dir.iterdir()):
            if not handle.is_file() or handle.name in placed_names:
                continue
            stem = normalized_expense_description(self.strip_filename(handle.name))
            if not stem:
                continue
            scored = sorted(
                ((similarity_score(stem, name), project) for name, project in named),
                key=lambda item: item[0],
                reverse=True,
            )
            if not scored:
                continue
            best_score, best_project = scored[0]
            margin = best_score - scored[1][0] if len(scored) > 1 else 1.0
            if best_score < 0.90 or margin < 0.10:
                orphan += 1
                continue
            target_dir = Path(best_project.directory_path) / self.folder_for(handle.name)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / handle.name
            if target.exists() and target.stat().st_size == handle.stat().st_size:
                continue
            shutil.copy2(handle, target)
            placed += 1
            if best_score < 1.0:
                review += 1
                self.stdout.write(f'  按名归位(复核) {handle.name[:40]} -> {best_project.name[:30]}')
        return placed, review, orphan

    @staticmethod
    def strip_filename(filename):
        stem = re.sub(r'\.[A-Za-z0-9]+$', '', filename)
        stem = re.sub(r'^\d+[、\.]?\s*', '', stem)
        stem = re.sub(r'[（(][^）)]*(?:不含税|万|盖章)[^）)]*[）)]', '', stem)
        stem = re.sub(r'[-_—\s]*(任务书|申报书|立项书|盖章版|合同|扫描件|中期报告)$', '', stem)
        return stem
        report_path = Path(settings.BASE_DIR) / 'tmp' / f'任务书归位缺口_{timezone.localtime():%Y%m%d_%H%M%S}.txt'
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text('\n'.join(missing), encoding='utf-8')
        self.stdout.write(f'缺口清单 -> {report_path}')

    @staticmethod
    def folder_for(filename):
        # 中期必须在立项之前判：'自主立项中期验收意见表' 同时含两个词，但它属于中期材料。
        if '中期' in filename:
            return '04_中期'
        if '结题' in filename:
            return '06_结题'
        if '申报' in filename:
            return '01_申报'
        if '立项' in filename or '合同' in filename:
            return '02_立项'
        return '03_开题及任务书'

    def refresh_expense_snapshots(self):
        """按新课题池重算当前批次的快照。

        ExpenseImport 按 (format_version, file_sha256) 唯一，同一份余额表永远复用旧批次，
        课题档案变了也不会自动重算，因此这里原地重建该批次快照。
        硬闸：专项课题逐课题金额必须与刷新前完全一致，否则整体回滚。
        """
        data_path = Path(settings.BASE_DIR) / 'zichouktfeiyong' / EXPENSE_TARGET_NAME
        if not data_path.exists():
            raise CommandError(f'找不到支出监控数据文件：{data_path}')

        current = ExpenseImport.objects.filter(format_version=EXPENSE_FORMAT_VERSION).order_by('-created_at').first()
        if current is None:
            raise CommandError('尚无支出导入批次，请先跑 seed_expense_initial。')

        before = {
            row['project__project_id']: row['total']
            for row in ExpenseSnapshot.objects.filter(import_log=current, project__isnull=False)
            .values('project__project_id').annotate(total=Sum('total_expense'))
        }

        projects = list(Project.objects.order_by('project_id'))
        lookup = {project.project_id: project for project in projects}
        analysis = analyze_expense_workbook(
            data_path,
            projects,
            mappings=list(ExpenseMapping.objects.select_related('project').all()),
            threshold=current.threshold or 0.85,
        )

        after = {}
        for entry in analysis['project_rows']:
            project = lookup.get(entry['project_id'])
            if project is None or project.funding_category != 'special':
                continue
            after[project.project_id] = after.get(project.project_id, 0) + entry['total']

        special_before = {k: v for k, v in before.items()
                          if lookup.get(k) and lookup[k].funding_category == 'special'}
        moved = [k for k in set(special_before) | set(after)
                 if round(special_before.get(k, 0), 4) != round(after.get(k, 0), 4)]
        if moved:
            detail = '、'.join(
                f'{k}: {special_before.get(k, 0)} -> {after.get(k, 0)}' for k in sorted(moved)[:10]
            )
            raise CommandError(f'专项侧支出金额发生变化，已放弃刷新（保持面板归集口径不变）：{detail}')

        with transaction.atomic():
            ExpenseSnapshot.objects.filter(import_log=current).delete()
            snapshots = []
            for entry in analysis['project_rows']:
                project = lookup.get(entry['project_id'])
                if project is None:
                    continue
                snapshots.append(ExpenseSnapshot(
                    import_log=current,
                    project=project,
                    project_name=entry['project_name'],
                    company_name=entry['company_names'][:100],
                    matched_description=entry['sample_desc'] or '',
                    match_score=entry['max_score'],
                    total_expense=entry['total'],
                    funding_category=project.funding_category,
                ))
            ExpenseSnapshot.objects.bulk_create(snapshots)

        split = {
            row['funding_category']: row['total']
            for row in ExpenseSnapshot.objects.filter(import_log=current)
            .values('funding_category').annotate(total=Sum('total_expense'))
        }
        self.stdout.write(self.style.SUCCESS(
            f'支出快照已刷新：批次#{current.id} 共 {len(snapshots)} 条，'
            f'专项 {split.get("special", 0)}（与刷新前一致），'
            f'自筹 {split.get("self_funded", 0)}，未匹配描述 {len(analysis["unmatched_rows"])} 组'
        ))

    def backup_database(self):
        source = Path(settings.BASE_DIR) / 'db.sqlite3'
        if not source.exists():
            raise CommandError(f'找不到数据库文件：{source}')
        target_dir = Path(settings.BASE_DIR) / 'backups'
        target_dir.mkdir(exist_ok=True)
        target = target_dir / f'db_before_self_funded_{timezone.localtime():%Y%m%d_%H%M%S}.sqlite3'
        shutil.copy2(source, target)
        self.stdout.write(self.style.SUCCESS(f'已备份数据库 -> backups/{target.name}'))
