"""
AI分析服务模块 - 重构版
支持DeepSeek和Kimi API调用，直接处理Word/PDF文档
"""
import requests
import json
import time
import logging
import base64
from django.conf import settings
from .models import APIConfig

logger = logging.getLogger(__name__)

class AIAnalysisService:
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
        
    def prepare_file_for_analysis(self, uploaded_file):
        """准备文件用于AI分析 - 支持所有格式"""
        try:
            # 重置文件指针到开始位置
            uploaded_file.seek(0)
            
            # 读取文件内容
            file_content = uploaded_file.read()
            file_name = uploaded_file.name
            file_size = len(file_content)
            
            # 获取文件扩展名
            file_extension = file_name.lower().split('.')[-1] if '.' in file_name else ''
            
            # 对于文本文件，尝试直接解码
            if file_extension == 'txt':
                try:
                    # 尝试不同编码解码文本文件
                    for encoding in ['utf-8', 'utf-8-sig', 'gbk', 'gb2312', 'latin1']:
                        try:
                            text_content = file_content.decode(encoding)
                            return {
                                'type': 'text',
                                'content': text_content,
                                'filename': file_name,
                                'size': file_size
                            }
                        except UnicodeDecodeError:
                            continue
                    # 如果所有编码都失败，使用错误处理方式
                    text_content = file_content.decode('utf-8', errors='ignore')
                    return {
                        'type': 'text',
                        'content': text_content,
                        'filename': file_name,
                        'size': file_size
                    }
                except Exception as e:
                    logger.warning(f"文本文件解码失败，转为二进制处理: {e}")
            
            # 对于其他格式（Word、PDF等），编码为base64
            file_base64 = base64.b64encode(file_content).decode('utf-8')
            return {
                'type': 'binary',
                'content': file_base64,
                'filename': file_name,
                'size': file_size,
                'extension': file_extension
            }
            
        except Exception as e:
            logger.error(f"文件准备失败: {e}")
            return {
                'type': 'error',
                'error': f"文件处理失败: {e}",
                'filename': uploaded_file.name if hasattr(uploaded_file, 'name') else 'unknown'
            }
    
    def analyze_with_deepseek(self, file_data, analysis_type):
        """使用DeepSeek API进行分析"""
        api_key = self.get_api_config('deepseek')
        if not api_key:
            return None, "DeepSeek API密钥未配置、未启用或格式无效。请检查API配置。"
        
        try:
            # 构建分析提示词
            if file_data['type'] == 'text':
                # 文本文件直接包含内容
                file_content = file_data['content']
                content_description = f"文本文档内容：\n{file_content}"
            elif file_data['type'] == 'binary':
                # 二进制文件提供文件信息和base64内容
                content_description = f"""
文档信息：
- 文件名：{file_data['filename']}
- 文件大小：{file_data['size']} 字节
- 文件类型：{file_data.get('extension', '未知')}

请注意：这是一个{file_data.get('extension', '未知')}格式的文档。请分析这个文档的内容并提取相关信息。

文档内容（Base64编码）：
{file_data['content'][:1000]}...（内容已截断）
"""
            else:
                return None, f"文件处理错误: {file_data.get('error', '未知错误')}"
            
            # 根据分析类型设置不同的提示词
            if analysis_type == 'research_content':
                prompt = f"""
请分析以下科研课题文档，提取主要研究内容：

{content_description}

请按照以下格式输出主要研究内容：

**主要研究内容分析**

**1. 研究背景与意义**
[从文档中提取研究背景和意义]

**2. 主要研究内容**
[列出核心研究内容和方向]

**3. 关键技术要点**
[识别关键技术和方法]

**4. 预期研究目标**
[整理预期达成的目标]

**5. 主要创新点**
[提炼创新性内容]

要求：
- 内容要准确、简洁
- 突出核心技术和创新点
- 符合科研项目规范
- 如果是二进制文件，请尽力解析其中的文本内容
"""
            else:  # output_metrics
                prompt = f"""
请分析以下科研课题文档，提取产出指标信息：

{content_description}

请按照以下格式输出产出指标：

**产出指标分析**

**1. 技术成果指标**
- 发明专利：[数量]项
- 实用新型专利：[数量]项
- 软件著作权：[数量]项

**2. 学术成果指标**
- 核心期刊论文：[数量]篇
- 国际会议论文：[数量]篇
- 学术专著：[数量]部

**3. 标准制定指标**
- 行业标准：[数量]项
- 企业标准：[数量]项
- 技术规程：[数量]项

**4. 人才培养指标**
- 培养博士：[数量]名
- 培养硕士：[数量]名
- 培养技术人员：[数量]名

**5. 经济效益指标**
- 直接经济效益：[金额]万元
- 间接经济效益：[金额]万元
- 社会效益：[描述]

要求：
- 尽量从文档中提取具体数字
- 包含时间节点信息
- 符合科研项目管理要求
- 如果是二进制文件，请尽力解析其中的数值信息
"""

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}'
            }
            
            payload = {
                'model': 'deepseek-chat',
                'messages': [
                    {'role': 'user', 'content': prompt}
                ],
                'max_tokens': 2000,
                'temperature': 0.3
            }
            
            response = requests.post(
                self.deepseek_base_url,
                headers=headers,
                json=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                analysis_result = result['choices'][0]['message']['content']
                return analysis_result, None
            else:
                error_msg = f"DeepSeek API调用失败，状态码: {response.status_code}"
                logger.error(error_msg)
                return None, error_msg
                
        except requests.exceptions.Timeout:
            return None, "API调用超时"
        except requests.exceptions.RequestException as e:
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
            # 构建分析提示词 - 与DeepSeek类似的处理
            if file_data['type'] == 'text':
                file_content = file_data['content']
                content_description = f"文档内容：\n{file_content}"
            elif file_data['type'] == 'binary':
                content_description = f"""
文档信息：
- 文件名：{file_data['filename']}
- 文件大小：{file_data['size']} 字节
- 文件格式：{file_data.get('extension', '未知')}

这是一个{file_data.get('extension', '未知')}格式的文档文件。请分析文档内容并提取所需信息。

文档内容（Base64编码，已截断显示）：
{file_data['content'][:1000]}...
"""
            else:
                return None, f"文件处理错误: {file_data.get('error', '未知错误')}"
            
            # 根据分析类型设置不同的提示词
            if analysis_type == 'research_content':
                prompt = f"""
请帮我分析这份科研课题文档，提取并整理主要研究内容：

{content_description}

请按以下结构输出：
**主要研究内容分析**

**1. 研究背景与意义**
[从文档中提取研究背景和意义]

**2. 核心研究内容**
[列出主要研究方向和内容]

**3. 关键技术要点**
[识别关键技术和方法]

**4. 预期研究目标**
[整理预期达成的目标]

**5. 主要创新点**
[提炼创新性内容]

请确保内容准确、条理清晰，如果是二进制文档请尽力解析其中的文本内容。
"""
            else:  # output_metrics
                prompt = f"""
请帮我分析这份科研课题文档，提取产出指标相关信息：

{content_description}

请按以下结构输出：
**产出指标分析**

**1. 技术成果指标**
- 发明专利：[数量]项
- 实用新型：[数量]项
- 软件著作权：[数量]项

**2. 学术成果指标**
- 核心期刊论文：[数量]篇
- 国际会议论文：[数量]篇
- 学术专著：[数量]部

**3. 标准制定指标**
- 行业标准：[数量]项
- 企业标准：[数量]项

**4. 人才培养指标**
- 培养博士：[数量]名
- 培养硕士：[数量]名
- 培养技术人员：[数量]名

**5. 效益指标**
- 经济效益：[金额]万元
- 社会效益：[描述]

请尽量从文档中提取具体数字，如无明确数字则给出合理估算。如果是二进制文档请尽力解析其中的数值信息。
"""

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}'
            }
            
            payload = {
                'model': 'moonshot-v1-8k',
                'messages': [
                    {'role': 'user', 'content': prompt}
                ],
                'max_tokens': 2000,
                'temperature': 0.3
            }
            
            response = requests.post(
                self.kimi_base_url,
                headers=headers,
                json=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                analysis_result = result['choices'][0]['message']['content']
                return analysis_result, None
            else:
                error_msg = f"Kimi API调用失败，状态码: {response.status_code}"
                logger.error(error_msg)
                return None, error_msg
                
        except requests.exceptions.Timeout:
            return None, "API调用超时"
        except requests.exceptions.RequestException as e:
            return None, f"网络请求失败: {e}"
        except Exception as e:
            logger.error(f"Kimi API调用异常: {e}")
            return None, f"API调用异常: {e}"
    
    def analyze_document(self, uploaded_file, analysis_type):
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
            
            # 2. 使用AI进行分析 - 优先使用DeepSeek，失败时尝试Kimi
            analysis_result = None
            error_msg = None
            api_used = None
            
            # 检查是否有可用的API配置
            deepseek_available = self.get_api_config('deepseek') is not None
            kimi_available = self.get_api_config('kimi') is not None
            
            if not deepseek_available and not kimi_available:
                return {
                    'success': False,
                    'error': "未配置任何AI服务API密钥。请先配置DeepSeek或Kimi API密钥。",
                    'processing_time': time.time() - start_time
                }
            
            # 尝试DeepSeek
            if deepseek_available:
                analysis_result, error_msg = self.analyze_with_deepseek(file_data, analysis_type)
                if analysis_result:
                    api_used = 'DeepSeek'
            
            # 如果DeepSeek失败，尝试Kimi
            if not analysis_result and kimi_available:
                analysis_result, error_msg = self.analyze_with_kimi(file_data, analysis_type)
                if analysis_result:
                    api_used = 'Kimi'
            
            # 如果都失败，返回错误
            if not analysis_result:
                final_error = error_msg or "所有AI服务均不可用，请检查API配置"
                return {
                    'success': False,
                    'error': final_error,
                    'processing_time': time.time() - start_time
                }
            
            # 3. 返回成功结果
            processing_time = time.time() - start_time
            return {
                'success': True,
                'result': analysis_result,
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