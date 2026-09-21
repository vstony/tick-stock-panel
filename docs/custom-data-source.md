# 自定义数据源接入

本项目默认使用 TickFlow。自定义数据源是一个可选扩展: 外部 HTTP 服务负责取数和整理, 本项目只把返回结果映射成内部标准字段, 然后复用现有存储、指标、enriched、策略和前端展示逻辑。

## 支持范围

当前自定义源支持六类数据:

| 数据集 | 配置名 | 说明 |
| --- | --- | --- |
| 日K | `daily` | 批量返回一组股票在指定区间内的日K |
| 除权因子 | `adj_factor` | 批量返回一组股票的复权因子 |
| 实时行情 | `realtime` | 返回全市场快照,用于盘中 enriched 增量计算 |
| 分钟K | `minute` | 返回 1m 分钟K(需映射出 symbol / datetime / OHLC / 量额) |
| 全量分钟 | `full_minute` | 与 `minute` 同形;声明后可被路由为「全量分钟」生效源,内置服务盘中按当日窗口全市场批量落盘(仅修复轮语义,节奏下限 60s)。**要求上游能低成本提供全市场数据**: 每轮会把全市场标的都拉一遍, 只能按标的拉的上游代价很高, 见下 |
| 财务数据 | `financial` | 一个配置覆盖内部 5 张财务表(`metrics`/`income`/`balance_sheet`/`cash_flow`/`shares`),用 `table_map` 把内部表名映射到上游接口名 |

深度盘口(depth5)暂无数据集契约,仍由 TickFlow 提供。

`full_minute` 声明式源只提供修复轮(当日窗口批量);廉价增量端点
(`get_intraday_latest`)是 Python 插件契约,见
[plugin-development.md](./plugin-development.md)。

> **上游需要真·全市场数据**: `full_minute` 每轮都把全市场标的拉一遍(用 `batch` 分块)。
> 上游若有「一次回全市场」的端点(如 TickFlow `intraday.universe`、插件实现
> `get_intraday_latest`)则代价低;纯按标的的源只能靠批量循环**模拟**, 一轮请求数 =
> 标的数 / `batch`(以全市场 ≈ 5568 标的、`batch` 20 计 ≈ 279 请求/轮), 且每轮重拉当日全量。
> 选源时先确认这个代价可以接受。

> **声明 ≠ 生效**: 声明 datasets 只是让该源进入对应能力的**候选列表**, 真正生效还需在
> 设置 → 数据源 的对应能力卡片里点选它(每个能力独立路由)。侧栏徽标/能力卡里的「未接入」
> 含义是「**当前路由的源供不了这个能力**」, 不代表没人支持 —— 候选里有源时徽标会提示
> 「未接入 · 可切到 X」, 点一下即可; 候选为空才是真的没有源提供。

> 单请求行数上限是**静默截断**(不是报错、不翻页): 超出时上游只回前 N 条且丢最旧数据。
> 实测某上游 `stk_mins` 硬上限 8000 行 —— 20 标的跨 2 日应回 9640 行, 实测恰好 8000 行、
> 且只剩后一天。因此 `batch` 必须按「单标的行数 × 窗口内天数」折算: 全量分钟只拉当日
> (241 根/股) 可以取 20, 多日历史分钟请调小 batch 或按交易日拆成多轮调用。

## 配置位置

把 YAML 放到运行数据目录下:

```text
data/data_sources/*.yaml
```

Dev 模式下，默认位置是项目根目录的 `data/`；Docker 部署中，项目的 `data/` 会挂载为容器内的 `/app/data`。可通过 `DATA_DIR` 覆盖。

修改 YAML 后可在「设置 -> 数据源」点击「重新加载」,或调用:

```bash
curl -X POST http://127.0.0.1:3018/api/settings/data-sources/reload
```

## 最小 YAML

```yaml
name: mock_source
display_name: "Mock 自定义数据源"
auth:
  type: none

datasets:
  daily:
    url: http://127.0.0.1:3021/daily
    method: POST
    batch: 100
    rpm: 200
    response_path: data
    field_map:
      ts_code: symbol
      trade_date: date
      open: open
      high: high
      low: low
      close: close
      vol: volume
      amt: amount
    transforms:
      date: "parse_date(value, '%Y-%m-%d')"

  adj_factor:
    url: http://127.0.0.1:3021/adj_factor
    method: POST
    batch: 100
    rpm: 200
    response_path: data
    field_map:
      ts_code: symbol
      trade_date: trade_date
      factor: ex_factor
    transforms:
      trade_date: "parse_date(value, '%Y-%m-%d')"

  realtime:
    url: http://127.0.0.1:3021/realtime
    method: GET
    rpm: 60
    response_path: data
    field_map:
      ts_code: symbol
      name: name
      last: last_price
      pre_close: prev_close
      open: open
      high: high
      low: low
      vol: volume
      amt: amount
      pct: change_pct
      amount_change: change_amount
      amplitude: amplitude
      turnover: turnover_rate
```

## 字段契约

### daily 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码,如 `000001.SZ` |
| `date` | 交易日 |
| `open` / `high` / `low` / `close` | 不复权 OHLC |
| `volume` | 成交量 |
| `amount` | 成交额 |

### adj_factor 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码 |
| `trade_date` | 除权日期 |
| `ex_factor` | 复权因子 |

### realtime 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码 |
| `last_price` | 最新价 |
| `prev_close` | 昨收 |
| `open` / `high` / `low` | 当日 OHLC |
| `volume` | 成交量 |

建议实时接口额外提供 `amount`、`change_pct`、`change_amount`、`amplitude`、`turnover_rate`、`name`。缺失时部分字段会由 pipeline 回算,但精度取决于可用输入。

`change_pct`、`amplitude`、`turnover_rate` 统一使用小数制,例如 `0.0366` 表示 `3.66%`。百分制单位必须在 realtime 数据集上**显式声明**,不做数值猜测(数值无法区分两种单位:`0.05` 既可能是 0.05% 也可能是 5%):

```yaml
datasets:
  realtime:
    url: https://api.example.com/snapshot
    pct_unit: percent   # 接口返回 3.66 表示 3.66%;小数制源声明 decimal 或省略
```

处理规则:

| 声明 | 行为 |
| --- | --- |
| `pct_unit: percent` | `change_pct` / `amplitude` / `turnover_rate` 无条件 `/100` |
| `pct_unit: decimal` | 三列原样透传 |
| 未声明 | `change_pct` 按截面中位数归一(A 股涨跌停 30% 上限使两种单位物理可分);`amplitude` / `turnover_rate` **置 `None`** 交由 pipeline 按价格与股本口径重算,并记录 WARNING |
| 列已配置 `transforms` | 视为用户已接管该列单位,原样透传 |

## 请求约定

- `daily` / `adj_factor` 会按 `batch` 切分 symbols。
- POST 请求会发送 JSON body: `symbols`、`start_time`、`end_time`。
- GET 请求会发送 query 参数: `symbols=000001.SZ,600000.SH`。
- `realtime` 必须是全市场快照接口,不支持逐个 symbol 拉实时行情。

可通过这些字段改参数名:

```yaml
symbols_param: symbols
start_param: start_time
end_param: end_time
```

分钟数据源如果需要区分资产类型或周期，可继续配置：

```yaml
asset_type_param: asset_type
freq_param: period
```

配置后，分钟请求会分别传入 `stock` / `etf` / `index` 和 `1m`；留空时不向上游发送这两个参数，以兼容已有数据源。

### 请求超时

每个数据集可单独配置请求超时（秒），默认 30：

```yaml
timeout: 60
```

留空或省略时用默认 30 秒，可配置范围为大于 0 且不超过 300 秒；该值对数据同步与「试拉测试」均生效。在设置页编辑数据源时可在「超时」输入框修改（与 批量 / RPM / 响应路径 同行）。「试拉测试」直接使用当前表单内容，新建数据源或尚未保存的修改也可测试。

### 请求模板(${...} 占位符)

有些上游的请求格式无法用「固定 URL + 固定参数名」表达: token 必须放请求体、业务参数要求嵌套、
标的要用逗号串、日期只认紧凑 `YYYYMMDD`(实测 Tushare 收到短横/ISO 会返回 `code=0` 但 **0 行**)。
在这些数据集的 `body` / `params` 里写占位符即可声明式描述协议:

```yaml
body:
  api_name: daily
  fields: "ts_code,trade_date,open,high,low,close,vol,amount"
  params:
    ts_code: "${symbols}"            # 逗号拼接的标的串 600519.SH,000001.SZ
    start_date: "${start:%Y%m%d}"     # 指定 strftime 格式
    end_date: "${end:%Y%m%d}"
```

| 变量 | 含义 |
| --- | --- |
| `${symbols}` | 本批标的, 逗号拼接 |
| `${start}` / `${end}` | 请求窗口起止; 可加 `:%Y%m%d` 等格式 |
| `${table}` | 财务表名(`financial` 数据集), 已按 `table_map` 映射成上游接口名 |
| `${asset_type}` / `${freq}` | 资产类型 / 周期(`minute`、`full_minute`) |

- 值不存在(None) 渲染为空串; **未知变量保留原样**并告警, 便于上游报错时定位(只做字面替换, 不求值)。
- 声明了任一占位符的数据集, 其余参数名/窗口**不再默认注入**(否则注入的标的列表会破坏这类协议),
  即该数据集的请求完全由模板描述。

#### 标的是「600000.SH」形式

`${symbols}` 渲染出来的是本项目内部规范格式: **6 位代码 + 点 + 两位交易所大写后缀**
(`600000.SH` / `000001.SZ` / `920002.BJ`, 与落盘表 `symbol` 列、策略与 JOIN 口径一致)。
上游若只认这种写法(Tushare 的 `ts_code` 就是), 直接把 `${symbols}` 塞给它即可, **不要**
自行拼 `sh600000` 之类的前缀式:

| 写法 | Tushare 实测结果 |
| --- | --- |
| `600000.SH` | ✅ 正常返回(如 14 行日K / 100 期指标 / 6 根分钟) |
| `600000`、`sh600000`、`SH600000`、`600000.XSHG`、`600000.sz` | ❌ `code=0` 但 **0 行**(静默无数据, 不报错) |

- 映射出的 `symbol` 不合该格式时, 本项目会记一条 WARNING(带样例值), 避免"0 行"无从查起。
- 设置页「试拉测试」的手工输入会被**归一**成规范格式(优先查 instruments 维表, 查不到时
  6 开头 → `.SH`、其余 → `.SZ`), 结果区会回显「实际请求标的」, 所以填 `600000` 也能测通。

### 上游响应形状(记录列表 / 列式信封)

`response_path` 定位到的节点支持两种记录形状, 其他形状明确告警并跳过, 不猜列序——猜错只会
静默生成看似合理的错误行情:

| 上游返回 | 结果 |
| --- | --- |
| `{"data": [{"ts_code": "600519.SH", "close": 1467.96}]}` | ✅ 逐条映射 |
| `{"data": {"ts_code": "600519.SH", "close": 1467.96}}` | ✅ 按单条记录处理 |
| `{"data": {"fields": ["ts_code", ...], "items": [["600519.SH", ...]]}}` | ✅ 列式信封: 按字段名与位置解包 |
| `{"data": {"items": [["600519.SH", ...]]}}` | ❌ 纯位置数组(无字段名) → 告警 + 空数据 |

启用 token 放在请求体(`auth.type: body`)的数据集, 若上游把错误放在 HTTP 200 的 `code`/`msg` 里
(如 Tushare 40101/40203), 本项目会记一条 WARNING 指出业务错误码——不让它只表现为"0 行"。

单位换算用 `transforms`, 支持固定表达式白名单: `value * 100` / `value * 1000` / `value / 100` /
`value / 10000` / `parse_date(value, '格式')` / `parse_datetime(value, '格式')`。不支持任意算式。

### 复权因子口径: adj_factor_mode

内部 `ex_factor` 契约是**单事件比值**(除权前收盘价 / 除权参考价, 通常 > 1), 累积链由本项目
pipeline 按交易日自建。上游给的是**累积**因子时(Tushare `adj_factor`、同花顺事件 dump 等),
必须声明由本项目换算:

```yaml
datasets:
  adj_factor:
    adj_factor_mode: cumulative   # 默认 single(上游直接给单事件比值)
```

换算规则: `ex_factor = factor(D) / factor(前一交易日)`(累积乘积恰好还原上游累积序列),
取数窗口会自动向前多取 40 天回看(否则窗口首个除权日会因缺"前一交易日因子"而丢失),
换算后再裁回请求窗口; 因子修订抖动与超出量级的值一律剔除。

### 财务数据集: 表名映射与单位(table_map)

一个 `financial` 数据集要覆盖多张上游接口(利润表、资产负债表…), 靠 `table_map` 把**内部表名**
映射成**上游取值**; 该值经 `${table}` 注入请求(通常就是 `body.api_name`):

```yaml
  financial:
    batch: 1                 # 多标的共用一个请求时, 上游可能静默返回 0 行(见下)
    response_path: data
    table_map:               # 内部表名 → 上游接口名; 未声明的表视为「不支持」直接跳过
      metrics: fina_indicator
      income: income
      balance_sheet: balancesheet
      cash_flow: cashflow
    body:
      api_name: "${table}"   # 渲染为上面的上游接口名
      fields: "ts_code,end_date,ann_date,..."
      params:
        ts_code: "${symbols}"
    field_map: { ... }        # 一份映射覆盖各表(列不冲突即可)
    transforms: { ... }
```

内部表与必需列(与内置财务表的落盘契约一致):

| 内部表 | 必需列 |
| --- | --- |
| `metrics` | `symbol`、`period_end`、`announce_date` + 因子用指标(`bps`/`roe`/`gross_margin`/`net_margin`/`revenue_yoy`/`net_income_yoy`/`debt_to_asset_ratio` 等) |
| `income` / `balance_sheet` / `cash_flow` | `symbol`、`period_end`、`announce_date` + 各表 canonical 列(canonical 名见 `plugins/fuyao/provider.py` 的字段表) |
| `shares` | `symbol`、`period_end`、`float_shares`(单位: 股) |

口径与单位(上游不同接口常不一致, 必须在 `transforms` 里统一):

- **金额 → 元**(如千元/万元乘系数); 内部财务金额一律以元落盘。
- **比率 → 百分数**(ROE 34.19 表示 34.19%), 与 `factors/registry.py` 的中文口径一致; 小数制需 `value * 100`。
- **股本 → 股**: Tushare `daily_basic.float_share` 是万股, 需 `value * 10000`。
- 同一来源在两张表里同名不同单位时**不要映射**(如 `total_share` 在 `balancesheet` 是股、在
  `daily_basic` 是万股), 否则后映射的会覆盖先映射的值。

其它工程约束(均经实测, 踩过就记在这里):

- `(symbol, period_end)` 在同一批返回里**唯一**: 上游可能对同一报告期返回完全重复的行, 本项目在
  合并后按 `announce_date` 去重保留最新一行。
- 单请求行数上限: 上游常按接口限行(如 Tushare `income` 一次最多 100+ 期、`balancesheet` 100 期),
  超出部分不会自动翻页 —— 需要更长历史时只能按标的拆请求。
- `batch` 不一定是越大越好: 部分接口**不支持多标的**(逗号串静默返回 0 行), 这类数据集应 `batch: 1`。
- 声明了 `table_map` 时它就是支持范围: 内部会调用 `shares` 等未声明的表时直接跳过(返回空表,
  已有落盘数据保留), 不会向上游发无效请求。
- **`batch: 1` 的源同步很慢, 前端进度又按整表计数**: 一张表要按标的逐个请求(上游没有"一次取全市场"
  的接口), 5000+ 只标的即 5000+ 次请求, 受 `rpm` 限制 —— 例如 `rpm: 400` 时约 14 分钟/表、全量 5 张表
  约 70 分钟。所以「已同步 0/5 张表…」会持续十几分钟才跳到 1/5; 只想看核心指标时用**单表同步**更快。
  想缩短时间只能提高 `rpm`, 上限是上游配额(Tushare 财务接口实测 500 次/分钟, 超限返回 `code=40203`)。

### 完整示例(token 进请求体 + 业务参数嵌套 + 财务表映射)

覆盖「token 在请求体 / 业务参数嵌套在 params / 时间占位符 / `${table}` 多表映射」的骨架,
把 `url` 与字段名换成你要接的厂商实测值即可(可本地跑通的 mock 示例见
[docs/examples/custom-data-source](./examples/custom-data-source/README.md): `mock_server.py` + `mock_source.yaml`):

```yaml
name: my_http_api
display_name: "My HTTP 数据源"
auth:
  type: body            # token 进 POST 请求体(参数名默认 token)
  token_env: MY_HTTP_TOKEN

datasets:
  daily:
    url: https://api.example.com/v1/kline
    method: POST
    batch: 20            # 有行数上限时: 单标的行数 × 窗口交易日数 ≤ 上限
    rpm: 240
    response_path: data  # 列式信封 {fields, items} 由本项目按字段名解包
    body:
      method: kline.daily
      fields: "code,date,open,high,low,close,vol,amount"
      params:
        codes: "${symbols}"                 # 逗号串(见「请求模板」)
        start_date: "${start:%Y%m%d}"
        end_date: "${end:%Y%m%d}"
    field_map:
      code: symbol
      date: date
      open: open
      high: high
      low: low
      close: close
      vol: volume
      amount: amount
    transforms:
      date: "parse_date(value, '%Y%m%d')"
      amount: "value * 1000"                # 上游给千元时必须显式换算

  adj_factor:
    # ...同构 daily; 上游给累积因子时加 adj_factor_mode: cumulative
  minute:
    # ...同构 daily; 时间参数用 ${start:%Y-%m-%d %H:%M:%S},
    # volume 单位是股时用 transforms: volume: "value / 100"(股→手)
  financial:
    # ...同构 daily; 用 table_map 把内部表名映射到上游接口名,
    # 请求模板里用 ${table} 注入(见「财务数据集: 表名映射与单位」)
```

覆盖范围与注意:
- **一个数据集只有一个 url/body 模板**: 同一数据集若需按资产类型换接口(如 A 股 / ETF / 指数
  各一个端点), 请把该数据集路由到其它源, 或另写插件(插件可在 provider 内按 asset_type 分流)。
- 财务只覆盖**内部真实消费**的 4 张表(`metrics`/`income`/`balance_sheet`/`cash_flow`):
  上游那些没有下游消费方的接口(业绩预告/快报、分红送股、审计意见、主营构成等)接进来
  只会落一堆无人读的 parquet。需要时先在项目侧加内部表契约与消费方(因子/页面), 再在
  `table_map` 里加一行映射。
- 历史股本(`shares`)默认也可以不开: 上游常只有按交易日的股本(单标的数千行), 全市场同步代价高;
  需要时给 `table_map` 加一行, 并在 `field_map` 里处理单位(常见「万股 → 股」)。
- `full_minute` 见上文「上游需要真·全市场数据」: 纯按标的追加批量的源代价高,
  且声明式源没有 `get_intraday_latest`, 只能走仅修复轮。

> **Tushare Pro 已改为内置插件接入**(`backend/app/plugins/tushare/`, 见
> [plugin-development.md](./plugin-development.md)): 插件能在 provider 内按 asset_type 分流接口、
> 处理累积因子换算与财务多表映射, 比 YAML 模板表达力更强。本文档保留的是**通用**接入方式。

> `body` / `params` / `table_map` / `adj_factor_mode` 没有设置页表单控件, 但会在配置回填与保存时
> **原样保留**; 修改这几项请直接编辑 `data/data_sources/*.yaml` 后点「重新加载」。

## 鉴权

支持四种鉴权:

```yaml
auth:
  type: bearer
  token_env: MY_DATA_TOKEN
```

```yaml
auth:
  type: header
  header: X-Token
  token_env: MY_DATA_TOKEN
```

```yaml
auth:
  type: query
  param: token
  token_env: MY_DATA_TOKEN
```

```yaml
auth:
  type: body            # Token 注入 POST 请求体(参数名 param, 默认 token)
  param: token
  token_env: MY_HTTP_TOKEN
```

Token 可以放在系统环境变量或项目 `.env` 中。`type: body` 只适用于 POST 数据集(否则配置校验报错);
Token 需要放进请求体时**用它而不是把密钥写进 `body` 模板**, 避免密钥进配置文件与日志。

## 联调流程

1. 启动 mock 数据源:

```bash
cd docs/examples/custom-data-source
python mock_server.py
```

2. 复制示例配置:

```bash
mkdir -p data/data_sources
cp docs/examples/custom-data-source/mock_source.yaml data/data_sources/mock_source.yaml
```

3. 在「设置 -> 数据源」点击「重新加载」。

4. 使用「试拉测试」选择 `mock_source` 和 `daily` / `adj_factor` / `realtime`。

5. 保存数据源选择:

- 日K: `mock_source`
- 除权因子: `mock_source` (或保持默认 `tickflow`)
- 实时行情: `mock_source`

6. 触发同步或开启实时行情。

## 常见错误

| 现象 | 处理 |
| --- | --- |
| 列表里没有 custom 源 | 检查 YAML 是否放在 `data/data_sources/` 并点击重新加载 |
| errors 提示 missing mapped fields | `field_map` 没映射到必填内部字段 |
| 试拉 rows 为 0 | 检查 `response_path` 是否指向数组 |
| 日期列全为空 | 检查 `parse_date` 的格式是否和返回值一致 |
| 实时行情没刷新 | 确认实时数据源已保存为 custom,且返回全市场快照 |

## 用 AI 生成映射配置

如果你的数据源 API 文档比较复杂,可以把 API 文档和返回示例丢给 AI,让它帮你生成 `field_map` 和 YAML 配置。

### 操作步骤

1. 从你的数据源获取 API 文档(接口地址、请求方式、返回字段说明)
2. 试拉一次,拿到返回的 JSON 示例
3. 把下面的 prompt 模板 + API 文档 + JSON 示例一起发给 AI
4. 把 AI 生成的 YAML 贴到 `data/data_sources/xxx.yaml`
5. 在设置页点「重新加载」,再「试拉测试」验证

### Prompt 模板

复制以下内容发给 AI(替换方括号部分):

```text
我在配置一个自定义数据源接入股票面板。请根据我的 API 文档和返回示例,生成 YAML 配置。

要求:
1. 输出标准 YAML 配置,包含 name / display_name / auth / datasets
2. 每个数据集的 field_map 把我的接口字段名映射到内部字段名
3. 日期类字段如果格式不是 YYYY-MM-DD, 加上 transforms 里的 parse_date
4. 只配置我能提供的接口, 不存在的数据集不要写

内部字段对照表:

日K (daily):
  symbol = 股票代码, 格式 000001.SZ / 600000.SH
  date = 交易日期
  open / high / low / close = OHLC
  volume = 成交量
  amount = 成交额

除权因子 (adj_factor):
  symbol = 股票代码
  trade_date = 除权日期
  ex_factor = 复权因子, **单事件比值(非累积)**: 即 除权前收盘价 / 除权参考价(通常 > 1);
              累积链由本项目 pipeline 按交易日自建。上游若只能给累积因子(Tushare adj_factor 等),
              必须在上游换算为相邻交易日因子之比后再返回

实时行情 (realtime):
  symbol = 股票代码
  last_price = 最新价
  prev_close = 昨收价
  open / high / low = 当日 OHLC
  volume = 成交量
  amount = 成交额
  change_pct = 涨跌幅 (小数, 0.0366 = 3.66%)
  change_amount = 涨跌额
  amplitude = 振幅 (小数, 0.024 = 2.4%)
  turnover_rate = 换手率 (小数, 0.05 = 5%)
  # 上游若返回百分数值 (3.66 表示 3.66%), 在 realtime 数据集声明 pct_unit: percent,
  # 不要依赖数值自动识别; 逐列转换也可用 transforms: turnover_rate: "value / 100"

分钟K (minute) 与 全量分钟 (full_minute, 字段同 minute):
  symbol = 股票代码
  # datetime 必须是北京时间墙钟 (如 2026-08-28 09:35:00), 不要返回 UTC;
  # 入口守卫会自动纠偏 UTC 特征帧, 但契约仍要求源头写对
  datetime = 北京时间墙钟 (YYYY-MM-DD HH:MM:SS)
  open / high / low / close = OHLC
  volume = 成交量
  amount = 成交额

=== 我的 API 文档 ===
[把你的接口文档贴这里: URL / 请求方式 / 参数 / 返回字段说明]

=== 返回 JSON 示例 ===
[把试拉的 JSON 返回贴这里]
```

AI 会输出类似这样的结果:

```yaml
name: my_source
display_name: "我的数据源"
auth:
  type: bearer
  token_env: MY_API_TOKEN

datasets:
  daily:
    url: https://api.example.com/kline
    method: POST
    batch: 100
    rpm: 200
    response_path: data.list
    field_map:
      ts_code: symbol
      trade_date: date
      open: open
      vol: volume
    transforms:
      date: "parse_date(value, '%Y%m%d')"
```

把这段 YAML 保存为 `data/data_sources/my_source.yaml`,然后在设置页重新加载即可。
