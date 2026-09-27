# 巨灾保险理赔调度系统

标准库实现的巨灾理赔受理、分级、查勘、复核、紧急预付和最终核定服务，数据保存到 SQLite。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8207`，默认数据库 `catastrophe_claims.db`。可通过 `--db`、`--host`、`--port` 修改。

## 主要接口

请求头 `X-User`、`X-Role` 表示用户与角色。角色有 `intake`、`adjuster`、`surveyor`、`supervisor`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/queue`
- `POST /api/claims`：创建报案并识别重复报案
- `POST /api/claims/triage`：计算优先级和欺诈风险，可同时登记灾区（`disaster_lat`、`disaster_lon`、`disaster_time`、`radius_km`）
- `POST /api/claims/disaster-zone`：登记或更新灾区中心、灾情时刻和半径（仅 supervisor）
- `POST /api/claims/assign`：分配查勘人员
- `POST /api/evidence`：添加证据并识别跨案件批量复用；现场证据补录 `latitude`、`longitude`、`captured_at`、`device_id` 后自动核验
- `POST /api/evidence/verify`：主管确认（`confirm`）或驳回（`reject`，必须填写原因）待核验证据，处理后证据锁定
- `GET /api/verifications/pending`：待核验证据清单（supervisor、auditor）
- `POST /api/claims/survey`、`POST /api/claims/submit-review`
- `POST /api/claims/emergency-advance`：仅限监督人员、紧急且未超20%的案件
- `POST /api/claims/finalize`：锁定最终核定结果

## 现场证据核验

规则在 `verification_rules.py`，数据读写和迁移在 `verification_store.py`，接口在 `app.py`，展示在 `static/index.html`。现场证据触发以下任一规则时进入待核验（`pending`）：

- 拍摄位置距灾区中心超过登记半径（`outside_zone`）
- 拍摄时间早于灾情时刻（`before_disaster`）
- 同一设备 60 分钟内在相距 100 公里以上的现场出现（`device_jump`）

未触发规则的现场证据自动通过并锁定（`verified`）。案件存在待核验证据时不能提交核损；主管确认后证据锁定，驳回必须写明原因且原始证据保留（`rejected`）。页面展示每案核验状态和待核验清单。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别、角色权限，以及现场证据核验（灾区登记、灾区外/早于灾情/设备跨区标记、提交核损拦截、确认锁定与驳回留痕）。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
