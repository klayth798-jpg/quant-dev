# 服务器生产部署手册

本文档对应 2026 年 6 月 15 日的生产 Compose。目标拓扑是：

```text
Internet -> Caddy(80/443) -> FastAPI
                            -> PostgreSQL
FastAPI -> Redis -> Worker  -> PostgreSQL
Paper Runner               -> PostgreSQL
Backup Service             -> PostgreSQL dump
```

PostgreSQL 和 Redis 只在 Docker 内网开放，不映射服务器端口。

## 1. 准备服务器

建议先使用一台 Ubuntu 24.04 LTS 云服务器：

- 最低 2 核、4 GB 内存、80 GB SSD。
- 安全组仅开放 `22`、`80`、`443`。
- 域名 A 记录指向服务器公网 IP。
- 安装 Docker Engine 和 Compose Plugin。

验证：

```bash
docker --version
docker compose version
```

## 2. 创建生产配置

在服务器克隆项目后：

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

生成三个不同的随机值：

```bash
openssl rand -hex 32
openssl rand -hex 32
openssl rand -hex 32
```

分别填入 `POSTGRES_PASSWORD`、`REDIS_PASSWORD` 和
`QUANTDEV_ADMIN_API_KEY`。密码建议只用字母数字，避免 DSN 转义问题。再填写域名和
Tinyshare/Tushare Token。初次部署保持：

```dotenv
QUANTDEV_BROKER_MODE=disabled
QUANTDEV_LIVE_TRADING_ENABLED=false
```

## 3. 搬迁现有 SQLite 数据

先停止本地写入并把 `data/quantdev.db` 上传到服务器项目的 `data/`。启动数据库：

```bash
docker compose --env-file .env.production -f compose.prod.yml up -d postgres redis
```

目标 PostgreSQL 必须是空库。执行批量 `COPY`：

```bash
docker compose --env-file .env.production -f compose.prod.yml \
  run --rm -v "$PWD/data:/source:ro" migrate \
  python -m quantdev.cli migrate-sqlite-to-postgres \
  --source /source/quantdev.db
```

命令会逐表打印复制行数。迁移期间不要启动 API 或 Worker；如果不需要旧数据，可跳过。

## 4. 启动生产栈

```bash
docker compose --env-file .env.production -f compose.prod.yml \
  --profile ops up -d --build
```

`migrate` 容器必须成功退出后，API 和 Worker 才会启动。查看状态：

```bash
docker compose --env-file .env.production -f compose.prod.yml ps
docker compose --env-file .env.production -f compose.prod.yml logs -f app worker
```

验证：

```bash
curl https://你的域名/api/health/live
curl https://你的域名/api/health/ready
```

`ready` 必须同时返回数据库和队列健康。Caddy 在域名解析正确且 80/443 可访问时自动
申请 HTTPS 证书。

## 5. 日常运维

更新代码：

```bash
git pull
docker compose --env-file .env.production -f compose.prod.yml \
  --profile ops up -d --build
```

扩展两个 Worker：

```bash
docker compose --env-file .env.production -f compose.prod.yml \
  up -d --scale worker=2
```

查看失败任务：

```bash
curl "https://你的域名/api/tasks?status=FAILED"
```

启动模拟盘策略运行器：

```bash
docker compose --env-file .env.production -f compose.prod.yml \
  --profile paper-live up -d paper-runner
```

真实券商接入前不要开启 `live`。

## 6. 备份与恢复

`ops` profile 每天生成 PostgreSQL custom dump，默认保留 14 天，存放在
`postgres-backups` Docker volume。检查：

```bash
docker compose --env-file .env.production -f compose.prod.yml \
  exec postgres-backup ls -lh /backups
```

定期把备份复制到另一台机器或对象存储：

```bash
container_id="$(docker compose --env-file .env.production \
  -f compose.prod.yml ps -q postgres-backup)"
docker cp "$container_id:/backups/." ./offsite-backups/
```

恢复前先停止 `app`、`worker` 和 `paper-runner`，在新空库执行 `pg_restore`，然后重新
运行迁移和健康检查。至少每月做一次恢复演练；只有实际恢复成功的备份才算可用备份。

## 7. 上线检查

1. `/api/health/ready` 连续正常。
2. PostgreSQL 和 Redis 没有公网端口。
3. `.env.production` 权限为 `600` 且未提交 Git。
4. Worker 能完成一次因子任务和一次回测任务。
5. SQLite 迁移行数与源库关键表统计一致。
6. 备份已复制到服务器之外并完成恢复演练。
7. Broker 仍为 `disabled`，直至模拟实盘阶段验收完成。
