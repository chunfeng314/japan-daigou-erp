# 日代 ERP 上线手册

> 目标环境：东京轻量云（2C4G / 60G SSD）+ 域名 + 腾讯云 COS。
> 全程约 1~2 小时，其中「第 0 步」和「HTTPS 签发」最容易出意外，先做这两步。

---

## 0. 先做这一件事：实测国内访问东京的延迟

文档 §5 把这条标成「**部署第一件事**」，因为**如果不通畅，整个架构要改**。
请在**合肥这台机器上**执行（不是在服务器上）：

```bash
# ① 基础连通性：RTT 与丢包
ping -c 20 <服务器IP>

# ② HTTP 层面的分段耗时（域名解析 / TCP / TLS / 首字节 / 总计）
curl -o /dev/null -s -w \
  'DNS %{time_namelookup}s | TCP %{time_connect}s | TLS %{time_appconnect}s | 首字节 %{time_starttransfer}s | 合计 %{time_total}s\n' \
  https://erp.example.com/healthz

# ③ 上传吞吐（截图可能几 MB 一张）—— 用同一台服务器测同一条线路
dd if=/dev/urandom of=/tmp/5mb.bin bs=1m count=5
time scp /tmp/5mb.bin erp@<服务器IP>:/tmp/
```

**怎么判读**

| 结果 | 结论 |
|---|---|
| RTT < 100ms、5MB 上传 < 30s | 正常，按本手册继续 |
| RTT 100~200ms | 页面会略慢但可用；确认 COS 走的是东京接入点 |
| 上传 > 60s 或大量丢包 | **先别上线**，回来找我调整方案 |

> **一个重要的架构缓冲**：截图是**浏览器直传 COS**，不经东京服务器中转。
> 所以「国内上传慢」这个最要命的问题在设计上已经规避了 ——
> 延迟主要影响**页面打开速度**（HTML/JS/CSS 走服务器），而页面只有几十 KB。
> 真正要盯的是 RTT 和丢包，不是上传带宽。

---

## 1. 服务器初始化

```bash
# 用 root 登录一次，做完这些就不再需要 root
apt update && apt upgrade -y
apt install -y python3-venv nginx-common git curl unzip

# 建专用账号（不用 root 跑应用）
adduser --system --group --home /srv/ri-ben-erp --shell /usr/sbin/nologin erp

# 时区：业务在国内、服务器在东京 —— 设成东八区，
# 这样日志里的时间和大家的直觉一致，备份定时器也照国内的"凌晨"走
timedatectl set-timezone Asia/Shanghai
timedatectl   # 确认 Time zone: Asia/Shanghai (CST, +0800)

mkdir -p /srv/ri-ben-erp/{data,backups}
chown -R erp:erp /srv/ri-ben-erp
```

**防火墙**：只放 22 / 80 / 443。**8000 端口不要对公网开** —— 应用只监听回环。

```bash
ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw enable
ufw status
```

---

## 2. 部署代码

```bash
su -s /bin/bash erp
cd /srv/ri-ben-erp
git clone git@github.com:chunfeng314/japan-daigou-erp.git .   # 或 git clone https://...
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 用 COS 存储时还要装 SDK（详见第 4 步）
exit
```

> ⚠️ 服务器上 `git pull` 走 HTTPS 时，如果用的是**只有读权限的 token** 会 push 失败。
> 部署机只需要读，HTTPS 或 SSH 都行；要能推就配 SSH key。

---

## 3. 配置

```bash
su -s /bin/bash erp
cd /srv/ri-ben-erp
cp deploy/env.production.example .env
# 生成一个随机密钥填进 ERP_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"
nano .env      # 逐项填：SECRET_KEY / ERP_COOKIE_SECURE=1 / COS_* / ERP_LLM_API_KEY
chmod 600 .env

# 建表 + 建管理员（幂等；没配 ADMIN_PASSWORD 会随机生成并打印，记得存下来）
.venv/bin/python -m scripts.init_db
```

---

## 4. 存储与模型（按需）

**COS**：`.env` 里 `ERP_STORAGE=cos` 并填四个 `COS_*`，然后装 SDK：

```bash
.venv/bin/pip install cos-python-sdk-v5
```

**还要去 COS 控制台配 CORS**，否则浏览器直传会被拦（这是最容易漏的一步）：

| 配置项 | 值 |
|---|---|
| 允许来源 Origin | `https://erp.example.com` |
| 允许方法 | `PUT, GET, HEAD` |
| 允许头部 | `*` |
| 暴露头部 | `ETag` |

**AI 助手**：填 `ERP_LLM_BASE_URL / ERP_LLM_API_KEY / ERP_LLM_MODEL`（推荐 `deepseek-flash`，
它**支持读图**，截图预填要用它）。不填则自动降级为规则模式，其余功能不受影响。

---

## 5. 托管进程（systemd）

```bash
cp /srv/ri-ben-erp/deploy/ri-ben-erp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ri-ben-erp
systemctl status ri-ben-erp --no-pager
curl -s http://127.0.0.1:8000/healthz    # 应返回 ok 1.1.0
```

支持两种做法：也可以 `ln -s` 到 `/etc/systemd/system`，这样以后 `git pull` 就能直接更新 unit 文件。

**看日志**：`journalctl -u ri-ben-erp -f`

---

## 6. 域名与 HTTPS（Caddy）

```bash
# 装 Caddy（官方源，比 apt 自带的版本新）
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

# 域名已解析到本机后：
cp /srv/ri-ben-erp/deploy/Caddyfile /etc/caddy/Caddyfile
nano /etc/caddy/Caddyfile          # 把 erp.example.com 改成真实域名
systemctl reload caddy
journalctl -u caddy -n 50 --no-pager   # 看证书申请结果
curl -I https://erp.example.com        # 应 200/303 且有有效证书
```

**证书申请卡住的常见原因**：域名 A 记录没生效（`dig +short erp.example.com` 对一下）、
80 端口没通（Let's Encrypt 要回调验证）、或云厂商安全组没放 80。

---

## 7. 每日备份

```bash
cp /srv/ri-ben-erp/deploy/ri-ben-erp-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ri-ben-erp-backup.timer

# 立刻手动跑一次，确认能出文件
systemctl start ri-ben-erp-backup.service
journalctl -u ri-ben-erp-backup -n 20 --no-pager
ls -lh /srv/ri-ben-erp/backups/
systemctl list-timers ri-ben-erp-backup.timer    # 确认下次触发时间
```

**备份覆盖什么**：SQLite 全库（用官方在线备份 API，兼容 WAL）+ 截图清单；
本地存储模式下额外打包 `data/uploads`。

> **注意**：`cos` 模式下图**只存 COS**，不在本地备份里 —— 所以 COS 的版本控制/生命周期规则
> 也要配一份，别把唯一副本放在一个地方。这一点需要在 COS 控制台人工确认。

---

## 8. 上线自检 + 冒烟

```bash
cd /srv/ri-ben-erp
.venv/bin/python -m scripts.preflight      # 全绿才算过；有 ❌ 先别上线
```

然后**在合肥这台机器上**跑一遍真实路径：

```bash
# 登录页能开
curl -s -o /dev/null -w '%{http_code}\n' https://erp.example.com/login

# 用管理员账号在浏览器里走一遍：登录 → 录一单（含截图）→ 列表 → 详情 → 日报 → 助手
```

---

## 9. 交付给老板

1. **账号**：给每个角色建号（销售/采购/上海仓），初始密码让他们自己改；
   管理员账号**别共用**，一人一号才有流转记录可查。
2. **讲清楚三件事**：日报每天看（哪里卡住了）、录单可以传截图自动填、
   助手能问"XX 单到哪一步了"。
3. **要跟老板确认的**（这几项我做不了）：
   - **卡单阈值**（`ERP_SLA_DAYS`）：现在默认值是开发时拍的，要按实际节奏改；
   - 文档 §7 的三项遗留：回退规则、「已发货→待结算」谁推进、异常是否阻塞完成；
   - 二期 AI 管家的报价口径与模型费用实充方式。

---

## 10. 日常维护

```bash
# 更新代码
cd /srv/ri-ben-erp && git pull && .venv/bin/pip install -r requirements.txt
systemctl restart ri-ben-erp

# 看备份有没有在跑
ls -lht /srv/ri-ben-erp/backups/ | head
systemctl list-timers ri-ben-erp-backup.timer

# 恢复演练（务必在测试机上先练一遍，别等真出事才第一次用）
cp /srv/ri-ben-erp/backups/erp-20261010-0330.sqlite3 /tmp/restore-test.sqlite3
ERP_DB_PATH=/tmp/restore-test.sqlite3 .venv/bin/python -m scripts.preflight
```

---

## ⚠️ 本手册中未经真机验证的部分

写这份手册时手上**没有东京服务器、没有域名、也没有 COS 凭据**，所以下面这些
**只是按官方文档写的，没有实跑过**——上线时以实际报错为准，我随时可以一起排查：

| 项 | 状态 |
|---|---|
| systemd unit / timer | 语法按官方规范写，**未在真实 systemd 上加载过** |
| Caddyfile | **未跑过 `caddy validate`**（本机没装 caddy） |
| HTTPS 证书签发 | 未验证（需域名 + 公网 80 端口） |
| 国内→东京延迟 | **未测**（这正是第 0 步要做的） |
| COS 直传 + CORS | 未验证（需凭据；代码侧已实现） |
| `cos-python-sdk-v5` 上传备份 | 未验证（需凭据） |

**已经真机验证过的**（本机 macOS 上实跑）：`scripts/backup_db.py` 的备份/清单/轮转逻辑、
`scripts/preflight.py` 的全部检查项、以及应用本身的全部功能与 261 项自动化测试。
