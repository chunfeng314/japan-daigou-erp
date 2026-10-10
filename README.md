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
  permissions.py    订单可见性（销售只看自己 / 采购看待采购+经手 / 上海仓按单号查 / 管理员全部）
  order_service.py  订单号生成（并发安全）、建单、商品需求历史累积
  transition_service.py ★ 状态推进通用机制 + 环节表单字段声明 + 异常追踪
  storage.py        ★ 存储抽象：local（本机盘）/ cos（腾讯云直传）
  security.py       口令哈希（stdlib PBKDF2）+ 签名会话 cookie
  deps.py           当前登录人 / 角色守卫
  routers/          auth(登录) · home(首页待办) · orders(列表/录单/详情/编辑)
                    · stages(状态推进 + 异常) · uploads(上传与受权读取) · users(账号管理)
  templates/        base / login / home / order_list / order_new / order_detail / users / forbidden
  static/           style.css · orders.js（历史下拉 + 截图上传）· htmx.min.js
scripts/init_db.py  建表 + 首次创建管理员
tests/              状态机 · 认证权限 · 订单与上传 · 状态推进与异常（共 130 项）
```

### 截图存储怎么切到 COS

默认 `ERP_STORAGE=local`（存本机 `data/uploads/`，开发够用）。上线切 COS：

1. `.env` 里改 `ERP_STORAGE=cos` 并填 `COS_SECRET_ID/KEY/REGION/BUCKET`；
2. 装 SDK：`.venv/bin/pip install cos-python-sdk-v5`；
3. 在 COS 控制台给存储桶配 **CORS**（允许来源=本站域名，方法=PUT/GET/HEAD）——
   否则浏览器直传会被拦。

业务代码不用改：前端会先问 `/api/uploads/sign` 拿落点，`cos` 模式下直接 `PUT` 到预签名地址。

### 进度

- **D1 已完成**：数据模型 · 状态机 · 登录/账号管理
- **D2~3 已完成**：销售录单（表单 + 商品需求历史复用 + 截图上传）· 订单列表（筛选/分页/权限/异常角标）· 订单详情 · 编辑
- **D4~D6 已完成**：**全链路状态推进**（采购 → 收货 → 带货 → 发货 → 结算）+ 取消 / 回退 + **异常追踪**（少货/错货）
- 后续：D7 查询与首页待办收口 → D8~10 手机端适配 + 部署东京 + 实测国内访问

### 状态是怎么推进的

只有一个通用端点 `POST /orders/{id}/transition`，目标状态写在表单的 `to_status` 里：

```
校验（问 state_machine：谁能推、必填什么）
  → 落环节表（purchases / sh_receives / jp_shipments / jp_deliveries / settlements）
  → 改主状态
  → 写 status_logs
```

环节的表单字段声明在 `app/transition_service.py` 的 `STAGE_FIELDS`；
**必填项不在这里写死**，渲染时从状态机的 `required_fields` 取，避免两处规则漂移
（有测试专门盯这条：`test_stage_fields_match_required_from_state_machine`）。

以后加环节 = 改 `state_machine` + 在 `STAGE_FIELDS` 补字段，**不用加路由**。

> ⚠️ **COS 直传路径已实现但尚未用真实凭据验证过**（`local` 模式已真机验证）。


