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
  sla.py            卡单（超时）检测：各状态合理停留天数 + 停留/超时计算
  timeutil.py       ★ 时间口径：库里存 naive UTC，展示统一转配置时区
  todo_service.py   按角色算待办（首页与 AI 助手**共用同一份**，避免两个数字打架）
  briefing.py       ★ 每日简报：一句话结论 + 卡单/待办/异常明细 + 可复制纯文本
  order_service.py  订单号生成（并发安全）、建单、商品需求历史累积
  agent/            ★ AI 助手（二期）
    tools.py          工具层 —— **权限过滤在这里执行**，模型绕不过去
    rules.py          未配模型凭据时的规则模式（文档也说这类问题「规则引擎即可」）
    extract.py        ★ 截图 → 表单字段（多模态提取，P3）
    llm.py            OpenAI 兼容客户端 + 可脚本化的测试替身
    service.py        编排：提问 → 工具调用 → 有依据的回答
    answer.py         回答结构（含 citations 与工具调用轨迹）
  transition_service.py ★ 状态推进通用机制 + 环节表单字段声明 + 异常追踪
  storage.py        ★ 存储抽象：local（本机盘）/ cos（腾讯云直传）
  security.py       口令哈希（stdlib PBKDF2）+ 签名会话 cookie
  deps.py           当前登录人 / 角色守卫
  routers/          auth(登录) · home(首页待办) · orders(列表/录单/详情/编辑)
                    · stages(状态推进 + 异常) · uploads(上传与受权读取) · users(账号管理)
  templates/        base / login / home / order_list / order_new / order_detail / users / forbidden
  static/           style.css · orders.js（历史下拉 + 截图上传）· htmx.min.js
scripts/init_db.py  建表 + 首次创建管理员
scripts/backup_db.py  每日备份：SQLite 一致性快照 + 截图清单 + 轮转（本地存储时连图片一起打包）
scripts/preflight.py  上线自检：把「配置错了但看着正常」的问题提前揪出来
scripts/local_rehearsal.py ★ 本地全流程彩排：不用服务器，走一遍真实业务链路
deploy/              部署材料：systemd unit + timer · Caddyfile · 生产 .env 模板 · **DEPLOY.md 上线手册**
tests/               状态机 · 认证权限 · 订单与上传 · 状态推进与异常 · 定时任务 · 助手 · 运维脚本（共 286 项）
```

### 上线前：本地全流程彩排（不用服务器）

```bash
.venv/bin/python scripts/local_rehearsal.py            # 跑一遍
.venv/bin/python scripts/local_rehearsal.py --headed   # 显示浏览器窗口，便于围观
.venv/bin/python scripts/local_rehearsal.py --keep     # 保留临时数据以便排查
```

它会起一个**临时环境**（临时库 + 独立端口，绝不碰你的开发数据），然后用**真浏览器**
把整条链路点一遍：建号 → 销售传截图录单 → 采购 → 上海仓收货/带货/发货 → 结算完成 →
异常登记与了结 → 第二单卡住触发卡单提醒 → 权限隔离 → 导出 CSV → AI 助手 → 手机视口巡检 →
备份与恢复演练。**某一步失败也继续往下跑**，最后给完整报告（不然只能看到第一个坑）。

**为什么强调"真浏览器"**：直接调接口测不到"按钮点不动""JS 报错""折叠面板里点不到"这类问题。
它会先展开 `<details>` 再点真实按钮 —— 就是要验证用户真的能操作。

### 部署

看 **[`deploy/DEPLOY.md`](deploy/DEPLOY.md)** —— 从「先测国内→东京延迟」到 systemd、Caddy、
备份定时器、上线自检、交付清单，一条龙。

两个脚本可以**先在本地跑一遍**（不依赖服务器）：

```bash
.venv/bin/python -m scripts.preflight     # 自检：密钥强度/cookie Secure/存储/模型/库完整性/磁盘
.venv/bin/python -m scripts.backup_db     # 备份：产出 backups/erp-*.sqlite3 + 清单 + 轮转
```

> 备份用 SQLite 官方**在线备份 API**，不是 `cp` —— 库开了 WAL，直接拷可能拿到
> 「新数据还在 WAL 里」的半截状态，**恢复时才发现少了最近的单**。
> 已做过恢复演练：从备份还原后账号/订单/截图/流转记录数与线上**完全一致**。

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
- **D7 已完成**：**卡单（超时）提醒** · **CSV 数据导出** · 首页待办收口 · 权限收口复查（有测试遍历全部路由）
- **手机端适配已完成**：宽表转卡片、筛选折叠、触摸目标 ≥40px、输入框 16px（防 iOS 聚焦缩放）
- **AI 助手 P1 已完成**：`/assistant` 自然语言查单（只读）
- **AI 助手 P2 已完成**：`/briefing` 每日简报（卡单 + 待办 + 异常，可复制转发）
- **AI 助手 P3 已完成**：录单页「传截图自动填表」（多模态提取 → 预填 → **人确认**）
- 待做：AI 助手 P4（对话式**写**操作 —— 写操作必须经接口层二次确认）；部署东京 + 实测国内访问

### 截图预填（P3，录单页）

录单页顶部有「**有订单截图？先传图，自动填表**」：选一张截图 → `POST /orders/extract`
→ 模型提取 8 个字段 → **预填进表单并高亮**，同时该图自动作为订单截图留存。

**两条铁律**（都有测试盯着）：

1. **绝不自动建单。** 这个接口只返回字段，不碰数据库写操作；建单仍然走 `/orders/new`，
   照样过 `_validate_order_form`。模型再怎么胡说，也造不出一张错的订单。
2. **模型输出一律不可信。** 解析层做防御：剥 markdown 围栏、只认白名单字段、
   逐字段类型/格式校验（日期不合法就丢、金额非正数就丢、"看不清"当没识别到）。
   模型吐垃圾进来的最坏结果是"没预填"，**不能是"填错"**。

**实弹验证**：喂一张内容已知的仿真订单截图（`tests/fixtures/order_screenshot.png`，合成数据），
真实模型抽出 8 个字段全对；再喂一张空白图，**一个字段都没编**（不许靠常识补全）。

### 每日简报（`/briefing`）

打开就看到「**哪里卡住了**」：一句话结论 + 卡单明细（卡在哪个状态、多久、该谁接手）
+ 该角色的待办 + 异常未了结。范围内**受可见性约束**——销售看不到别人的卡单。

**为什么这一版不接模型**：简报是给老板看业务事实的，用规则引擎生成能保证**每个数字都能溯源**；
"LLM 润色"收益极小、编造风险不小（文档 §9 里它也只是"可选增强"）。

**为什么是"可复制文本"而不是推送**：文档 §1 明确「不做短信/邮件/LINE/微信推送」，
所以做成一段可复制的纯文本（点「复制日报」），由人转发 —— 不越一期边界。

### AI 助手（`/assistant`）

助手是**只读**的，用大白话查订单：`20261010-001 到哪一步了` / `我有哪些待办` / `有哪些卡单`。

两种模式，按配置自动切换：

| 模式 | 触发条件 | 说明 |
|---|---|---|
| **规则模式** | 未配 `ERP_LLM_*`（默认） | 只认固定问法，不调用模型。开发文档 §9 自己也写了这类问题「规则引擎即可」——更快、不花额度、**不可能胡说** |
| **模型模式** | 配了三个 `ERP_LLM_*` | 走 OpenAI 兼容协议，DeepSeek / 通义 / Kimi / 中转站通用，可自由提问 |

当前已接 **DeepSeek**，配置在 `.env`（不进仓库）：

```
ERP_LLM_BASE_URL=https://api.deepseek.com/v1
ERP_LLM_MODEL=deepseek-flash
```

**关于模型名（容易踩）**：DeepSeek 实际只开放两个模型（`GET /v1/models` 可查）：

| 模型名 | 实测 | 说明 |
|---|---|---|
| `deepseek-flash` | 1.1s / 工具调用正常 / **支持读图** | 快、便宜 —— **当前用的** |
| `deepseek-v4-pro` | 1.7s / 工具调用正常 / **不支持读图** | 更强、更慢、更贵 |

- ⚠️ **别写 `deepseek-chat` / `deepseek-reasoner`**：它们只是别名，都会被路由到 `deepseek-flash`，
  这种隐式映射出了问题不好排查。

**关于视觉能力（实测，别踩这个坑）**：把图按 OpenAI 视觉格式塞进 `messages`
（`content` 数组 + `image_url` + base64）即可 —— **`deepseek-flash` 能正确读出图中文字、
颜色，甚至数出图形个数**，所以截图预填直接用它就行，不需要另配厂商。

> ⚠️ 我一开始判断"DeepSeek 没有视觉模型"，依据是 `/v1/models` 里没有单独的 VL 模型名、
> 且点名 `deepseek-vl` 会被 400 拒 —— **这个推断是错的**：能力内置在 `flash` 里，
> 不体现在模型名上。**判断模型能力要实测，别从模型清单推。**

**实弹安全回归**（默认跳过，会花少量额度）：

```bash
ERP_LLM_LIVE=1 .venv/bin/python -m unittest tests.test_llm_live -v
```

单元测试用 `ScriptedLLM` 验编排逻辑；实弹测试验的是**真实模型被诱导时会不会吐**——
断言的都是不变量（不能泄露什么），不依赖模型措辞，换模型也该成立。

**★ 安全红线（文档 §9 原文：权限过滤在工具层执行，不能靠提示词）**

模型只能决定「调哪个工具、传什么参数」；工具拿的是**真实登录用户**，自己去查库。
所以模型无法通过提示词看到别人的单。三点具体保证，都有测试盯着：

1. **越权查询拿不到** —— 销售查别人的单，工具直接返回查不到；
2. **不泄露存在性** —— 「存在但无权」与「不存在」的回复**一字不差**（只差单号本身，而那是提问者自己输入的）；
3. **没有写工具** —— 工具清单里不允许出现任何写操作（有测试断言 `read_only`）。

每个答案都会展示**依据**（引用了哪几张单，可点开核对）和**它调了哪些工具**，
方便老板验证答案不是编的。

### 手机端

窄屏（≤700px）下的处理，都在 `app/static/style.css` **文件末尾**的媒体查询里
（放最后才能覆盖桌面规则）：

| 处理 | 说明 |
|---|---|
| 宽表转卡片 | 表格加 `class="table table-cards"`，每个 `<td>` 加 `data-label="列名"`，手机上竖排成卡片 |
| 筛选折叠 | 订单页筛选收进 `<details>`，**有筛选条件时才默认展开** |
| 触摸目标 | 按钮最小高度 40px（`.btn-small` 的元素同时带 `.btn`，别单独覆盖它的高度） |
| 输入字号 | 手机端输入框 16px —— 低于这个值 iOS 聚焦时会自动放大页面 |
| 键值表 | 详情页 `.kv` 改成紧凑的「标签左 / 值右」一行一条 |

> 验证方式：用 Playwright 在 390×844（iPhone 12/13/14）视口渲染全部页面，
> 检测「文档是否横向溢出」「有没有 <40px 的点击目标」，并截图人工过目。
> 同时跑 1280px 宽回归，确认手机规则没污染桌面。

### 时间口径（重要）

库里**一律存 naive UTC**（SQLite 不保存时区）。展示时统一走 `app/timeutil.py`：

```jinja
{{ order.created_at | dt }}      {# ✅ 转成 ERP_DISPLAY_TZ 时区 #}
{{ order.created_at.strftime('%Y-%m-%d %H:%M') }}   {# ❌ 会少 8 小时 #}
```

时区用 `ERP_DISPLAY_TZ` 配置（默认 `Asia/Shanghai`）。
有测试专门扫描模板，防止有人又写回裸的 `strftime`。

### 卡单阈值怎么调

`app/sla.py` 里 `DEFAULT_SLA_DAYS` 是**拍脑袋的初值**，需要老板按实际节奏校一遍。
不想改代码就用环境变量覆盖：

```bash
ERP_SLA_DAYS="待采购=2,采购中=5,待上海收货=10"
```

超时的单在列表里标"超时"、首页显示"卡单提醒"。
**提醒范围受角色可见性约束**——采购看不到销售的卡单，销售也看不到别人的。

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


