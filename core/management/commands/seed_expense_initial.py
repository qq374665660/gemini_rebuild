"""清理经费监控历史数据，并以三张源表作为初始数据重新导入。

用法：
    python manage.py seed_expense_initial --desktop-dir "C:/Users/tuantuan/Desktop" --remove-test-project --yes

三张源表：
    外部立项课题经费管理台账*.xlsx  -> SpecialLedgerImport(external)
    院自主立项课题经费管理台账*.xlsx -> SpecialLedgerImport(institute)
    2019-2026研发费用多维度科目余额表*.XLSX -> 支出监控_当前月.xlsx + ExpenseImport 批次

导入复用视图所用的同一套解析/入库代码，保证结果与界面上传一致。
"""

import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.expense_analysis import (
    EXPENSE_FORMAT_VERSION,
    analyze_expense_workbook,
    file_sha256,
    inspect_expense_workbook,
)
from core.models import (
    ExpenseImport,
    ExpenseMapping,
    ExpenseSnapshot,
    Project,
    SpecialLedgerAssignment,
    SpecialLedgerImport,
    SpecialLedgerRow,
)
from core.special_ledger import (
    LEDGER_EXTERNAL,
    LEDGER_INSTITUTE,
    SpecialLedgerError,
    parse_special_ledger,
)

DEFAULT_TEST_PROJECT_ID = 'TEST-FIX-001'
# 归集面板读取的固定文件名；余额表内容复制到这里后即可被视图识别。
EXPENSE_TARGET_NAME = '支出监控_当前月.xlsx'


class Command(BaseCommand):
    help = '清理经费监控历史数据，并导入外部/院自主台账与科目余额表作为初始数据。'

    def add_arguments(self, parser):
        parser.add_argument(
            '--desktop-dir',
            default=str(Path.home() / 'Desktop'),
            help='三张源表所在目录（默认当前用户桌面）。',
        )
        parser.add_argument(
            '--external-file', help='外部立项课题经费管理台账文件名或完整路径。'
        )
        parser.add_argument(
            '--institute-file', help='院自主立项课题经费管理台账文件名或完整路径。'
        )
        parser.add_argument(
            '--balance-file', help='研发费用多维度科目余额表文件名或完整路径。'
        )
        parser.add_argument(
            '--remove-test-project',
            nargs='?',
            const=DEFAULT_TEST_PROJECT_ID,
            metavar='PROJECT_ID',
            help='删除测试课题（默认 TEST-FIX-001），可重复指定多个。',
            action='append',
            default=[],
        )
        parser.add_argument('--keep-expense-data', action='store_true', help='保留支出监控_当前月.xlsx。')
        parser.add_argument('--no-backup', action='store_true', help='跳过导入前的数据库备份。')
        parser.add_argument('--yes', action='store_true', help='确认执行清空操作。')

    def handle(self, *args, **options):
        desktop = Path(options['desktop_dir'])
        sources = {
            'external': self._resolve(desktop, options['external_file'], '外部立项课题经费管理台账'),
            'institute': self._resolve(desktop, options['institute_file'], '院自主立项课题经费管理台账'),
            'balance': self._resolve(desktop, options['balance_file'], '研发费用多维度科目余额表'),
        }
        test_ids = [item for item in options['remove_test_project'] if item]

        self.stdout.write('源表：')
        for key, path in sources.items():
            self.stdout.write(f'  {key:9s} {path.name}  ({path.stat().st_size:,} 字节)')

        rows_before = self._counts()
        self.stdout.write('当前数据量：')
        for name, count in rows_before.items():
            self.stdout.write(f'  {name:24s} {count:,}')

        if test_ids:
            self.stdout.write('待删除测试课题：')
            for project_id in test_ids:
                project = Project.objects.filter(project_id=project_id).first()
                if project is None:
                    self.stdout.write(f'  {project_id} （系统内不存在，跳过）')
                else:
                    self.stdout.write(f'  {project_id}  {project.name}  [{project.status}]  -> {project.directory_path}')

        if not options['yes']:
            self.stdout.write(self.style.WARNING('未加 --yes，仅预览，不做任何修改。'))
            return

        if not options['no_backup']:
            self.backup_database()

        with transaction.atomic():
            self.clear_expense_batches()
            self.delete_test_projects(test_ids)
            self.import_ledgers(sources, LEDGER_EXTERNAL, sources['external'])
            self.import_ledgers(sources, LEDGER_INSTITUTE, sources['institute'])
            self.import_balance(sources['balance'], options['keep_expense_data'])

        self.report()

    def _resolve(self, desktop, explicit, needle):
        if explicit:
            path = Path(explicit)
            if not path.is_absolute():
                path = desktop / explicit
        else:
            matches = sorted(
                (p for p in desktop.glob('*.xlsx') if needle[:6] in p.name),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            matches += sorted(
                (p for p in desktop.glob('*.XLSX') if needle[:6] in p.name),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not matches:
                raise CommandError(f'在 {desktop} 找不到“{needle}”，请用 --*-file 指定。')
            path = matches[0]
        if not path.exists():
            raise CommandError(f'文件不存在：{path}')
        return path

    def _counts(self):
        return {
            'core_expenseimport': ExpenseImport.objects.count(),
            'core_expensesnapshot': ExpenseSnapshot.objects.count(),
            'core_expensemapping': ExpenseMapping.objects.count(),
            'core_specialledgerimport': SpecialLedgerImport.objects.count(),
            'core_specialledgerrow': SpecialLedgerRow.objects.count(),
            'core_specialledgerassign': SpecialLedgerAssignment.objects.count(),
            'core_project': Project.objects.count(),
        }

    def backup_database(self):
        source = Path(settings.BASE_DIR) / 'db.sqlite3'
        if not source.exists():
            raise CommandError(f'找不到数据库文件：{source}')
        target_dir = Path(settings.BASE_DIR) / 'backups'
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = timezone.localtime().strftime('%Y%m%d_%H%M%S')
        target = target_dir / f'db_before_seed_expense_{stamp}.sqlite3'
        shutil.copy2(source, target)
        self.stdout.write(self.style.SUCCESS(f'已备份数据库 -> backups/{target.name}'))

    def clear_expense_batches(self):
        deleted = []
        for model in (SpecialLedgerRow, SpecialLedgerAssignment, SpecialLedgerImport, ExpenseSnapshot, ExpenseImport):
            count, _ = model.objects.all().delete()
            deleted.append(f'{model.__name__}:{count}')
        ledger_dir = getattr(settings, 'SPECIAL_LEDGER_DIR', None)
        ledger_dir = Path(ledger_dir) if ledger_dir else Path(settings.BASE_DIR) / 'zichouktfeiyong' / '专项台账'
        if ledger_dir.exists():
            stamp = timezone.localtime().strftime('%Y%m%d_%H%M%S')
            quarantine = ledger_dir.with_name(f'{ledger_dir.name}_old_{stamp}')
            ledger_dir.rename(quarantine)
            self.stdout.write(f'旧台账文件 -> {quarantine.name}/')
        self.stdout.write(self.style.SUCCESS(f'已清空历史批次：{", ".join(deleted)}'))

    def delete_test_projects(self, project_ids):
        for project_id in project_ids:
            project = Project.objects.filter(project_id=project_id).first()
            if project is None:
                continue
            directory = project.directory_path
            name = project.name
            project.delete()
            if directory:
                path = Path(directory)
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
            self.stdout.write(self.style.SUCCESS(f'已删除测试课题 {project_id}（{name}）及其空目录骨架'))

    def import_ledgers(self, sources, ledger_type, source_path):
        projects = list(Project.objects.all())
        try:
            result = parse_special_ledger(source_path, ledger_type, projects=projects)
        except SpecialLedgerError as exc:
            raise CommandError(f'{ledger_type} 台账解析失败：{exc}') from exc

        file_hash = file_sha256(source_path)
        file_mtime = datetime.fromtimestamp(
            source_path.stat().st_mtime,
            tz=ZoneInfo(getattr(settings, 'TIME_ZONE', 'Asia/Shanghai')),
        )
        store_dir = Path(settings.BASE_DIR) / 'zichouktfeiyong' / '专项台账'
        store_dir.mkdir(parents=True, exist_ok=True)
        store_path = store_dir / f'{ledger_type}_{source_path.name}'
        shutil.copy2(source_path, store_path)

        label = dict(SpecialLedgerImport.LEDGER_TYPE_CHOICES)[ledger_type]
        import_log, created = SpecialLedgerImport.objects.get_or_create(
            ledger_type=ledger_type,
            file_sha256=file_hash,
            defaults={
                'source_file': str(store_path),
                'original_filename': source_path.name[:255],
                'sheet_name': result['sheet_name'],
                'row_total': result['row_total'],
                'matched_total': result['matched_total'],
                'ambiguous_total': result['ambiguous_total'],
                'ignored_total': result['ignored_total'],
                'totals': {k: str(v) for k, v in result['totals'].items() if v is not None},
                'ignored_detail': [
                    {
                        'ledger_name': item['ledger_name'],
                        'ledger_status': item['ledger_status'],
                        'funder': item['funder'],
                        'executed_total': str(item['executed_total'])
                        if item['executed_total'] is not None
                        else None,
                        'reason': item['reason'],
                    }
                    for item in result['ignored_rows']
                ],
                'file_mtime': file_mtime,
            },
        )
        if created:
            SpecialLedgerRow.objects.bulk_create(
                [
                    SpecialLedgerRow(
                        import_log=import_log,
                        ledger_type=ledger_type,
                        row_number=record['row_number'],
                        sequence=record['sequence'][:50],
                        project=record['project'],
                        ledger_name=record['ledger_name'][:255],
                        owning_unit=record['owning_unit'][:100],
                        funder=record['funder'][:255],
                        ledger_status=record['ledger_status'][:50],
                        principal=record['principal'][:100],
                        start_text=record['start_text'][:50],
                        end_text=record['end_text'][:50],
                        match_state=record['match_state'],
                        **{
                            field: record.get(field)
                            for field in (
                                'contract_total',
                                'contract_allocated',
                                'received_amount',
                                'approved_budget',
                                'executed_total',
                                'remaining_amount',
                                'year_disposable',
                                'year_budget',
                                'year_executed',
                            )
                        },
                    )
                    for record in result['records']
                ]
            )
        state = '新批次' if created else '已存在批次（复用）'
        self.stdout.write(
            self.style.SUCCESS(
                f'{label}台账：{state}，共 {result["row_total"]} 行，'
                f'匹配 {result["matched_total"]}，待确认 {result["ambiguous_total"]}，忽略 {result["ignored_total"]}'
            )
        )

    def import_balance(self, source_path, keep_data_file):
        data_path = Path(settings.BASE_DIR) / 'zichouktfeiyong' / EXPENSE_TARGET_NAME
        data_path.parent.mkdir(parents=True, exist_ok=True)
        inspect_expense_workbook(source_path)
        shutil.copy2(source_path, data_path)

        projects = list(Project.objects.order_by('project_id'))
        project_lookup = {project.project_id: project for project in projects}
        analysis = analyze_expense_workbook(
            data_path,
            projects,
            mappings=list(ExpenseMapping.objects.select_related('project').all()),
            threshold=0.85,
        )
        file_hash = file_sha256(data_path)
        import_log, created = ExpenseImport.objects.get_or_create(
            format_version=EXPENSE_FORMAT_VERSION,
            file_sha256=file_hash,
            defaults={
                'source_file': str(data_path),
                'sheet_name': analysis['sheet_name'],
                'original_filename': source_path.name[:255],
                'file_mtime': datetime.fromtimestamp(
                    data_path.stat().st_mtime,
                    tz=ZoneInfo(getattr(settings, 'TIME_ZONE', 'Asia/Shanghai')),
                ),
                'threshold': 0.85,
            },
        )
        created_snapshots = 0
        if created:
            snapshots = [
                ExpenseSnapshot(
                    import_log=import_log,
                    project=project_lookup[entry['project_id']],
                    project_name=entry['project_name'],
                    company_name=entry['company_names'][:100],
                    matched_description=entry['sample_desc'] or '',
                    match_score=entry['max_score'],
                    total_expense=entry['total'],
                    funding_category=project_lookup[entry['project_id']].funding_category,
                )
                for entry in analysis['project_rows']
                if entry['project_id'] in project_lookup
            ]
            if snapshots:
                ExpenseSnapshot.objects.bulk_create(snapshots)
            created_snapshots = len(snapshots)
        self.stdout.write(
            self.style.SUCCESS(
                f'归集余额表：{data_path.name} 已更新（sha256 {file_hash[:12]}…），'
                f'纳入 {analysis["filtered_rows"]} 条6606明细，写入 {created_snapshots} 条课题快照，'
                f'未匹配描述 {len(analysis["unmatched_rows"])} 组'
            )
        )

    def report(self):
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('导入后数据量：'))
        for name, count in self._counts().items():
            self.stdout.write(f'  {name:24s} {count:,}')
        for ledger_type in (LEDGER_EXTERNAL, LEDGER_INSTITUTE):
            latest = (
                SpecialLedgerImport.objects.filter(ledger_type=ledger_type)
                .order_by('-created_at')
                .first()
            )
            if latest:
                self.stdout.write(
                    f'  {ledger_type:9s} 批次#{latest.id} {latest.original_filename} '
                    f'合计 {latest.totals}'
                )
