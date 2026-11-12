# 疏散演练跨页表格的证据保留抽取资料源

本项目保存本地合成资料及对应的来源站行为，供资料接入开发使用。内容取自虚构场景，不代表真实活动、人员、设施或设备参数。来源服务只模拟公开读取与指定的表单行为，不包含资料采集端。

## 环境与运行

使用 Python 3.11 及以上版本，仅依赖标准库。来源只监听本机地址，端口可以通过参数调整。两个终端分别启动不同端口可以表示两个独立来源。

```sh
python -m source_site --port 8090
```

`source_site/source.py` 管理来源响应，`source_site/__main__.py` 是 HTTP 适配层，`sources/` 保存原文与接口约定。状态只存在来源进程内，重启来源会恢复初始样本。`/__control` 和 `/__calls` 属于本地运维入口，资料接入端的正常操作不使用它们。运维端可以通过 POST `/__control` 提供 JSON 对象改变 phase 或更新 state。phase 列表与业务接口见下文，细节见 `sources/contract.json`。

- `GET /documents`：文档id、修订与有序链接
- `GET /doc/D1/r{n}/p{n}`：保留单元格与继承标记的HTML
- `phases`：["base", "revised", "partial"]
- `cell_reference`：页码、table-id与原始DOM行列坐标，序号从1开始

用 Python 的 HTTP 客户端推进来源状态，例如：

```sh
python -c 'import urllib.request; r=urllib.request.Request("http://127.0.0.1:8090/__control", data=b"{\"phase\":\"base\"}", headers={"Content-Type":"application/json"}); print(urllib.request.urlopen(r).read().decode())'
```

## 开发检查

以下检查覆盖来源协议、异常响应和真实本地 HTTP 启动。检查不会要求访问公网，也不会写入业务状态目录。

```sh
python -m unittest discover -s tests -v
python -m compileall -q .
```

容器环境也可以复现相同检查：

```sh
docker build -t evacuation-tables-source .
docker run --rm --network none evacuation-tables-source
```

来源的HTTP异常是合成协议行为，便于资料接入端开发和诊断。样本可扩展；业务数据、原始HTML以及接口响应不应被当作真实部署凭据或真实业务事实。

## 采集与结构化抽取模块（evac_tables）

`evac_tables/` 是新增的采集与结构化抽取模块，只依赖标准库，与来源站解耦。
它把跨页 HTML 表格整理成带证据的结构化记录；任何无法可靠归属的内容都进入
隔离清单，绝不猜测。

### 文档链接入口

```sh
python -m source_site --port 8090 &          # 启动本地来源
python -m evac_tables list --base-url http://127.0.0.1:8090
```

`list` 读取 `GET /documents`，输出每个文档的 doc_id、revision 和有序页面链接。
采集与发布：

```sh
python -m evac_tables extract --base-url http://127.0.0.1:8090 \
    --doc-id D1 --state-dir state --output-dir out \
    --options sources/extraction-options.json
```

离线复核本地 HTML（顺序即页序）：

```sh
python -m evac_tables extract-local --doc-id D1 --revision 1 \
    --state-dir state --output-dir out \
    --options sources/extraction-options.json sources/r1-p1.html sources/r1-p2.html
```

逐字段比较两个归档修订：

```sh
python -m evac_tables diff --state-dir state --doc-id D1 --from 1 --to 2
python -m evac_tables current --state-dir state --output-dir out --doc-id D1
```

### 抽取规则要点

- **跨页继承**：只有同 doc_id、同 revision、同 `data-table-id`，且下一页表格
  显式带 `data-continue="true"` 时才沿用上一页的楼栋/楼层；普通新表不继承。
  续页表必须是本页第一张表且紧接上一页最后一张同 id 表。
- **单位**：表头可写`人数（人）`或`人数（百人）`；单元格里显式写的`人`/`百人`
  优先；都没有时按`人`处理并在输出里标 `unit_source: "default"`。中文数字只
  支持一到九、十、百的合法组合。含`约`、`多人`、区间（`10-20`、`十至二十`）
  的数量一律隔离，不截成精确值。
- **楼层**：支持 `B2`、`1F`、`1-3F`、`B2-B1`、`B1-3F`；跨度上限由
  `extraction-options.json` 的 `maximum_floor_span` 控制，超限时整条隔离，
  不截取前几层。跨层记录拆分后共享同一 `source_row_key` 与原始行证据，
  人数标记 `participants_scope: "row_record"`，不按层分摊。
- **字段定位**：按表头语义（楼栋/楼层/人数/集合点）匹配，支持多行列头与
  列序变化；不硬编码列号或楼栋名。插入无关备注列不影响映射。
- **脚注与备注**：`<sup data-ref="...">` 按引用关系附着到引用行的
  `footnotes`；单元格内嵌套备注表进入 `notes`，绝不混入主体字段。

### 输出格式

`out/<doc_id>/current.json` 是唯一提供给下游的结果，且只含最新完整修订：

- `document`：doc_id、revision、`publishable`、页面清单（页码、URL、SHA-256、
  各页 table-id 与行数）、版本校验错误。
- `records[]`：每条含 `building`、`floor`、`participants`（整数人）、
  `assembly_point`，以及 `evidence`——页码、`table_id`、原始 DOM 行列
  （`row`/`col`，从 1 开始）、每个字段的单元格原文与 HTML 片段、
  `via_rowspan` 标记、`inheritance` 继承路径（来源单元格 + 续页链）、
  `row_raw_text` 原文。
- `quarantine[]`：隔离记录，含 `reason`、`message`、`page`、`table_id`、
  `row` 和涉及的原始单元格 `cells[]`。
- `footnotes[]` / `notes[]`：脚注引用与嵌套备注表，均带原始单元格坐标。

隔离原因：`merged_grid_invalid`（合并网格不合法）、`header_ambiguous` /
`header_missing`（列头无法消歧）、`cross_version_page` /
`cross_version_continuation`（新旧页混用、跨版本续页）、
`row_numbering_conflict`（行内编号冲突）、`quantity_inexact` /
`quantity_unparseable`（数量不精确）、`floor_span_exceeded` /
`floor_unparseable`、`required_field_missing`、`continuation_*`（续页链不合法）、
`footnote_unresolved`、`missing_table_id`。

### 复查方式

1. **定位原始单元格**：取记录或隔离项里的 `page` + `table_id` + `row` + `col`
   （均为原始 DOM 序号，从 1 开始），对照 `state/<doc_id>/revisions/r<n>/pages/p<n>.html`
   归档原文逐格核对；`evidence.fields.*.cell.html` 保存了单元格原始片段。
2. **继承路径**：`evidence.fields.building.inheritance.source` 指向被继承的
   原始单元格，`continuation_chain` 列出跨页链，可验证商场二层不会被套到办公楼。
3. **版本比较**：`state/<doc_id>/diffs/r<旧>_to_r<新>.json` 给出逐字段
   added/removed/changed；旧版本结果与原始页面保留在
   `state/<doc_id>/revisions/` 下。
4. **不完整版本**：采集失败或校验未通过时 `extract` 以退出码 2 结束，
   `current.json` 保持不变，失败详情写入 `state/<doc_id>/failed/` 供核查，
   不会用旧修订页面补缺，也不会发布不完整版本。

`examples/` 保存了用 sources/ 合成文档生成的 r1/r2 完整结果与 r1→r2 比较，
可直接查看跨页继承、单位换算、楼层展开、脚注附着与隔离记录的输出形态。

### 自动检查

```sh
python -m unittest discover -s tests -v
python -m compileall -q .
```

`tests/test_extract.py` 覆盖：跨页继承与继承路径、表头/单元格单位换算、
楼层展开与跨度上限、脚注附着、嵌套备注隔离，以及三类防静默错配场景——
**交换两页顺序**（版本不可发布，4F 不会被归到任何楼栋）、**插入无关备注列**
（字段仍按表头语义定位）、**新旧页混用**（采集层拒绝 + 抽取层整页隔离）。
