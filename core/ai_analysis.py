"""
AI分析服务模块 - 重构版
支持DeepSeek和Kimi API调用，直接处理Word/PDF文档
"""
import requests
import json
import time
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from django.conf import settings
from .models import APIConfig
from .analysis_parsing import parse_ai_analysis
from .ai_providers import (
    PROVIDER_ORDER,
    config_is_usable,
    get_model_name,
    post_chat_completion,
    provider_payload,
    ready_configs,
)

logger = logging.getLogger(__name__)

class AIAnalysisService:
    MAX_DOCUMENT_CHARS = 120000

    def __init__(self):
        # DeepSeek API配置
        self.deepseek_base_url = 'https://api.deepseek.com/v1/chat/completions'
        
        # Kimi API配置
        self.kimi_base_url = 'https://api.moonshot.cn/v1/chat/completions'
    
    def get_api_config(self, service_name):
        """从数据库获取API配置"""
        try:
            config = APIConfig.objects.filter(
                service_name=service_name,
                is_active=True
            ).first()
            if config:
                api_key = config.get_api_key()
                # 验证API密钥格式和有效性
                if api_key and api_key.strip() and len(api_key.strip()) > 10:
                    return api_key.strip()
                else:
                    logger.warning(f"{service_name} API密钥格式无效或为空")
                    return None
            else:
                logger.warning(f"{service_name} API配置未找到或未启用")
                return None
        except Exception as e:
            logger.error(f"获取API配置失败: {e}")
            return None

    @staticmethod
    def _record_api_health(service_name, success):
        """同步记录实际分析请求的可用性，防止失效服务反复拖慢每次分析。"""
        try:
            from django.utils import timezone

            APIConfig.objects.filter(service_name=service_name).update(
                test_success=success,
                last_test_time=timezone.now(),
            )
        except Exception as exc:
            logger.warning('更新 %s API 健康状态失败: %s', service_name, exc)
        
    @staticmethod
    def _decode_text(file_content):
        for encoding in ['utf-8', 'utf-8-sig', 'gbk', 'gb2312']:
            try:
                return file_content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return file_content.decode('utf-8', errors='ignore')

    @staticmethod
    def _extract_pdf_text(file_content):
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(file_content))
        pages = []
        for page_number, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or '').strip()
            if page_text:
                pages.append(f"[[任务书第{page_number}页]]\n{page_text}")
        return '\n\n'.join(pages)

    @staticmethod
    def _extract_docx_text(file_content):
        namespace = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        with zipfile.ZipFile(BytesIO(file_content)) as archive:
            document_xml = archive.read('word/document.xml')
        root = ElementTree.fromstring(document_xml)
        blocks = []
        body = root.find('w:body', namespace)
        if body is None:
            return ''
        for child in body:
            if child.tag.endswith('}p'):
                text = ''.join(node.text or '' for node in child.findall('.//w:t', namespace)).strip()
                if text:
                    blocks.append(text)
            elif child.tag.endswith('}tbl'):
                for row in child.findall('./w:tr', namespace):
                    cells = []
                    for cell in row.findall('./w:tc', namespace):
                        value = ''.join(node.text or '' for node in cell.findall('.//w:t', namespace)).strip()
                        cells.append(value)
                    if any(cells):
                        blocks.append(' | '.join(cells))
        return '\n'.join(blocks)

    @classmethod
    def _convert_doc_with_word(cls, file_content, file_name):
        """使用服务器已有的 Microsoft Word 后台转换旧版 DOC。"""
        powershell_path = shutil.which('powershell.exe') or shutil.which('powershell')
        if not powershell_path:
            raise RuntimeError('未检测到 PowerShell，无法调用 Microsoft Word 转换。')

        safe_name = Path(file_name).name or 'taskbook.doc'
        with tempfile.TemporaryDirectory(prefix='taskbook_word_') as temp_dir:
            source_path = Path(temp_dir) / safe_name
            target_path = source_path.with_suffix('.docx')
            source_path.write_bytes(file_content)

            def ps_quote(value):
                return "'" + str(value).replace("'", "''") + "'"

            script = f"""
$ErrorActionPreference = 'Stop'
$word = $null
$document = $null
try {{
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $document = $word.Documents.Open({ps_quote(source_path)}, $false, $true)
    $document.SaveAs2({ps_quote(target_path)}, 16)
}} finally {{
    if ($document -ne $null) {{ $document.Close(0) }}
    if ($word -ne $null) {{ $word.Quit() }}
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}}
"""
            run_options = {
                'capture_output': True,
                'timeout': 90,
                'check': False,
            }
            if os.name == 'nt':
                run_options['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                [
                    powershell_path,
                    '-NoProfile',
                    '-NonInteractive',
                    '-ExecutionPolicy',
                    'Bypass',
                    '-Command',
                    script,
                ],
                **run_options,
            )
            if result.returncode != 0 or not target_path.exists():
                detail = cls._decode_text(result.stderr or result.stdout).strip()
                if len(detail) > 240:
                    detail = detail[:240] + '…'
                raise RuntimeError(detail or 'Microsoft Word 未能完成格式转换。')
            return target_path.read_bytes()

    @classmethod
    def _extract_doc_text(cls, file_content, file_name):
        soffice_candidates = [
            os.environ.get('LIBREOFFICE_PATH'),
            r'C:\Program Files\LibreOffice\program\soffice.exe',
            r'C:\Program Files (x86)\LibreOffice\program\soffice.exe',
        ]
        soffice_path = next((path for path in soffice_candidates if path and os.path.exists(path)), None)
        conversion_errors = []
        if soffice_path:
            safe_name = Path(file_name).name or 'taskbook.doc'
            with tempfile.TemporaryDirectory(prefix='taskbook_doc_') as temp_dir:
                source_path = Path(temp_dir) / safe_name
                source_path.write_bytes(file_content)
                result = subprocess.run(
                    [soffice_path, '--headless', '--convert-to', 'docx', '--outdir', temp_dir, str(source_path)],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                converted_path = source_path.with_suffix('.docx')
                if result.returncode == 0 and converted_path.exists():
                    return cls._extract_docx_text(converted_path.read_bytes())
                conversion_errors.append('LibreOffice 转换失败')

        try:
            converted_content = cls._convert_doc_with_word(file_content, file_name)
            return cls._extract_docx_text(converted_content)
        except Exception as exc:
            logger.warning('Microsoft Word 转换旧版 DOC 失败: %s', exc)
            conversion_errors.append(f'Microsoft Word 转换失败：{exc}')

        detail = '；'.join(conversion_errors)
        raise RuntimeError(
            f'旧版 .doc 转换失败（{detail}）。请确认服务器已安装 Microsoft Word/LibreOffice，'
            '或将文件另存为 .docx、PDF 后重试。'
        )

    def prepare_file_for_analysis(self, uploaded_file):
        """把任务书转换为模型可读文本，并保留 PDF 页码标记。"""
        try:
            # 重置文件指针到开始位置
            uploaded_file.seek(0)
            
            # 读取文件内容
            file_content = uploaded_file.read()
            file_name = uploaded_file.name
            file_size = len(file_content)
            
            # 获取文件扩展名
            file_extension = file_name.lower().split('.')[-1] if '.' in file_name else ''
            
            if file_extension == 'txt':
                text_content = self._decode_text(file_content)
            elif file_extension == 'pdf':
                text_content = self._extract_pdf_text(file_content)
            elif file_extension == 'docx':
                text_content = self._extract_docx_text(file_content)
            elif file_extension == 'doc':
                text_content = self._extract_doc_text(file_content, file_name)
            else:
                return {
                    'type': 'error',
                    'error': '仅支持 PDF、DOC、DOCX、TXT 格式的任务书。',
                    'filename': file_name,
                }

            if not text_content or len(text_content.strip()) < 30:
                return {
                    'type': 'error',
                    'error': '未能从文档中提取可识别文字。扫描版 PDF 请先进行 OCR 后再分析。',
                    'filename': file_name,
                }
            truncated = len(text_content) > self.MAX_DOCUMENT_CHARS
            text_content = text_content[:self.MAX_DOCUMENT_CHARS]
            return {
                'type': 'text',
                'content': text_content,
                'filename': file_name,
                'size': file_size,
                'extension': file_extension,
                'truncated': truncated,
            }
            
        except Exception as e:
            logger.error(f"文件准备失败: {e}")
            return {
                'type': 'error',
                'error': f"文件处理失败: {e}",
                'filename': uploaded_file.name if hasattr(uploaded_file, 'name') else 'unknown'
            }

    @staticmethod
    def _document_excerpt(text, analysis_type, max_chars=60000):
        if len(text) <= max_chars:
            return text
        headings = (
            ['预期成果及考核指标', '考核指标', '主要经济、社会指标', '课题进度计划']
            if analysis_type == 'output_metrics'
            else ['课题研究目标', '课题研究内容', '技术路线', '研究方法', '创新与突破', '课题进度计划']
        )
        excerpts = [text[:12000]]
        window = max(6000, (max_chars - 12000) // max(len(headings), 1))
        for heading in headings:
            position = text.find(heading)
            if position >= 0:
                excerpts.append(text[max(0, position - 1000):position + window])
        return '\n\n[[文档节选分隔]]\n\n'.join(excerpts)[:max_chars]

    def _build_prompt(self, file_data, analysis_type, max_chars=60000):
        document_text = self._document_excerpt(file_data['content'], analysis_type, max_chars=max_chars)
        source_header = (
            f"文件名：{file_data['filename']}\n"
            f"文件格式：{file_data.get('extension', '未知')}\n"
            "以下是从任务书正文和表格中提取的可检索文字；[[任务书第N页]] 是可靠页码标记。\n"
        )
        if analysis_type == 'research_content':
            schema = r'''
{
  "analysis_type": "research_content",
  "research_goal": "任务书中的课题研究目标原文或忠实概括",
  "summary": "整体研究范围概述",
  "research_sections": [
    {
      "title": "任务书中的部分/专题标题",
      "description": "该部分总体说明",
      "subitems": [{"title": "子项标题", "description": "子项研究内容"}]
    }
  ],
  "methods": ["研究方法及说明"],
  "technical_route": ["按先后顺序列出的技术路线节点"],
  "technical_difficulties": ["技术难点"],
  "innovations": ["创新与突破"],
  "milestones": [{"time_range": "原文时间安排", "stage_goal": "阶段目标原文"}]
}
'''
            instructions = '''
你是科研课题任务书信息抽取员。请严格以任务书原文为依据，提取“课题研究目标、课题研究内容、技术路线、研究方法、技术难点、创新与突破、课题进度计划”。
研究内容要保留任务书本身的层级与标题，不要套用通用的“背景/关键技术/预期目标”空模板，不要编造文档中不存在的内容。
'''
        else:
            schema = r'''
{
  "analysis_type": "output_metrics",
  "summary": "指标总体说明",
  "metrics": [
    {
      "category": "deliverable | benefit | milestone | other",
      "indicator_description": "任务书指标/应用项目/阶段目标的完整原文",
      "target_value": "数量、金额或完成要求，按原文填写",
      "assessment_method": "考核方式或成果形式，按原文填写",
      "planned_period": "推广时间或时间安排，按原文填写",
      "deadline": "仅在能明确换算为 YYYY-MM-DD 时填写，否则留空",
      "source_section": "考核指标 | 主要经济、社会指标 | 课题进度计划 | 其他",
      "source_page": "页码数字",
      "notes": "预期节省/转化效益等不适合放入其他字段的原文"
    }
  ]
}
'''
            instructions = '''
你是科研课题任务书考核指标抽取员。最重要的规则是：逐行保留任务书“考核指标”表中指标描述的完整原文，并同时提取数量和考核方式；绝不能用发明专利、论文、标准等通用模板替换原文。
还要把“主要经济、社会指标”表逐行提取为 category=benefit，把“课题进度计划”表逐行提取为 category=milestone，便于后续跟踪。普通考核指标使用 category=deliverable。
任务书没写的数量、方式、日期必须留空，不得估算或补造。重复行只保留一次。
'''
        return f'''{instructions}
只返回一个合法 JSON 对象，不要使用 Markdown 代码围栏，不要输出 JSON 之外的解释。字段必须符合下面的结构：
{schema}
{source_header}
----- 任务书正文开始 -----
{document_text}
----- 任务书正文结束 -----
'''
    
    def analyze_with_deepseek(self, file_data, analysis_type):
        """使用DeepSeek API进行分析"""
        api_key = self.get_api_config('deepseek')
        if not api_key:
            return None, "DeepSeek API密钥未配置、未启用或格式无效。请检查API配置。"
        
        try:
            if file_data['type'] != 'text':
                return None, f"文件处理错误: {file_data.get('error', '未提取到文本')}"
            prompt = self._build_prompt(file_data, analysis_type)

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}'
            }
            
            payload = {
                'model': 'deepseek-v4-flash',
                'messages': [
                    {'role': 'user', 'content': prompt}
                ],
                'response_format': {'type': 'json_object'},
                'max_tokens': 6000,
                'temperature': 0.1
            }
            
            response = requests.post(
                self.deepseek_base_url,
                headers=headers,
                json=payload,
                timeout=120
            )
            
            if response.status_code == 200:
                self._record_api_health('deepseek', True)
                result = response.json()
                analysis_result = result['choices'][0]['message']['content']
                return analysis_result, None
            else:
                self._record_api_health('deepseek', False)
                error_detail = response.text[:240].strip()
                error_msg = f"DeepSeek API调用失败，状态码: {response.status_code}"
                if error_detail:
                    error_msg += f"，响应: {error_detail}"
                logger.error(error_msg)
                return None, error_msg
                
        except requests.exceptions.Timeout:
            self._record_api_health('deepseek', False)
            return None, "API调用超时"
        except requests.exceptions.RequestException as e:
            self._record_api_health('deepseek', False)
            return None, f"网络请求失败: {e}"
        except Exception as e:
            logger.error(f"DeepSeek API调用异常: {e}")
            return None, f"API调用异常: {e}"
    
    def analyze_with_kimi(self, file_data, analysis_type):
        """使用Kimi API进行分析"""
        api_key = self.get_api_config('kimi')
        if not api_key:
            return None, "Kimi API密钥未配置、未启用或格式无效。请检查API配置。"
        
        try:
            if file_data['type'] != 'text':
                return None, f"文件处理错误: {file_data.get('error', '未提取到文本')}"
            prompt = self._build_prompt(file_data, analysis_type, max_chars=22000)

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}'
            }
            
            payload = {
                'model': 'moonshot-v1-8k',
                'messages': [
                    {'role': 'user', 'content': prompt}
                ],
                'max_tokens': 5000,
                'temperature': 0.1
            }
            
            response = requests.post(
                self.kimi_base_url,
                headers=headers,
                json=payload,
                timeout=120
            )
            
            if response.status_code == 200:
                self._record_api_health('kimi', True)
                result = response.json()
                analysis_result = result['choices'][0]['message']['content']
                return analysis_result, None
            else:
                self._record_api_health('kimi', False)
                error_detail = response.text[:240].strip()
                error_msg = f"Kimi API调用失败，状态码: {response.status_code}"
                if error_detail:
                    error_msg += f"，响应: {error_detail}"
                logger.error(error_msg)
                return None, error_msg
                
        except requests.exceptions.Timeout:
            self._record_api_health('kimi', False)
            return None, "API调用超时"
        except requests.exceptions.RequestException as e:
            self._record_api_health('kimi', False)
            return None, f"网络请求失败: {e}"
        except Exception as e:
            logger.error(f"Kimi API调用异常: {e}")
            return None, f"API调用异常: {e}"

    def analyze_with_service(self, file_data, analysis_type, service_name):
        """使用任意已配置的 OpenAI 兼容模型分析文档。"""
        config = APIConfig.objects.filter(service_name=service_name, is_active=True).first()
        if not config or not config_is_usable(config):
            return None, f"{dict(APIConfig.SERVICE_CHOICES).get(service_name, service_name)} 配置不完整或未启用。"

        try:
            if file_data['type'] != 'text':
                return None, f"文件处理错误: {file_data.get('error', '未提取到文本')}"
            prompt_limit = 22000 if service_name == 'kimi' else 60000
            prompt = self._build_prompt(file_data, analysis_type, max_chars=prompt_limit)
            payload = provider_payload(
                config,
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=5000 if service_name == 'kimi' else 6000,
                temperature=0.1,
                stream=False,
            )
            if service_name != 'kimi':
                payload['response_format'] = {'type': 'json_object'}
            result = post_chat_completion(config, payload, timeout=120)
            analysis_result = result['choices'][0]['message']['content']
            if not analysis_result:
                raise ValueError('模型返回了空结果')
            self._record_api_health(service_name, True)
            return analysis_result, None
        except requests.exceptions.Timeout:
            self._record_api_health(service_name, False)
            return None, "API调用超时"
        except requests.exceptions.RequestException as exc:
            self._record_api_health(service_name, False)
            detail = str(exc)
            response = getattr(exc, 'response', None)
            if response is not None and response.text:
                detail = f'{detail}，响应: {response.text[:240].strip()}'
            return None, f"网络请求失败: {detail}"
        except (KeyError, TypeError, ValueError) as exc:
            self._record_api_health(service_name, False)
            logger.error('%s 文档分析返回异常: %s', service_name, exc)
            return None, f"模型返回格式异常: {exc}"
    
    def analyze_document(self, uploaded_file, analysis_type, service_name=None):
        """分析上传的文档 - 支持所有格式"""
        start_time = time.time()
        
        try:
            # 1. 准备文件数据
            file_data = self.prepare_file_for_analysis(uploaded_file)
            
            if file_data['type'] == 'error':
                return {
                    'success': False,
                    'error': file_data['error'],
                    'processing_time': time.time() - start_time
                }
            
            # 2. 使用用户选择的模型；未选择时按配置顺序自动回退。
            analysis_result = None
            error_msg = None
            api_used = None

            selected_service = service_name if service_name in dict(APIConfig.SERVICE_CHOICES) else None
            configs = ready_configs(selected_service)
            if not configs and not selected_service:
                active_configs = {
                    config.service_name: config
                    for config in APIConfig.objects.filter(is_active=True)
                    if config_is_usable(config)
                }
                configs = [active_configs[name] for name in PROVIDER_ORDER if name in active_configs]

            if not configs:
                selected_label = dict(APIConfig.SERVICE_CHOICES).get(selected_service, 'AI服务')
                return {
                    'success': False,
                    'error': f"{selected_label}尚未配置、未启用或连接测试未通过。请先在AI模型配置页面检查。",
                    'processing_time': time.time() - start_time
                }

            for config in configs:
                analysis_result, error_msg = self.analyze_with_service(
                    file_data,
                    analysis_type,
                    config.service_name,
                )
                if analysis_result:
                    api_used = f'{config.get_service_name_display()} / {get_model_name(config)}'
                    break
            
            # 如果都失败，返回错误
            if not analysis_result:
                final_error = error_msg or "所有AI服务均不可用，请检查API配置"
                return {
                    'success': False,
                    'error': final_error,
                    'processing_time': time.time() - start_time
                }
            
            # 3. 将模型 JSON 转换为可展示文本和可管理的结构化记录
            display_result, structured_data, metrics = parse_ai_analysis(
                analysis_result,
                analysis_type,
            )
            processing_time = time.time() - start_time
            return {
                'success': True,
                'result': display_result,
                'raw_result': analysis_result,
                'structured_data': structured_data,
                'metrics': metrics,
                'processing_time': processing_time,
                'api_used': api_used,
                'confidence_score': 0.85,  # 暂时固定置信度
                'file_info': {
                    'name': file_data['filename'],
                    'size': file_data['size'],
                    'type': file_data['type']
                }
            }
            
        except Exception as e:
            logger.error(f"文档分析过程中出错: {e}")
            return {
                'success': False,
                'error': f"文档分析过程中出错: {e}",
                'processing_time': time.time() - start_time
            }

# 创建全局服务实例
ai_service = AIAnalysisService()
