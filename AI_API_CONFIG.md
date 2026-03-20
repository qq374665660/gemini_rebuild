# AI分析功能配置说明

## 功能介绍

本系统集成了AI文档分析功能，支持自动分析课题申报书和任务书，提取研究内容和产出指标。

## 支持的AI服务

### 1. DeepSeek API
- **优点**: 专业的AI模型，分析质量高
- **获取方式**: 访问 https://platform.deepseek.com/ 注册获取API密钥
- **费用**: 按使用量计费

### 2. Kimi API (Moonshot)
- **优点**: 中文理解能力强，适合中文科研文档
- **获取方式**: 访问 https://platform.moonshot.cn/ 注册获取API密钥  
- **费用**: 按使用量计费

## 配置方法

### 方法1: 直接在代码中配置

编辑 `project_manager/settings.py` 文件：

```python
# AI Analysis API Configuration
DEEPSEEK_API_KEY = 'sk-your-deepseek-api-key-here'  # 填入您的DeepSeek API密钥
KIMI_API_KEY = 'sk-your-kimi-api-key-here'          # 填入您的Kimi API密钥
```

### 方法2: 使用环境变量（推荐）

在系统环境变量中设置：

**Windows:**
```cmd
set DEEPSEEK_API_KEY=sk-your-deepseek-api-key-here
set KIMI_API_KEY=sk-your-kimi-api-key-here
```

**Linux/macOS:**
```bash
export DEEPSEEK_API_KEY=sk-your-deepseek-api-key-here
export KIMI_API_KEY=sk-your-kimi-api-key-here
```

## 文件格式支持

### 当前支持
- **TXT文件**: 直接支持，推荐使用

### 需要额外配置
- **Word文档 (.doc/.docx)**: 需要安装 `python-docx` 库
  ```bash
  pip install python-docx
  ```

- **PDF文档**: 需要安装 `PyPDF2` 或 `pdfplumber` 库
  ```bash
  pip install PyPDF2
  # 或者
  pip install pdfplumber
  ```

## 使用说明

1. **配置API密钥**: 至少配置一个AI服务的API密钥
2. **上传文档**: 在课题详情页面的"研究内容分析"或"产出指标分析"tab中上传文档
3. **等待分析**: 系统会自动调用AI服务进行分析
4. **查看结果**: 分析完成后会显示结果，可以进一步手动编辑
5. **自动同步**: 研究内容分析的结果会自动同步到课题信息的"主要研究内容"字段

## 故障排除

### 常见问题

1. **分析失败: API密钥未配置**
   - 检查settings.py中的API密钥配置
   - 确保API密钥格式正确（通常以'sk-'开头）

2. **分析失败: 网络连接超时**
   - 检查网络连接
   - 如果在内网环境，可能需要配置代理

3. **分析失败: 文件格式不支持**
   - 当前只完整支持TXT文件
   - Word和PDF需要安装额外的Python库

4. **分析失败: API调用限制**
   - 检查API账户余额
   - 确认API调用频率限制

### 备选方案

如果AI分析不可用，系统提供手动编辑功能：
1. 点击"开始编辑"按钮
2. 在文本框中输入分析结果
3. 点击"保存更改"

## 技术说明

- 系统优先使用DeepSeek API，失败时自动尝试Kimi API
- 分析结果包含置信度评分和处理时间
- 所有分析结果都保存在数据库中，支持历史记录查看
- 系统会记录使用的AI服务类型，便于问题诊断

## 隐私说明

- 上传的文档内容会发送到AI服务商进行分析
- 请确保上传的文档不包含敏感信息
- 分析结果仅存储在本地数据库中

## 更新说明

如需支持更多AI服务或文件格式，请联系系统开发团队。