# 物性数据 Agent V3

## 文件

- `app.py`：Streamlit 对话界面和固定模型服务的受限工具调用循环。
- `tools.py`：通用组分识别、输入校验、参数选择、物性计算、参考源查询和状态码处理。
- `requirements.txt`：运行依赖。
- `.env.example`：部署主机的密钥变量模板；固定模型服务不可在网页修改。

## 启动

需要 Python 3.10 或更高版本。此机器已将 Python 3.12.10 安装在 `D:\Process Analysis\python312`，并将应用运行环境、依赖与缓存放在 `D:\Process Analysis` 下。

```powershell
cd outputs
python -m venv .venv
.
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

模型接口和模型名由后端固定；API Key 从本机 Streamlit secrets 或进程环境读取。密钥不要写入源码、`.env.example`、公开仓库或共享给其他电脑的项目包。部署到另一台机器时，应在那台服务器单独设置密钥。

如项目使用非英文组分别名，优先在 `component_aliases.json` 中扩充名称到 CAS RN 的映射；其余名称仍会尝试通过 `chemicals` 索引通用识别。结构化表单支持英文逗号、中文逗号、顿号、分号和换行分隔。

## 参数台账

将台账存为 UTF-8 JSON，并设置 `BINARY_PARAMETER_LEDGER`。CAS 是稳定键，方向参数随 `CAS1`、`CAS2` 顺序定义。每条二元对记录都必须带可追溯的 `source_id`，否则不会被视为已检测到的文献参数。NRTL 参数是无量纲 `tau = tau_a + tau_b/T`，`alpha_ij` 为非随机参数；UNIQUAC 参数 `u_ij_J_mol`、`u_ji_J_mol` 单位为 J/mol，纯组分结构参数 `r`、`q` 放在 `components` 中。

```json
{
  "components": {
    "64-17-5": {"r": 2.1055, "q": 1.972},
    "67-56-1": {"r": 1.4311, "q": 1.432}
  },
  "pairs": [
    {
      "model": "NRTL",
      "CAS1": "64-17-5",
      "CAS2": "67-56-1",
      "tau_a_ij": 0.0,
      "tau_a_ji": 0.0,
      "tau_b_ij": 0.0,
      "tau_b_ji": 0.0,
      "alpha_ij": 0.3,
      "source_id": "literature:replace-with-verified-citation"
    },
    {
      "model": "UNIQUAC",
      "CAS1": "64-17-5",
      "CAS2": "67-56-1",
      "u_ij_J_mol": 0.0,
      "u_ji_J_mol": 0.0,
      "source_id": "literature:replace-with-verified-citation"
    }
  ]
}
```

上面数字仅用于说明 JSON 字段结构，不是推荐或有效的文献参数。只有当台账完整覆盖混合物的全部二元对时，才选用 NRTL 或 UNIQUAC；否则尝试 UNIFAC。上线前应由团队用经过审核的文献数据替换示例数值，并为参数记录增加出处、温度范围、拟合目标与版本信息。

## 外部参考数据边界

纯组分通过 NIST WebBook Fluid CGI 查询给定温度/压力下的状态点。混合体系直接连接 NIST/TRC 官方 ThermoML：搜索端点 `https://trc.nist.gov/ThermoML-API/objects` 用于检索候选文献元数据；随后按 DOI 从 `https://trc.nist.gov/ThermoML/{DOI}.json` 读取实际实验数据点，并仅在组分和液相摩尔分数匹配、温度及压力位于明确的实验取整容差内时比较。该 JSON 路径与 NIST 官方站点提供的搜索说明及 DOI 对象链接一致。官方归档说明：完整数据集也可从 [NIST ThermoML Data Archive](https://data.nist.gov/od/id/mds2-2422) 下载约 189 MB 的 `.tgz`。当前集成采用按需下载单篇 JSON，避免每台安装都必须先拉取整个归档。

`thermo` 物性计算不需要模型密钥；NIST 参考数据也不要求单独密钥，但查询时需要互联网连接。自然语言输入默认交给固定模型服务转换为工具参数（摄氏/华氏温度、压力单位和配比均转换为 K、Pa 和摩尔分数）；模型不可用时才尝试保守的本地解析。外部源不可达或超时会明确报告，不以模型值替代参考数据。

ThermoML Search API 本身仅供检索元数据/摘要，不包含数值数据点；因此代码不能只依赖搜索 API 就声称已做数值比较。当前对搜索排序前 20 条候选进行元数据筛选，并按 DOI 读取候选完整 JSON；受官方覆盖范围（公开站点标注截至 2019 年）及检索排序影响，未命中时会明确标注“前 20 条候选未找到直接状态点”，不会静默跳过或插值/外推。泡点暂不从文献数据猜算；焓只有参考态明确一致才计算偏差；来源不确定度按 ThermoML 扩展不确定度显示。WebBook 和 ThermoML 各自单独成列，相对偏差按来源分别计算。

## 重要工程边界

- 输入温度范围为 273.15–450 K，压力范围为 1–100,000,000 Pa；范围外返回 `out_of_domain`。
- 组分与摩尔分数按输入顺序传递；分数和在 1.0 的 `1e-6` 容差内校验，不自动归一化。
- 本地规则解析是默认流程；无法识别的完整自然语言请求会交给固定语言模型辅助解析。界面不提供模型/接口选择；密钥由运行主机安全配置。界面仅将后端返回的数值作为物性结果展示。
- `thermo` 给出的焓使用其默认基准，必须与下游模型对齐；相态枚举为 `single_liquid_assumed`，未做严格 LLE 稳定性校核。
- 生产使用前应以目标组分、目标相态和目标压力范围进行回归验证，并记录 `thermo` 数据库/版本和文献参数出处。

## 调试与多人运行

- 模型服务请求仅用于自然语言转工具参数；401、503 等连接错误折叠显示，详情中不会回显密钥。模型地址和名称在后端固定。
- 工具计算成功后直接返回卡片，不再额外请求 LLM 做二次总结，避免模型的第二次 HTTP 错误遮住已经计算出的结果。
- 自然语言默认由固定模型转换为工具参数，中文组分来自包含 200+ 别名的可编辑名录，英文名优先用 `chemicals` 本地索引，未命中时再查 PubChem 并验证 CAS。明确比例按总份数归一化（如 `甲醇与甲苯摩尔比 5/1` 转成 `[0.833333, 0.166667]`）；明确质量比不会伪装成摩尔比。对于名称有歧义或不能核实唯一 CAS 的物质，会要求用户明确名称，不猜测替代组分。仅返回用户明确点名或结构化勾选的目标物性。
- 单机运行绑定地址默认为 `127.0.0.1`。在团队 LAN 共享时，需由管理员按本机网络策略开放 TCP 8501，且仅在可信网络使用。
- 其他 Windows 电脑可安装 Python 3.12 后，在项目目录创建虚拟环境并执行 `pip install -r requirements.txt`。若团队电脑装有 Docker，运行 `docker compose up --build`，通过 `http://localhost:8501` 使用；同一 LAN 的其他电脑使用主机内网 IP 和 `:8501`。API Key 不要提交进共享代码仓库。
- 公网部署模板为 `render.yaml`，容器会读取平台分配的 `PORT`。将代码放入私有或受控 GitHub 仓库后，在 Render 创建 Web Service 并选择该 Blueprint；在平台的环境变量面板私密填写 `API_KEY`。部署完成后，平台会分配可供电脑和手机访问的 HTTPS 地址。公开服务可能消耗模型账户额度，正式分享前应设置访问控制或预算限制。
- 本机若要临时给同一可信 LAN 的同学演示，可运行 `D:\Process Analysis\Start Property Agent LAN.bat`，然后将本机内网 IP 和 `:8501` 发给对方；不要在不可信公共网络直接暴露该端口。

## 本机网页应用

在此机器上双击 `D:\Process Analysis\Start Property Agent.bat` 即可启动，浏览器地址为 `http://127.0.0.1:8501`。自然语言输入由后端固定的服务和模型转换，网页不显示模型选择；模型不可用时才回退到本地解析。API Key 仅保存在本机 `.streamlit\secrets.toml`（已排除在项目文件之外）或服务进程环境中。手动结构化查询收在折叠区；结果表将单位放在物性表头，来源对照分别折叠显示。

本机 Python、应用虚拟环境、安装器、临时文件和 pip 缓存分别位于 `D:\Process Analysis\python312`、`D:\Process Analysis\property-agent\.venv`、`D:\Process Analysis\installers`、`D:\Process Analysis\temp` 和 `D:\Process Analysis\package-cache`。
