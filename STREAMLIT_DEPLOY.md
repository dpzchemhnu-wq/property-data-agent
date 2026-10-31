# 固定网址部署（Streamlit Community Cloud）

项目仓库已经准备好：`dpzchemhnu-wq/property-data-agent`。这是最简便的固定网址方案，适合课程展示和手机/电脑访问。

1. 打开 <https://share.streamlit.io/>，使用 GitHub 账号登录。
2. 点击 **New app**。
3. 选择 **Deploy from existing repo**。
4. 填写：
   - Repository：`dpzchemhnu-wq/property-data-agent`
   - Branch：`main`
   - Main file path：`app.py`
5. 打开 **Advanced settings / Secrets**，填入以下内容。将实际 API Key 放在引号中；不要把这段内容提交到 GitHub：

```toml
API_KEY = "在这里粘贴模型服务 API Key"
```

6. 点击 **Deploy**，等待健康检查通过。平台会给出一个固定的 `https://......streamlit.app` 网址。
7. 在 App settings 中将访问权限设为公开（若课程展示需要免登录打开），然后用手机流量和另一台电脑测试。

注意：公开网址不要求网页口令，但任何拿到链接的人都可能调用服务并消耗 API Key 额度。密钥只放在 Streamlit Secrets，不要放在 Python、GitHub、浏览器或截图里。免费计划可能在长时间无访问后休眠，首次打开会慢一些，但网址不会因此改变。

## 部署后验收

- 查询 `查 25℃、常压下水的密度`，确认自然语言单位转换和单项结果。
- 查询 `查 25℃、常压下甲醇与甲苯摩尔比 5/1 的密度`，确认顺序和摩尔分数。
- 点击查询框右侧的“查看详情”，确认结果表、NIST/ThermoML 来源折叠正常。
- 点击下方“结构化查询”，取消若干目标物性，确认只显示勾选项。
- 从手机浏览器打开网址，确认输入框和详情按钮没有横向溢出。
