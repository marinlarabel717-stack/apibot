# apibot

一个独立的 Telegram 号铺机器人仓库，用来对接 `https://onlinestore-fx-api.add4533.com` 这套供应商 API。

这个项目和现有 `botshop` 没关系，不复用它的代码、不依赖它的数据库。

## 当前已接好的能力

- 供应商 API 封装
  - 获取分类
  - 获取分类商品
  - 获取商品详情
  - 搜索商品
  - 购买商品
  - 查询订单状态
  - 查询供应商余额
- Telegram Bot 基础命令
  - `/start`
  - `/me`
  - `/categories`
  - `/products <category_id>`
  - `/product <product_id>`
  - `/buy <product_id> <数量>`
  - `/orders`
  - `/order <task_id>`
  - `/credit <user_id> <金额>` 管理员加余额
  - `/supplier_balance` 管理员查看上游余额
- 本地 SQLite
  - 用户余额
  - 钱包流水
  - 订单记录
- 后台轮询处理中订单
  - 完成后自动通知用户
  - 失败自动退款
  - 部分成功自动按差额退款

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

但是这份文档 **没有写认证方式**。直接匿名请求会返回：

```json
{"code":500,"msg":"认证错误","data":null,"success":false}
```

所以现在项目里把认证做成了可配置模式：

- 单个认证 Header
- 单个认证 Query 参数
- 多个额外 Header
- 多个额外 Query 参数

如果对方给你的是固定 token / apiKey 这一类，改 `.env` 就能直接接。

如果对方给你的是签名算法，比如 `md5(secret + timestamp)` 这种，再补一小段签名逻辑就行。

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
API_AUTH_HEADER_NAME=
API_AUTH_HEADER_VALUE=
API_AUTH_TRY_BEARER_VARIANTS=true
API_AUTH_QUERY_NAME=
API_AUTH_QUERY_VALUE=
```

### 3. 启动

```powershell
python bot.py
```

## 认证配置示例

### 示例 1：对方给的是 Header Token

```text
API_AUTH_HEADER_NAME=Authorization
API_AUTH_HEADER_VALUE=xxxxx
API_AUTH_TRY_BEARER_VARIANTS=true
```

### 示例 2：对方给的是 Query Token

```text
API_AUTH_QUERY_NAME=token
API_AUTH_QUERY_VALUE=xxxxx
```

### 示例 3：对方要求多个固定参数

```text
API_EXTRA_HEADERS_JSON={"X-Api-Key":"xxxxx"}
API_EXTRA_QUERY_JSON={"uid":"10001"}
```

## 说明

- 用户购买走的是 **机器人本地余额**，不是直接透传让任何人消耗你的上游额度。
- 供应商订单失败或部分成功时，会自动退回本地余额。
- 这个仓库现在是一个可跑的独立起点，等你拿到上游认证细节后，就可以继续补真正的通道。
- 如果认证 header 用的是 `Authorization`，程序默认会自动尝试 `Authorization: key` 和 `Authorization: Bearer key` 两种格式。
