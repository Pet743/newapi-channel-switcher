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

## 部署

```bash
cp .env.example .env
# 填写 NEWAPI_DB_DSN、ADMIN_PASSWORD

docker compose up -d --build
```

本项目的服务器配置使用：

- PostgreSQL：`newapi-rc40-postgres:5432/newapi`
- 日志挂载：`/opt/newapi-rc40/logs`
- 服务端口：`19041`
- 后台地址：`http://43.248.139.184:19041/`

## 后台使用

1. 访问 `http://43.248.139.184:19041/`，输入 `.env` 中的 Basic Auth 用户名和密码。
2. 新建分组，选择渠道并填写优先级，点击“保存渠道配置”。
3. “测试”只验证该渠道的 `gpt-6.1-sol` 请求；“启用并选优”按最高优先级逐个测试，首个成功渠道作为唯一启用渠道。
4. 检测到当前渠道的 503 后立即测试其他候选；定时探测更高优先级渠道（默认60秒），恢复后自动回切。所有候选失败时保留当前状态并记录失败。
5. “暂停”停止接管；“恢复接管前状态”把该分组保存的原始启停状态恢复到 newapi。修改渠道前先暂停，删除前建议先恢复。

## API

- `GET /api/groups`：分组及渠道配置。
- `GET /api/channels`：读取 newapi 渠道（密钥不会返回）。
- `POST /api/groups`：提交 `{"name":"备用线路","newapi_group":"newapi中的分组名称"}` 新建分组。
- `POST /api/groups/{id}/channels`：提交 `{"channels":[{"channel_id":48,"priority":100}]}`。
- `POST /api/groups/{id}/activate`：提交 `{}`，按优先级选优，测试成功后单活启用。
- `POST /api/groups/{id}/restore`：提交 `{}`，恢复快照并暂停。
- `POST /api/test-channel`：提交 `{"channel_id":48}`，手动测试。
- `GET /api/events`：最近自动切换事件。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

## 运行约束

服务只修改后台配置分组中列出的渠道，不会触碰其他渠道；候选测试失败时保持当前状态，所有候选都失败时也保持当前状态。

## 切换边界与运行说明

- 伴生优先级保存在自身SQLite中，不覆盖newapi原有priority。数字越大越优先，相同优先级按ID升序。
- 渠道必须归属所选的单一newapi分组；同一个渠道不能由两个伴生分组共同管理。已有启用渠道需全部纳入，保证分组单活。
- `channels.status`与`abilities.enabled`在同一PostgreSQL事务中修改。newapi内存路由缓存按自身同步周期更新；当前实例约30秒。已经发出的请求不重放，客户端下次请求使用新渠道。
- 简单测试使用渠道自身base_url/key及代理设置，发送chat/completions非流式请求；要求2xx且有正常choices/output。HTTP401、404、429、503、网络错误、HTTP200错误体都不会被判定为成功。当前实现针对OpenAI兼容渠道，不是所有newapi适配器的复刻。
- 首次启动跳过历史日志，新日志轮转从头读取；只消费完整行，迟到的备用渠道503不会覆盖当前渠道。
- 更高优先级探测会产生少量上游调用费用。默认60秒一次，使用RECOVERY_INTERVAL调整。
- 后台独立Basic Auth，ADMIN_PASSWORD必填；数据库密钥、渠道密钥和代理凭证不返回到浏览器。
- 实例只运行一个伴生服务副本；使用线程锁串行切换，持久化配置和原始状态快照。

## 回滚

先对每个已接管的分组点击“恢复接管前状态”，然后停止伴生服务：

```bash
docker compose stop
```

也可在服务器项目目录执行 `sh artifacts/ROLLBACK.sh --deployment`，脚本会通过后台API恢复全部已有快照后停止服务。文件回滚演练：`sh artifacts/ROLLBACK.sh /tmp/core.rollback.py`。
