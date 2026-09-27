# 巨灾保险理赔调度系统

标准库实现的巨灾理赔受理、分级、查勘、复核、紧急预付和最终核定服务，数据保存到 SQLite。

## 代码结构（数据 / 规则 / 接口 / 页面分离）

- `data.py`：数据层，SQLite 表结构、连接和旧库增量迁移；
- `rules.py`：规则层，纯函数实现灾区围栏（haversine 距离 + 半径）、拍摄时间与灾情时刻先后、同设备 60 分钟内 100 公里以上跨区判定；
- `service.py`：服务层，事务、状态机、角色守卫、审计和查询；
- `app.py`：HTTP 接口层，只做路由、请求解析和 JSON 序列化（`CatastropheClaimService`/`DomainError` 仍从此导出）；
- `static/index.html`：只读页面，展示每案证据核验状态、主管待核验清单和已登记灾区。

## 运行

要求 Python 3.11+（Python 3.9 环境亦可，使用 `from __future__ import annotations`）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8207`，默认数据库 `catastrophe_claims.db`。可通过 `--db`、`--host`、`--port` 修改。旧数据库首次打开时会自动补齐证据核验相关列，历史证据默认视为已核验、已锁定。

## 主要接口

请求头 `X-User`、`X-Role` 表示用户与角色。角色有 `intake`、`adjuster`、`surveyor`、`supervisor`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/queue`
- `POST /api/claims`：创建报案并识别重复报案
- `POST /api/claims/triage`：计算优先级和欺诈风险；同时传 `zone_latitude`、`zone_longitude`、`zone_radius_km`、`disaster_time`（ISO 8601）时登记该灾情的灾区中心、灾情时刻和半径
- `POST /api/zones`：主管单独登记/更新灾区（字段同上，另需 `event_id`、`name`）
- `POST /api/claims/assign`：分配查勘人员
- `POST /api/evidence`：添加证据。补录现场证据时传 `latitude`、`longitude`、`captured_at`、`device_id`（四项必须同时给齐；只给哈希维持旧行为）。命中以下任一规则进入 `pending` 待核验：
  1. 拍摄位置距灾区中心超过半径（落在灾区外）；
  2. 拍摄时间早于灾情时刻；
  3. 同一设备号 60 分钟内在相距 100 公里以上的其他现场出现。
- `GET /api/evidence/pending`：主管/审计查看待核验清单（含距灾区中心距离、命中信息）
- `POST /api/evidence/verify`：主管核验，`decision` 为 `confirmed`（确认通过并锁定证据）或 `rejected`（驳回必须填 `reason`，原始证据及定位信息原样保留，状态锁定为已驳回）
- `POST /api/claims/survey`、`POST /api/claims/submit-review`：存在待核验证据时提交核损被 409 拦截
- `POST /api/claims/emergency-advance`：仅限监督人员、紧急且未超20%的案件
- `POST /api/claims/finalize`：锁定最终核定结果

证据状态：`pending`（待核验，未锁定）、`verified`（核验通过，已锁定）、`rejected`（已驳回，已锁定）。案件查询结果中的 `evidence_summary` 给出每案三态计数。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别、角色权限，以及灾区围栏/灾情时刻/同设备跨区三条核验规则、主管确认驳回与提交核损拦截。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；经纬度、拍摄时间和设备号由现场端上报，服务端不校验其来源真实性；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
