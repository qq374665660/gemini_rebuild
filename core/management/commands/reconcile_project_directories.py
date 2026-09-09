import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import Project
from core.views import _get_project_folder_name, _sanitize_folder_segment


def _sha256(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with path.open('rb') as file_obj:
        for chunk in iter(lambda: file_obj.read(chunk_size), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _same_file(left, right):
    try:
        return left.stat().st_size == right.stat().st_size and _sha256(left) == _sha256(right)
    except OSError:
        return False


def _inventory(root):
    file_count = 0
    directory_count = 0
    total_bytes = 0
    for current_root, directories, files in os.walk(root, followlinks=False):
        directory_count += len(directories)
        for filename in files:
            file_count += 1
            try:
                total_bytes += (Path(current_root) / filename).stat().st_size
            except OSError:
                pass
    return {
        'file_count': file_count,
        'directory_count': directory_count,
        'total_bytes': total_bytes,
    }


def _unique_path(destination, source_folder_name, is_directory=False, reserved=None):
    reserved = reserved if reserved is not None else set()
    source_label = _sanitize_folder_segment(source_folder_name, 'old-folder')[:80]
    if is_directory:
        candidate = destination.with_name(f'{destination.name}__from_old_folder_{source_label}')
    else:
        candidate = destination.with_name(
            f'{destination.stem}__from_old_folder_{source_label}{destination.suffix}'
        )
    counter = 2
    while candidate.exists() or str(candidate).lower() in reserved:
        if is_directory:
            candidate = destination.with_name(
                f'{destination.name}__from_old_folder_{source_label}_{counter}'
            )
        else:
            candidate = destination.with_name(
                f'{destination.stem}__from_old_folder_{source_label}_{counter}{destination.suffix}'
            )
        counter += 1
    reserved.add(str(candidate).lower())
    return candidate


def _merge_directory(source, destination, apply_changes):
    result = {
        'copied_files': 0,
        'identical_files': 0,
        'conflict_files': 0,
        'created_directories': 0,
        'errors': [],
        'file_mappings': [],
    }
    reserved = set()

    def merge_level(source_dir, destination_dir):
        if destination_dir.exists() and not destination_dir.is_dir():
            destination_dir = _unique_path(
                destination_dir, source.name, is_directory=True, reserved=reserved
            )
        if not destination_dir.exists():
            if apply_changes:
                destination_dir.mkdir(parents=True, exist_ok=True)
            result['created_directories'] += 1

        try:
            entries = list(source_dir.iterdir())
        except OSError as exc:
            result['errors'].append(f'Cannot read directory {source_dir}: {exc}')
            return

        for entry in entries:
            try:
                if entry.is_symlink():
                    result['errors'].append(f'Symbolic link was not processed: {entry}')
                    continue
                is_directory = entry.is_dir()
                is_file = entry.is_file()
            except OSError as exc:
                result['errors'].append(f'Cannot inspect {entry}: {exc}')
                continue

            target = destination_dir / entry.name
            if is_directory:
                merge_level(entry, target)
                continue
            if not is_file:
                result['errors'].append(f'Unsupported filesystem item: {entry}')
                continue

            final_target = target
            try:
                if target.exists() and target.is_file() and _same_file(entry, target):
                    result['identical_files'] += 1
                elif target.exists():
                    final_target = _unique_path(target, source.name, reserved=reserved)
                    result['conflict_files'] += 1
                    if apply_changes:
                        final_target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(entry, final_target)
                else:
                    reserved.add(str(target).lower())
                    result['copied_files'] += 1
                    if apply_changes:
                        final_target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(entry, final_target)

                if apply_changes and (
                    not final_target.exists() or not _same_file(entry, final_target)
                ):
                    raise OSError('copied file failed SHA-256 verification')
                result['file_mappings'].append({
                    'source': str(entry),
                    'destination': str(final_target),
                })
            except OSError as exc:
                result['errors'].append(f'Cannot process file {entry}: {exc}')

    merge_level(source, destination)
    return result


def _unique_quarantine_path(quarantine_root, source_name):
    candidate = quarantine_root / source_name
    counter = 2
    while candidate.exists():
        candidate = quarantine_root / f'{source_name}_{counter}'
        counter += 1
    return candidate


class Command(BaseCommand):
    help = (
        'Safely merge duplicate project folders. The default mode is dry-run; '
        '--apply copies and verifies files, then moves old folders to quarantine.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--project-id', action='append', dest='project_ids')
        parser.add_argument('--quarantine-root')
        parser.add_argument('--report')

    def handle(self, *args, **options):
        apply_changes = bool(options['apply'])
        projects_root = Path(settings.PROJECTS_ROOT)
        if not projects_root.is_dir():
            raise CommandError(f'Projects root does not exist: {projects_root}')

        run_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        quarantine_base = Path(
            options.get('quarantine_root')
            or projects_root.parent / 'projects_duplicate_quarantine'
        )
        quarantine_root = quarantine_base / run_id
        report_path = Path(
            options.get('report')
            or Path(settings.BASE_DIR)
            / 'directory_reconcile_reports'
            / f'{run_id}_{"apply" if apply_changes else "dry_run"}.json'
        )

        project_query = Project.objects.all().order_by('project_id')
        if options.get('project_ids'):
            project_query = project_query.filter(project_id__in=options['project_ids'])

        root_directories = [path for path in projects_root.iterdir() if path.is_dir()]
        candidate_owners = {}
        project_groups = []
        for project in project_query:
            expected = projects_root / _get_project_folder_name(project)
            project_id_segment = _sanitize_folder_segment(project.project_id, 'no-id')
            token = f'-{project_id_segment}-'
            matches = [path for path in root_directories if token in path.name]
            duplicates = [path for path in matches if path != expected]
            if not duplicates:
                continue
            for duplicate in duplicates:
                candidate_owners.setdefault(str(duplicate).lower(), []).append(project.project_id)
            project_groups.append((project, expected, duplicates))

        report = {
            'run_id': run_id,
            'mode': 'apply' if apply_changes else 'dry-run',
            'projects_root': str(projects_root),
            'quarantine_root': str(quarantine_root),
            'project_groups': [],
            'summary': {
                'duplicate_projects': 0,
                'duplicate_directories': 0,
                'quarantined_directories': 0,
                'copied_files': 0,
                'identical_files': 0,
                'conflict_files': 0,
                'errors': 0,
            },
        }

        for project, expected, duplicates in project_groups:
            group_report = {
                'project_id': project.project_id,
                'project_name': project.name,
                'expected_directory': str(expected),
                'expected_exists': expected.is_dir(),
                'duplicates': [],
            }
            report['summary']['duplicate_projects'] += 1
            if not expected.is_dir():
                group_report['error'] = 'Expected current folder is missing; skipped for safety.'
                report['summary']['errors'] += 1
                report['project_groups'].append(group_report)
                continue

            for duplicate in duplicates:
                duplicate_report = {
                    'source_directory': str(duplicate),
                    'inventory': _inventory(duplicate),
                }
                report['summary']['duplicate_directories'] += 1
                owners = candidate_owners.get(str(duplicate).lower(), [])
                if len(owners) != 1:
                    duplicate_report['errors'] = [
                        f'Directory matched multiple projects and was skipped: {owners}'
                    ]
                    report['summary']['errors'] += 1
                    group_report['duplicates'].append(duplicate_report)
                    continue

                merge_result = _merge_directory(duplicate, expected, apply_changes)
                duplicate_report['merge'] = merge_result
                for key in ('copied_files', 'identical_files', 'conflict_files'):
                    report['summary'][key] += merge_result[key]
                report['summary']['errors'] += len(merge_result['errors'])

                if apply_changes and not merge_result['errors']:
                    quarantine_root.mkdir(parents=True, exist_ok=True)
                    quarantine_target = _unique_quarantine_path(quarantine_root, duplicate.name)
                    shutil.move(str(duplicate), str(quarantine_target))
                    duplicate_report['quarantine_directory'] = str(quarantine_target)
                    report['summary']['quarantined_directories'] += 1
                group_report['duplicates'].append(duplicate_report)
            report['project_groups'].append(group_report)

        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        self.stdout.write(json.dumps(report['summary'], ensure_ascii=False))
        self.stdout.write(f'Report: {report_path}')
        if apply_changes and report['summary']['errors']:
            raise CommandError(
                'Errors occurred. Affected source folders were not quarantined; see report.'
            )
