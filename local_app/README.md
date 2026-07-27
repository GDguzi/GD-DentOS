# 本地牙科门诊系统（应用包）

## 启动

```bash
python3 -m local_app.run_local
```

默认监听 `0.0.0.0:8765`（局域网联机，iPad/手机可签字）。本机浏览器打开：

```text
http://127.0.0.1:8765
```

局域网设备（同 WiFi）打开 `http://<本机局域网IP>:8765`，首次需输访问口令（见启动横幅 / `data/.lan_password`，可用 `DENTAL_ACCESS_PASSWORD` 自设）。本机免口令。强制只本机：`DENTAL_LOCAL_ONLY=1`。

更多部署形态与环境变量见仓库根 `README.md`。

## 隐私边界

- 不上传患者数据。
- 不连接公网服务。
- 本地数据库、备份和日志不提交 Git（`data/` 已 gitignore）。
