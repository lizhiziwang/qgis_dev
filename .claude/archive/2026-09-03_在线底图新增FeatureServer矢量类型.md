# 在线底图新增 FeatureServer 矢量类型（属性工具可识别要素）

日期：2026-09-03

## 背景

承接 [[2026-09-03_自定义ArcGIS底图空白修复]] 与 [[2026-09-03_在线底图右键添加自定义底图]]。
用户：用「属性」工具点已添加的 arcgis 服务
`http://127.0.0.1:6080/arcgis/rest/services/江西省县级行政区域/MapServer`（矢量县界）无法识别要素。

根因（上篇已定）：该类型走 `arcgismapserver` **栅格**动态出图（服务端把矢量面渲染成 PNG），工程里
没有矢量要素可 identify；该 provider 不支持像元级 identify → 属性工具空结果 → 静默。

裁决（AskUserQuestion，用户选）：**新增 FeatureServer 矢量类型**，在「在线底图」按 FeatureServer
端点加载成真矢量层，属性工具即可识别。前提 = 服务开了 Feature Access（有并列 `/FeatureServer`
端点），需用户在真机确认。

## 关键经验（empirical，provider 契约）

- QGIS 4.2 `arcgisfeatureserver` provider 在本打包 QGIS 里**可用**。URI 契约：
  `crs='<EPSG:code>' url='<…>/FeatureServer/<图层id>'`。**crs 必填**（缺失 → invalid）；
  **图层 id 必须嵌在 URL 路径**（裸 FeatureServer 根 invalid）。
- 旧档案「arcgisfeatureserver 全无效」是**用错端点**：拿 `…/MapServer/0` 喂了该 provider。
- **异步加载**：isValid() 冷启动时常需 pump 嵌套事件循环后才转 True（网络往返秒级）→ 需要
  poll 循环（`_wait_until_layer_valid`，200ms 步进 / 25s 超时）。
- `crs=` 语义 = **目标/输出坐标系**：provider 读服务声明的 SR，把几何重投影到给定 crs。传错但合法的
  crs（如 3857 服务喂 4326）不会放错位置——是正确的坐标变换（probe 验证过）。
- SR 发现：FeatureServer 图层 `?f=json` 的 `spatialReference` 常在 `extent` 内或仅服务根有；
  优先 `latestWkid` 再 `wkid`；ArcGIS 别名 `102100/102113 → 3857`。
- `QgsBlockingNetworkRequest` 钉死 API（4.2）：`req.get(QNetworkRequest(QUrl(url)))` 返回
  `ErrorCode`，比 `== QgsBlockingNetworkRequest.ErrorCode.NoError`；`req.reply()` → `QgsNetworkReplyContent`，
  `.content()` 是 QByteArray → bytes。死主机秒回 TimeoutError 不挂起。

## 方案落地

新 type **`arcgisfeature`**（与 `arcgis` 平行，`arcgis` 栅格保留不动）。字段复用
`OnlineBasemapDef`：`url`=FeatureServer 服务根；`layers`=子图层 id（缺省 0 追加进 URL 路径）；
`crs`=服务坐标系（**可选**，留空建层时 `_discover_arcgis_feature_crs` 从服务元数据自动识别）。
构建 `QgsVectorLayer(f"crs='<EPSG:…>' url='<编码后完整 FeatureServer/<id>>'", name, "arcgisfeatureserver")`。

### 文件改动

- `app/core/domain/basemap.py`（tracked M）：新 `build_arcgis_feature_uri(d, crs, keys=None)`（进
  `__all__`）、`_arcgis_feature_layer_url`、`_discover_arcgis_feature_crs`（QgsBlockingNetworkRequest
  GET `{full_url}?f=json`）、`_wait_until_layer_valid`（嵌套 QEventLoop poll）；
  `build_online_basemap_layer` 首位分支 `elif d.type == "arcgisfeature":`，crs 拿不到 → warning 返回
  None（走既有加载失败 messageBar，不静默用错 crs）；返回类型改 `Optional["QgsMapLayer"]`；
  TypeChecking import 补 `QgsVectorLayer, QgsMapLayer`。
- `app/env/config.py`（tracked M）：docstring/字段注释同步；`to_dict` 加 `elif type == "arcgisfeature"`
  → url + 非空 layers/crs（不写 zmin/zmax/referer/format/styles）；`_ONLINE_BASEMAPS_HEADER` 类型清单补。
- `app/ui/dialogs/custom_basemap.py`（untracked ??）：`_SERVICE_OPTIONS` 加
  `("ArcGIS FeatureServer 矢量要素", "arcgisfeature")`；stack 加 `_build_arcgisfeature_page()`
  （子图层 ID + 坐标系 + hint 引导）；`_validate` 加 arcgisfeature 分支（url 须
  `/FeatureServer(?:/\d+)?$`；子图层须数字；crs 不含 &/=；**crs 可留空**）；`basemap_def` 分支。
- `app/ui/dialogs/edit_basemap.py`（untracked ??）：`_load_def` 加 arcgisfeature 预填。
- `app/ui/main_window/main_window.py` / `app/ui/docks/online_basemap_dock.py`：仅 docstring 类型清单同步。
  `_add_online_basemap` 加载路径对图层类型通用，无需代码改动。
- `config/online_basemap_config.yaml`：注释头类型清单补。

identify service **无需改动** —— 真矢量层走既有矢量识别路径即生效。

## 测试（tests/ 目录整棵 gitignored，纯本地跑，不进 git）

- `tests/env/test_config_and_runtime.py`：`to_dict` 最小键序 / 带 layers+crs / 无 zmin/zmax/referer /
  roundtrip，2 条。
- `tests/core/domain/basemap/test_basemap.py`：纯 uri —— 服务根拼 /0、显式 layers 拼 /<id>、
  已带 /FeatureServer/5 不重复、中文 percent 编码幂等，4 条。
- `tests/ui/dialogs/test_custom_basemap.py`：collects_def / crs 可选 / 子图层非数字报错 /
  /MapServer 尾段报错，4 条。
- `tests/ui/dialogs/test_edit_basemap.py`：arcgisfeature 预填 roundtrip，1 条。

结果：非 GUI+dialogs 91、docks GUI 15、main_window GUI 18 全绿（EXIT=0）。

## 验证（Step-0 均对公共开放服务，未碰 127.0.0.1）

- 公开 SF311：`crs='EPSG:4326' url='…/SF311/FeatureServer/0'` → valid、nfeat=9704、字段可读；
  crs 传错（3857→4326 服务）坐标正确重投影不位移；crs 留空自动识别 → EPSG:4326。
- E2E：`build_online_basemap_layer` 对 arcgisfeature def（crs 空）冷启动 4.1s 返回 valid 矢量层、
  几何+字段可读。

## 用户侧手动验证（需服务开 Feature Access，有并列 /FeatureServer 端点）

1. 自定义底图选「ArcGIS FeatureServer 矢量要素」，地址填
   `http://127.0.0.1:6080/arcgis/rest/services/江西省县级行政区域/FeatureServer`，
   坐标系留空（自动识别）→ 保存 → 双击进画布 → 应为矢量层。
2. 「属性」工具点县界 → Dock 弹出要素属性（矢量路径）。← 本轮目标。
3. 编辑连接重开预填正常；与既有 arcgis 栅格条目并存互不判重。

## 注意

- **外部改动**：config/online_basemap_config.yaml 最末「自定义底图 / arcgis」条目已在会话中被人改为
  `type: arcgisfeature` + `url: …/FeatureServer/0`（疑似用户直接转成要素类型试跑），且残留
  `zmin: 0 / zmax: 18`（arcgisfeature 加载时忽略，但下次「编辑连接」保存会剥掉）+ name 仍叫 arcgis。
  按系统提示当用户有意改动、**未回退**，已向用户说明。
- git 改动留 dev 未提交（整条 在线底图 功能线：6 tracked M + 一批 untracked 新模块/档案）。
