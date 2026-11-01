# apibot API 对接文档

## 1. 先说结论

`apibot` 当前是一个 **Telegram 机器人**，不是完整的通用 HTTP 开放平台。

现在真正可以给第三方直接对接的外部接口，主要只有两类：

1. `OKPay` 支付回调入口
2. `apibot` 依赖的上游供应商 API 约定

如果你的目标是“让别的系统直接调 `apibot` 完成查商品、下单、查余额、查订单”，**这套 HTTP 业务 API 目前还没有在 `apibot` 里实现**。现在业务主流程仍然跑在 Telegram 指令、按钮和回调里。

---

## 2. apibot 当前对外接口

### 2.1 OKPay 回调服务

`apibot` 在满足下面条件时会启动一个本地 HTTP 服务：

- `OKPAY_SHOP_ID` 已配置
- `OKPAY_SHOP_TOKEN` 已配置
- `OKPAY_CALLBACK_URL` 已配置

默认监听：

```text
OKPAY_CALLBACK_HOST=0.0.0.0
OKPAY_CALLBACK_PORT=8088
```

说明：

- 外部平台通常不会直接访问 `0.0.0.0:8088`
- 常见做法是把 `OKPAY_CALLBACK_URL` 反代到这台机器的 `8088`
- 内置回调处理器不限制固定 path，所以 `/okpay/callback` 这类路径可以由反代层自行映射

### 2.2 健康检查

#### 请求

```http
GET / HTTP/1.1
```

#### 响应

```http
HTTP/1.1 200 OK

OKPay callback server is running
```

用途：

- 反代探活
- 本地联通性检查

### 2.3 OKPay 支付回调

#### 请求

```http
POST / HTTP/1.1
Content-Type: application/x-www-form-urlencoded
```

也兼容：

```http
Content-Type: application/json
```

如果是 JSON，程序会把嵌套对象拍平成 `data[xxx]` 的形式参与验签和入账。

#### 必要业务条件

只有在以下条件全部成立时，回调才会进入“已支付入账”流程：

- `unique_id` 或 `data[unique_id]` 存在
- `type=deposit` 或 `data[type]=deposit`
- `status=1` 或 `data[status]=1`
- 签名校验通过
- 本地充值订单存在且状态允许入账
- 币种与金额匹配

#### 常见字段

表单和 JSON 两种格式，最终程序都会兼容读取这些字段：

| 字段 | 说明 |
| --- | --- |
| `sign` | OKPay 签名 |
| `unique_id` / `data[unique_id]` | 本地充值订单号 |
| `order_id` / `data[order_id]` | OKPay 上游订单号 |
| `amount` / `data[amount]` | 支付金额 |
| `coin` / `data[coin]` | 币种，预期为 `USDT` |
| `status` / `data[status]` | 支付状态，成功时为 `1` |
| `type` / `data[type]` | 业务类型，预期为 `deposit` |
| `pay_user_id` / `data[pay_user_id]` | 支付用户标识 |

#### 签名规则

程序会把除 `sign` 外的非空字段按 key 排序后拼接 query string，再按下面方式计算：

```text
MD5(query_string + "&token=" + shop_token).upper()
```

程序兼容两种签名输入结构：

1. 普通平铺表单
2. 包含 `data[xxx]` 的嵌套回调字段

#### 成功响应

```json
{
  "status": "success",
  "message": "paid"
}
```

幂等情况下也可能返回：

```json
{
  "status": "success",
  "message": "already_paid"
}
```

#### 失败响应

签名错误：

```http
HTTP/1.1 403 Forbidden

bad sign
```

业务校验失败时会返回 `400`，例如：

```json
{
  "status": "error",
  "message": "amount_mismatch"
}
```

#### 可能返回的 message

| message | 含义 |
| --- | --- |
| `paid` | 已成功入账 |
| `already_paid` | 该订单已处理，重复回调直接幂等返回 |
| `not_paid` | 回调状态不是成功入账事件 |
| `order_not_found` | 本地没有找到对应订单 |
| `processing` | 订单已在处理中 |
| `expired` | 订单已过期 |
| `coin_mismatch` | 币种不匹配 |
| `invalid_amount` | 金额无效 |
| `amount_mismatch` | 金额与本地订单不匹配 |
| 其他状态值 | 本地订单状态不允许重复处理 |

---

## 3. Telegram 业务入口

这部分不是 HTTP API，但它是 `apibot` 当前真实在跑的业务入口，对接时要知道边界。

### 3.1 已注册命令

| 命令 | 说明 |
| --- | --- |
| `/start` | 启动和首页 |
| `/menu` | 主菜单 |
| `/help` | 帮助 |
| `/admin` | 后台首页 |
| `/me` | 个人中心 |
| `/categories` | 列表①分类 |
| `/products <category_id>` | 列表①商品列表 |
| `/product <product_id>` | 列表①商品详情 |
| `/buy <product_id> <quantity>` | 列表①快捷购买 |
| `/orders` | 我的订单 |
| `/order <task_id>` | 查询单个订单 |
| `/supplier_balance` | 查询上游余额，仅管理员 |
| `/add <user_id> <+金额/-金额>` | 调整用户余额，仅管理员 |
| `/credit <user_id> <金额>` | `/add` 的兼容别名 |
| `/cha` | 管理员查用户/订单相关能力 |

说明：

- 普通用户主流程依赖 Telegram 消息、按钮和 callback
- 这些 callback 数据格式属于机器人内部协议，不建议外部系统直接依赖

---

## 4. 上游供应商接口约定

`apibot` 自己不提供完整业务 HTTP API，但它已经稳定封装了 3 套上游接口。  
如果你是要“照着 `apibot` 这套逻辑去做对接”，下面这部分就是当前代码里的真实接口契约。

### 4.1 列表① legacy 供应商

基础地址默认值：

```text
API_BASE_URL=https://onlinestore-fx-api.add4533.com
```

#### 认证

支持以下配置组合：

1. Header 鉴权

```text
API_AUTH_HEADER_NAME=Authorization
API_AUTH_HEADER_VALUE=你的key
API_AUTH_TRY_BEARER_VARIANTS=true
```

说明：

- 当前代码会优先尝试你填写的原值
- 如果 Header 名是 `Authorization`，并且打开了 `API_AUTH_TRY_BEARER_VARIANTS`，还会自动兼容裸 token / `Bearer token`

2. Query 鉴权

```text
API_AUTH_QUERY_NAME=token
API_AUTH_QUERY_VALUE=你的key
```

3. 额外固定头和固定参数

```text
API_EXTRA_HEADERS_JSON={"X-Api-Key":"xxx"}
API_EXTRA_QUERY_JSON={"uid":"10001"}
```

#### 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/tgapi/getCategoryList` | 获取分类列表 |
| `GET` | `/tgapi/getProductListByCategoryId` | 获取分类下商品 |
| `GET` | `/tgapi/getProductDetaiById` | 获取商品详情 |
| `GET` | `/tgapi/searchProductListByText` | 按关键词搜索商品 |
| `GET` | `/tgapi/byTgAccountApi` | 创建购买订单 |
| `GET` | `/tgapi/queryOrderState` | 查询订单状态 |
| `GET` | `/tgapi/queryBalance` | 查询上游余额 |

#### 请求示例

获取分类：

```http
GET /tgapi/getCategoryList HTTP/1.1
Authorization: your-key
```

获取商品列表：

```http
GET /tgapi/getProductListByCategoryId?categoryId=1001 HTTP/1.1
Authorization: your-key
```

购买：

```http
GET /tgapi/byTgAccountApi?productId=2001&quantityPurchased=3&timestamp=1761984000000 HTTP/1.1
Authorization: your-key
```

查单：

```http
GET /tgapi/queryOrderState?taskId=abc123&timestamp=1761984000000 HTTP/1.1
Authorization: your-key
```

#### 响应判定

程序把下面条件视为成功：

- 响应是 JSON object
- `success == true`
- `code == 200`

否则会按失败处理，并优先读取：

- `msg`
- `message`

#### apibot 依赖的核心字段

分类列表至少建议包含：

| 字段 | 说明 |
| --- | --- |
| `categoryId` | 分类 ID |
| `categoryName` | 分类名 |

商品列表 / 详情至少建议包含：

| 字段 | 说明 |
| --- | --- |
| `productId` | 商品 ID |
| `productName` | 商品名 |
| `categoryId` | 分类 ID |
| `categoryName` | 分类名 |
| `price` | 上游单价 |
| `totalStock` | 库存 |

订单查询至少建议包含：

| 字段 | 说明 |
| --- | --- |
| `status` | `processing` / `completed` / `partial` / `failed` |
| `quantityRequested` | 请求数量 |
| `quantityDelivered` | 成功数量 |
| `quantityFailed` | 失败数量 |
| `amountRefunded` | 退款金额 |
| `files` | 交付文件列表，可为空 |

余额查询至少建议包含：

| 字段 | 说明 |
| --- | --- |
| `data.userId` | 上游用户 ID |
| `data.userName` | 上游用户名 |
| `data.accountBalance` | 上游余额 |

### 4.2 列表② DAXI 供应商

基础地址：

```text
DAXI_API_BASE_URL=https://api.daxi.pro
```

认证头：

```http
X-API-Key: <DAXI_API_KEY>
```

#### 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/products` | 获取商品列表，可带 `query` |
| `POST` | `/api/v1/orders` | 创建订单 |
| `GET` | `/api/v1/orders/{task_id}` | 查询订单 |
| `GET` | `/api/v1/balance` | 查询余额 |

#### 请求示例

```http
GET /api/v1/products?query=vip HTTP/1.1
X-API-Key: your-key
```

```http
POST /api/v1/orders HTTP/1.1
Content-Type: application/json
X-API-Key: your-key

{
  "productId": "p_10001",
  "quantityPurchased": 2,
  "outOrderId": "local_123"
}
```

#### apibot 使用的关键字段

产品：

| 字段 | 说明 |
| --- | --- |
| `productId` | 商品 ID |
| `productName` | 商品名 |
| `price` | 单价 |
| `totalStock` | 库存 |

余额：

程序会从这些候选字段里归一化读取：

- `userId` / `user_id` / `uid` / `id`
- `userName` / `username` / `user_name` / `name`
- `accountBalance` / `balance` / `usdt` / `amount`

### 4.3 列表③ RZ / DariMarket 供应商

基础地址：

```text
RZ_API_BASE_URL=https://api2.darimarket.online
```

认证头：

```http
X-Api-Key: <RZ_API_KEY>
```

#### 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/v1/products` | 分页获取商品，支持 `q` 搜索 |
| `GET` | `/v1/products/{product_id}` | 商品详情 |
| `POST` | `/v1/buy` | 创建订单 |
| `GET` | `/v1/orders/{task_id}` | 查询订单 |
| `GET` | `/v1/me` | 查询余额和用户信息 |

#### 请求示例

```http
GET /v1/products?limit=100&offset=0&q=asia HTTP/1.1
X-Api-Key: your-key
```

```http
POST /v1/buy HTTP/1.1
Content-Type: application/json
X-Api-Key: your-key

{
  "product_id": "rz_10001",
  "quantity": 2
}
```

#### apibot 内部归一化规则

RZ 原始字段会被程序转换成统一结构，核心映射如下：

| RZ 原始字段 | 统一字段 |
| --- | --- |
| `id` | `productId` |
| `country_name` | `country` / 商品名候选 |
| `kind` | `category` |
| `section` | `categoryName` |
| `phone_code` | `prefix` |
| `stock` | `totalStock` |
| `price` | `price` |

订单状态会被归一化成：

| RZ 状态 | 统一状态 |
| --- | --- |
| `processing` | `processing` |
| `completed` | `completed` |
| `partially_completed` | `partial` |
| `partial` | `partial` |
| `failed` | `failed` |

---

## 5. 本地订单与充值状态

如果你是做外围系统联动，最好按下面状态理解 `apibot` 的本地逻辑。

### 5.1 充值订单状态

`topup_orders.state` 可能出现：

| 状态 | 含义 |
| --- | --- |
| `pending` | 待支付 |
| `processing` | 正在记账 |
| `paid` | 已到账 |
| `canceled` | 已取消 |
| `expired` | 已过期 |

### 5.2 商品订单状态

`orders.state` 主要使用：

| 状态 | 含义 |
| --- | --- |
| `processing` | 上游处理中 |
| `completed` | 全部完成 |
| `partial` | 部分成功 |
| `failed` | 失败 |

---

## 6. 对接建议

### 6.1 如果你只是要接现有 apibot

当前建议只把它当成：

- Telegram 业务机器人
- OKPay / TRC20 充值处理器
- 供应商 API 聚合客户端

### 6.2 如果你要让第三方系统直接调 apibot

建议单独补一层 HTTP API，而不是直接依赖 Telegram callback 或数据库结构。  
最实用的一版通常只需要这些接口：

1. `POST /api/topups/okpay`
2. `POST /api/topups/trc20/check`
3. `GET /api/catalog/categories`
4. `GET /api/catalog/products`
5. `POST /api/orders`
6. `GET /api/orders/{task_id}`
7. `GET /api/users/{user_id}/balance`

这部分当前还没有在代码里实现；如果你要，我可以下一步直接把这层 HTTP API 给你补出来。
