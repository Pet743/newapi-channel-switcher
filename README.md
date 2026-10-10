# newapi渠道切换

独立的 newapi 伴生服务。它通过 PostgreSQL 读取渠道，通过 newapi 日志捕获 `status_code=503`，并按后台配置的分组和优先级自动切换。每个分组只保持一个渠道启用：触发 503 后，服务从优先级最高的候选开始用 `gpt-6.1-sol` 发起最小测试请求，只有返回 2xx 才关闭当前渠道并启用候选渠道。

## 功能

- 后台自定义分组。
- 每个分组选择渠道 ID，独立设置优先级（数字越大越优先）。
- 查看 newapi 实例中的渠道状态、原分组和地址。
- 手动测试单个渠道，测试模型固定为 `gpt-6.1-sol`，请求内容为 `ping`、`max_tokens=1`。
- 捕获 `record error log ... channelId=... content=status_code=503` 后自动探测和切换。
- PostgreSQL 事务更新，保证同一分组的关闭/启用在一次事务内完成。
- Basic Auth 后台保护，SQLite 保存分组配置和切换事件。

## 部署到 newapi-rc40

```bash
cp .env.example .env
# 填写 NEWAPI_DB_DSN、ADMIN_PASSWORD

docker compose up -d --build
```

本项目的服务器配置使用：

- PostgreSQL：`newapi-rc40-postgres:5432/newapi`
- 日志挂载：`/opt/newapi-rc40/logs`
- 服务端口：`19041`
- Caddy 路径：`https://new.8w8.shop/qudao/`

将 Caddy 的 `new.8w8.shop` 站点在现有 fallback 之前加入：

```caddyfile
redir /qudao /qudao/ 308
handle_path /qudao/* {
    reverse_proxy newapi-channel-switcher:19041
}
```

然后执行 `docker exec newapi-draw-gateway caddy reload --config /etc/caddy/Caddyfile`。

## 后台使用

1. 访问 `https://new.8w8.shop/qudao/`，输入 `.env` 中的 Basic Auth 用户名和密码。
2. 新建分组，选择渠道并填写优先级，点击“保存渠道配置”。
3. “测试”只验证该渠道的 `gpt-6.1-sol` 请求；“按最高优先级探测并启用”会先测试，再执行单活切换。
4. 运行中的 503 会在服务事件表中留下测试、成功或失败记录。

## API

- `GET /api/groups`：分组及渠道配置。
- `GET /api/channels`：读取 newapi 渠道（密钥不会返回）。
- `POST /api/groups`：新建分组。
- `POST /api/groups/{id}/channels`：提交 `{"channels":[{"channel_id":48,"priority":100}]}`。
- `POST /api/groups/{id}/activate`：提交 `{"channel_id":48}`，测试成功后单活启用。
- `POST /api/test-channel`：提交 `{"channel_id":48}`，手动测试。
- `GET /api/events`：最近自动切换事件。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

## 运行约束

服务只修改后台配置分组中列出的渠道，不会触碰其他渠道；候选测试失败时保持当前状态，所有候选都失败时也保持当前状态。
