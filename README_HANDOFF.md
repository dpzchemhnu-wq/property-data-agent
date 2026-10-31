# 物性数据 Agent：接手与固定网址部署说明

本目录是一份可运行的 Streamlit 项目源码包。请接手的 AI 先通读本文件和 `README.md`，再检查代码；不要假设公网固定网址已经部署完成。

## 目标与当前状态

- 目标产品是可由电脑、手机浏览器打开的化工物性查询网页。
- 本地网页已实现居中自然语言输入、单独的结构化查询入口、查询结果详情弹出面板、参考来源折叠、窄屏布局。
- 查询只显示用户明确请求的物性。LLM 负责将自然语言转成工具参数；所有物性数值由 Python 后端校验和计算。
- 当前源代码没有访问口令。用户要求正式演示站点可直接点击进入。
- 之前的 Cloudflare Quick Tunnel 是临时入口，不等同固定部署；电脑关机、进程退出或隧道重启后可能失效。请不要把临时链接写成长期网址。
- 要生成长期稳定网址，必须由项目所有者登录并授权一个托管平台/代码托管账户。项目尚未绑定这些账户；需要在用户确认/登录后部署并把实际 URL 返回。

## 部署建议

优先方案：将本项目连接到 Render（项目含 `render.yaml` 和 `Dockerfile`），或 Streamlit Community Cloud。任一方案均需项目所有者的 GitHub/托管平台账户。部署时：

1. 创建私有代码仓库（若课程展示需要公开源码，应先征得所有者同意）。提交本包中的项目文件，不要提交任何本机配置、密钥或运行环境。
2. 在托管平台新建 Web Service/Streamlit App 并连接仓库。Render 可按 `render.yaml` 读取 Docker 配置；Streamlit Cloud 的入口文件为 `app.py`。
3. 在托管平台的服务端 Secrets/Environment Variables 中设置 `API_KEY`。不要把它写入 Python、前端、README、Dockerfile、公开仓库或构建日志。
4. 设置好后部署并等待健康检查通过：`/_stcore/health` 应返回 `ok`。验证自然语言、结构化查询、移动端布局、参考源错误显示和模型工具调用。
5. 将托管平台分配的稳定网址返回给项目所有者；同时说明免费计划可能休眠、额度/配额及平台限制。

严禁要求用户把 API Key 粘贴进公开仓库或浏览器代码。用户希望免口令公开访问；这意味着任何拿到网址的人都可能调用模型并消耗其 API 额度。部署前应提醒用户设置模型服务的余额/速率上限；若平台支持，应设置服务器端速率限制，但不得擅自重新加网页口令。

## 配置

- 固定模型地址：`https://api.bblabu.ai/v1`
- 固定模型名：`deepseek-v4.1-flash`
- 模型密钥只通过 `API_KEY` 环境变量或托管平台 Secret 注入。
- `BINARY_PARAMETER_LEDGER` 是可选参数台账路径。没有经过验证的团队文献参数时，不要伪造 NRTL/UNIQUAC 记录。
- ThermoML/NIST 是运行时外部数据源；网络不可达或未覆盖时必须如实显示，不得用模型编造参考值。

## 文件说明

- `app.py`：Streamlit 网页、自然语言交互、结构化查询、结果详情面板。
- `llm_client.py`：OpenAI Chat Completions 兼容客户端和受限 Tool Calling 循环。
- `tools.py`：组分识别、校验、thermo 计算、NIST WebBook/ThermoML 查询和来源对比。
- `local_parser.py`：LLM 服务不可用时的保守本地解析与单位转换。
- `component_aliases.json`：可编辑的中英文别名/CAS 映射。
- `test_tools.py`：离线回归测试。
- `requirements.txt`、`Dockerfile`、`docker-compose.yml`、`render.yaml`：依赖与部署配置。
- `README.md`：更完整的模型、热力学、数据来源和工程边界说明。

包内特意不包含 `.streamlit/secrets.toml`、`.env`、API Key、虚拟环境、pip 缓存或运行日志。请勿尝试从提交历史或旧聊天记录复制密钥；由所有者在新平台单独设置。

## 本地验证

Python 3.12 环境中：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m unittest -q
streamlit run app.py
```

Docker 环境中：先通过安全环境变量/本地未提交的 `.env` 配置 `API_KEY`，再运行：

```powershell
docker compose up --build
```

运行测试时不应需要有效模型密钥；真实模型验证需要用户自己的服务端 API_KEY。当前验收基线为 `python -m unittest -q` 全部通过。

## 接手 AI 的首要任务

先审阅整个项目和本说明，然后在不暴露密钥的前提下协助项目所有者完成托管账户授权、部署与线上回归。若没有账号凭据，不要尝试绕过登录、创建冒名账号或虚构完成状态；应让所有者自己在浏览器登录/授权，再继续部署。
