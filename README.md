# apibot

一个独立的 Telegram 号铺机器人仓库，用来对接 `https://onlinestore-fx-api.add4533.com` 这套供应商 API。

这个项目和现有 `botshop` 没关系，不复用它的代码，也不依赖它的数据库。

## 当前能力

- 供应商 API 封装
  - 获取分类
  - 获取分类商品
  - 获取商品详情
  - 搜索商品
  - 购买商品
  - 查询订单状态
  - 查询供应商余额
- Telegram Bot 功能
  - `/start`
  - `/menu`
  - `/me`
  - `/categories`
  - `/products <category_id>`
  - `/product <product_id>`
  - `/buy <product_id> <数量>`
  - `/orders`
  - `/order <task_id>`
  - `/add <user_id> <+金额/-金额>`（管理员调整余额）
  - `/credit <user_id> <金额>`（兼容旧命令）
  - `/supplier_balance`
- 底部常驻菜单按钮
  - `商品列表`
  - `主菜单`
  - `个人中心`
  - `我要充值`
- 本地 SQLite
  - 用户余额
  - 钱包流水
  - 订单记录
- 后台轮询处理中的订单
  - 完成后自动通知用户
  - 完成后自动发送打包预览图 + zip 文件
  - 失败自动退款
  - 部分成功自动按差额退款

## 交互风格

现在已经按“商城按钮面板”方式重做：

- 底部常驻菜单
- 分类按钮列表
- 商品按钮列表
- 商品详情页按钮
- 个人中心页
- 充值说明页

用户既可以用命令，也可以直接点按钮操作。

## API 文档结论

我已经确认文档的 OpenAPI 在：

```text
https://onlinestore-fx-api.add4533.com/v3/api-docs/default
```

公开出来的接口一共 7 个：

- `GET /tgapi/getCategoryList`
- `GET /tgapi/getProductListByCategoryId`
- `GET /tgapi/getProductDetaiById`
- `GET /tgapi/searchProductListByText`
- `GET /tgapi/byTgAccountApi`
- `GET /tgapi/queryOrderState`
- `GET /tgapi/queryBalance`

认证方式已经实测确认：

- 使用 `Authorization: 你的key` 可用
- `Authorization: Bearer 你的key` 上游会失败
- 程序里已做兼容回退，`Authorization` 配成裸 key 或 Bearer key 都会自动尝试

## 快速开始

### 1. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

### 2. 配置环境变量

```powershell
copy .env.example .env
```

然后编辑 `.env`。

最少要填：

```text
BOT_TOKEN=
ADMIN_USER_IDS=
SHOP_TITLE=TG-Matrix 账号商城
RECHARGE_TEXT=请联系管理员充值，或者让管理员使用 /add 给你调整余额。
SELL_PRICE_ADD=0.2
API_AUTH_HEADER_NAME=Authorization
API_AUTH_HEADER_VALUE=
API_AUTH_TRY_BEARER_VARIANTS=true
```

仓库已经附带一份可直接改的 `.env.example` 成品模板，复制后主要替换：

- `BOT_TOKEN`
- `ADMIN_USER_IDS`
- `API_AUTH_HEADER_VALUE`
- 如果要微调利润，再改 `SELL_PRICE_ADD` 和 `SELL_PRICE_RULES_JSON`

### 3. 启动

```powershell
python bot.py
```

## 认证配置示例

### 示例 1：Header Token

```text
API_AUTH_HEADER_NAME=Authorization
API_AUTH_HEADER_VALUE=xxxxx
API_AUTH_TRY_BEARER_VARIANTS=true
```

### 示例 2：Query Token

```text
API_AUTH_QUERY_NAME=token
API_AUTH_QUERY_VALUE=xxxxx
```

### 示例 3：额外固定参数

```text
API_EXTRA_HEADERS_JSON={"X-Api-Key":"xxxxx"}
API_EXTRA_QUERY_JSON={"uid":"10001"}
```

## 可配置项

- `SHOP_TITLE`
  - 商城标题，显示在主菜单、个人中心、充值页
- `RECHARGE_TEXT`
  - “我要充值”页面展示的充值说明
- `SELL_PRICE_ADD`
  - 全局固定差价，最终售价 = 上游价格 + 这里的金额
  - 例如 `SELL_PRICE_ADD=0.2`，上游 `1.3` 会卖 `1.5`
- `SELL_PRICE_RULES_JSON`
  - 按关键字单独覆盖固定差价
  - 示例：`{"VIP":{"add":0.5},"Spam":{"add":0.1},"7年":{"add":0.8}}`
- `INLINE_BUTTON_CUSTOM_EMOJI_ENABLED`
  - 是否启用 Telegram custom emoji 按钮图标
- `BUTTON_CUSTOM_EMOJI_IDS_JSON`
  - 按语义 key 配置按钮 custom emoji
  - 已支持：`vip`、`spam`、`liang`、`asia`、`west`、`africa`、`age_2_5`、`age_6_12`、`age_1_2y`、`age_3_4y`、`age_5y`、`age_7y`

## 管理员余额调整

- 增加余额：`/add 123456 +20`
- 扣减余额：`/add 123456 -20`
- 如果用户余额不足，扣减会被拦截，不会扣成负数
- 用户会同步收到余额变动提醒

## 说明

- 用户购买走的是机器人本地余额，不是直接透传消耗你的上游额度。
- 供应商订单失败或部分成功时，会自动退回本地余额。
- 按钮购买现在会先弹“确认购买”图片卡片，订单完成后不再发下载链接，而是直接给用户发 zip 文件。
- 亚洲 / 欧美 / 非洲 / 年龄段这类分类按钮已经内置固定图标规则；如果打开 custom emoji，会优先显示你在 `.env` 里配置的会员图标。
- 这是一个独立起点，后续还可以继续加真充值、支付回调、自动上分等功能。
