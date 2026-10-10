# 日代 ERP

中国代购发日本业务的订单管理系统（第一版）+ 关联的 Mercari 选品工具。

| 文档 | 内容 |
|---|---|
| `ERP开发文档-第一版.md` | **主文档**（v1.1）：需求边界 / 状态机 / 权限矩阵 / 数据库设计 / 模块清单 / 技术选型 / 排期 / 合同条款要点 / 已确认事项 / 二期 AI 管家展望 |
| `Mercari选品工具-方案概要.md` | 关联项目：Mercari 选品 + 半自动上架（同行店铺监控 / 日中搜索词 / 利润试算 / 上架草稿），含技术验证状态与模块拆解 |

**状态：§7 五项已确认锁定（2026-10-10，v1.1），可开工。** 锁定结论：

1. 一单**只装一种商品**（多商品拆多单）
2. 带货**按订单记**（不建航班实体）
3. **"已发货给买家"进系统** → 新增「已发货」状态 + `jp_deliveries` 表
4. 少货/错货**单独追踪** → 新增 `order_exceptions` 表 + 异常追踪模块
5. 旧系统历史数据**不迁移**，新系统只管新单

遗留微确认（不阻塞开工）：回退规则、「已发货→待结算」推进人、异常是否阻塞完成。

---

## 本地运行

技术栈：FastAPI + SQLite(WAL) + Jinja2 服务端渲染（无前端构建链）。

```bash
# 1) 装依赖（项目内虚拟环境）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 生成配置（首次）
cp .env.example .env      # 改成你自己的 ERP_SECRET_KEY 与 ADMIN_PASSWORD

# 3) 建表 + 创建管理员（幂等；没配 ADMIN_PASSWORD 会随机生成并打印）
.venv/bin/python -m scripts.init_db

# 4) 启动
.venv/bin/uvicorn app.main:app --reload --port 8000
# 打开 http://127.0.0.1:8000
```

跑测试：

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

### 代码结构

```
app/
  config.py         集中配置（全部可用环境变量覆盖）
  db.py             引擎/会话；SQLite WAL + 外键约束
  models.py         11 张表（对应文档 §3）
  state_machine.py  ★ 订单状态机（唯一事实来源：谁能推、推之前必须填什么）
  security.py       口令哈希（stdlib PBKDF2）+ 签名会话 cookie
  deps.py           当前登录人 / 角色守卫
  routers/          auth(登录) · home(首页待办) · users(账号管理)
  templates/        base / login / home / users / forbidden
  static/           style.css · htmx.min.js（本地引入，不依赖 CDN）
scripts/init_db.py  建表 + 首次创建管理员
tests/              状态机单测 + 登录/权限集成测试
```

D1 已交付：**数据模型 · 状态机 · 登录与账号管理**。订单录入、采购、收货、带货、发货、结算、异常追踪按排期逐步上线。

