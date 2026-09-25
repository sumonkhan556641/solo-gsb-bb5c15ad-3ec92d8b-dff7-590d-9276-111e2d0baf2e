"""FastAPI 入口：胶片档案扫描整理 API。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from sqlite3 import Connection

from . import database, schemas, services


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即建库建表（幂等），依赖与数据库初始化都纳入启动流程
    database.init_db()
    yield


app = FastAPI(
    title="胶片档案扫描整理 API",
    description="按胶卷汇总扫描帧、去重、统计预期范围内的连续缺帧；批次导入幂等；支持误导入批次撤销、补扫任务认领与补扫完成回执；支持交付快照及相对快照的区间级差异对比；支持单批次采集质量审查（批内重复率、越界帧、预期内缺口及稳定告警代码）；支持批次导入预检（持久化预检单、确认时在同一写事务内复核创建时的单调批次修订号，任何实际批次导入/撤销均使旧预检 409 失效）；支持扫描瑕疵复核（按原始帧列表 0 起算位置登记模糊/划痕/曝光异常，实时计算全出现被标记的瑕疵队列）；支持胶卷交付核验（同一事务内核验三类阻断项，全空时签发冻结核验依据与状态指纹的持久化凭证，凭证按当前批次与瑕疵标记重算有效性，历史凭证不改写）；支持胶卷母版选片（在请求区间与当前预期覆盖的交集内逐帧剔除瑕疵出现，按最少批次切换选择每帧母版来源，返回未扫描区间与无合格候选帧，缺帧只做区间运算）；支持批次帧号整体偏移诊断（按其余存活批次重建实扫基线与缺帧，区间事件扫描求最优整体偏移，严格优于零偏移才给出建议）。",
    version="1.12.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=schemas.HealthOut, tags=["系统"])
def health():
    return {"status": "ok", "db": str(database.get_db_path())}


@app.post(
    "/batches",
    response_model=schemas.BatchResult,
    status_code=status.HTTP_201_CREATED,
    tags=["批次"],
)
def submit_batch(
    payload: schemas.BatchIn,
    response: Response,
    conn: Connection = Depends(database.get_db),
):
    """提交一个扫描批次。相同内容重复提交返回 200 且不改变任何结果。"""
    try:
        result, code = services.submit_batch(conn, payload)
    except services.BatchIdConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"batch_id '{e.batch_id}' 已被其他内容占用，请更换后重试",
        )
    response.status_code = code
    return result


@app.post(
    "/batch-prechecks",
    response_model=schemas.BatchPrecheckOut,
    status_code=status.HTTP_201_CREATED,
    tags=["批次导入预检"],
)
def create_batch_precheck(
    payload: schemas.BatchIn,
    conn: Connection = Depends(database.get_db),
):
    """批次导入预检：提交与 `/batches` 完全相同的数据，生成持久化预检单但不写入批次。

    返回相对**当前胶卷状态**的预演：新增帧、重复帧（批内重复 + 胶卷已有）、
    导入后的合并预期范围与缺帧区间；预检单保留原始帧顺序、创建时的胶卷状态
    指纹与**批次修订号**。可凭预检单 ID 调用确认接口；确认前该胶卷若有批次
    实际导入或撤销（修订号前进，即使汇总不变），确认返回 409。
    """
    return services.create_precheck(conn, payload)


@app.get(
    "/batch-prechecks/{precheck_id}",
    response_model=schemas.BatchPrecheckOut,
    tags=["批次导入预检"],
)
def get_batch_precheck(precheck_id: str, conn: Connection = Depends(database.get_db)):
    """查询预检单及其预演内容（含是否已确认）。未知预检单返回 404。"""
    result = services.get_precheck(conn, precheck_id.strip())
    if result is None:
        raise HTTPException(status_code=404, detail="预检单不存在")
    return result


@app.post(
    "/batch-prechecks/{precheck_id}/confirm",
    response_model=schemas.BatchPrecheckConfirmOut,
    tags=["批次导入预检"],
)
def confirm_batch_precheck(
    precheck_id: str,
    response: Response,
    conn: Connection = Depends(database.get_db),
):
    """确认预检单并执行导入。

    在**同一 SQLite 写事务**内复核预检单创建时的胶卷**批次修订号**（单调递增，
    批次实际导入或撤销才前进），随后执行与 `POST /batches` 相同的导入逻辑
    （含内容幂等）：

    - 创建后该胶卷有批次**实际导入或撤销**（即使汇总区间与帧集合不变，或
      新增后又撤销、汇总恢复）→ 修订号不一致，返回 `409 Conflict`，**不落库**
      （预检单保留，可重新预检）；
    - 旧库升级前创建、没有修订号的未确认预检单 → `409 Conflict`，需重新预检；
    - 首次确认成功返回导入结果（`201` 新建 / `200` 内容重复，与 `/batches` 一致）；
    - 同一预检单再次确认返回**首次导入冻结的结果**（`200`，`confirmed=true`）；
    - 未知预检单 → `404`；预检携带的 `batch_id` 被其他内容占用 → `409`。
    """
    try:
        result, code = services.confirm_precheck(conn, precheck_id)
    except services.PrecheckNotFoundError:
        raise HTTPException(status_code=404, detail="预检单不存在")
    except services.PrecheckLegacyRevisionError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "该预检单由旧版服务在批次修订号机制上线前创建，无法安全复核，"
                "请重新调用 POST /batch-prechecks 生成新预检单后再确认；本次未写入任何数据"
            ),
        )
    except services.PrecheckStaleError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "预检单创建后该胶卷批次已变化（有批次实际导入或撤销，含汇总不变的"
                "内容不同批次、或新增后又撤销的情况），批次修订号不一致，请重新预检后"
                "再确认；本次未写入任何数据"
            ),
        )
    except services.BatchIdConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"batch_id '{e.batch_id}' 已被其他内容占用，请更换后重试",
        )
    response.status_code = code
    return result


@app.get("/batches/{batch_id}", response_model=schemas.BatchResult, tags=["批次"])
def get_batch(batch_id: str, conn: Connection = Depends(database.get_db)):
    result = services.get_batch(conn, batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return result


@app.get(
    "/batches/{batch_id}/quality",
    response_model=schemas.BatchQualityOut,
    tags=["批次"],
)
def get_batch_quality(batch_id: str, conn: Connection = Depends(database.get_db)):
    """审查单个扫描批次的采集质量（只读，不修改批次或胶卷汇总）。

    在一次只读事务中读取该批保存的原始帧列表与预期闭区间，按原始顺序计算：
    批内重复率、落在预期外的帧、最长连续扫到段、预期范围内的最长缺口、
    原始列表压缩成连续段后的段数，并给出按重复率、越界帧和缺口长度触发的
    稳定告警代码。批次不存在返回 404；旧版批次缺少原始帧列表时按旧数据
    规则返回 409。
    """
    try:
        return services.get_batch_quality(conn, batch_id.strip())
    except services.BatchNotFoundError:
        raise HTTPException(status_code=404, detail="批次不存在")
    except services.LegacyBatchDataError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "该批次为旧版数据，缺少原始帧列表，无法按原始顺序评估采集质量"
                f"（相关批次：{', '.join(e.legacy_batch_ids)}）。"
            ),
        )


@app.get(
    "/batches/{batch_id}/offset-suggestion",
    response_model=schemas.OffsetSuggestionOut,
    tags=["批次"],
)
def get_batch_offset_suggestion(
    batch_id: str,
    max_shift: int = Query(
        ...,
        ge=1,
        le=1_000_000_000,
        description="允许的最大偏移绝对值（帧数），1～1000000000 的整数",
    ),
    conn: Connection = Depends(database.get_db),
):
    """诊断该批帧号是否发生整体偏移（只读，不写库）。

    以该批原始帧去重集合为待校准数据，保持胶卷当前合并预期范围不变，
    按其余存活批次重建实扫基线与缺帧；在 [−max_shift, +max_shift] 的整数
    偏移中仅考虑平移后帧号全部非负的方案，用区间事件扫描求落入基线缺帧的
    最大不同帧数（并列取绝对值最小、再取偏移值较小），不逐一枚举偏移或
    展开巨大预期跨度。仅当最优非零偏移严格优于 0 时返回建议（含偏移、
    原/新覆盖数与新编号撞上其余批次的帧数），否则返回无建议。
    批次不存在返回 404；旧版批次缺少原始帧列表返回 409。
    """
    try:
        return services.get_offset_suggestion(conn, batch_id.strip(), max_shift)
    except services.BatchNotFoundError:
        raise HTTPException(status_code=404, detail="批次不存在")
    except services.LegacyBatchDataError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "该批次为旧版数据，缺少原始帧列表，无法做帧号整体偏移诊断"
                f"（相关批次：{', '.join(e.legacy_batch_ids)}）。"
            ),
        )


@app.put(
    "/batches/{batch_id}/defects",
    response_model=schemas.BatchDefectsOut,
    tags=["扫描瑕疵复核"],
)
def put_batch_defects(
    batch_id: str,
    payload: schemas.BatchDefectsIn,
    conn: Connection = Depends(database.get_db),
):
    """提交并**整体替换**该批原始帧列表上的瑕疵复核标记。

    请求体给出该批全部瑕疵项：``position`` 为该批**原始帧列表**中 0 起算的
    位置，``defect_type`` 为模糊 / 划痕 / 曝光异常之一；重复提交以本次内容
    整体替换旧标记，提交空列表即清空。位置越界或重复返回 422（数据不变）；
    批次不存在返回 404；旧版批次缺少原始帧列表、无法按位置定位时返回 409。
    """
    try:
        return services.put_batch_defects(conn, batch_id, payload)
    except services.BatchNotFoundError:
        raise HTTPException(status_code=404, detail="批次不存在")
    except services.LegacyBatchDataError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "该批次为旧版数据，缺少原始帧列表，无法按 0 起算位置登记瑕疵"
                f"（相关批次：{', '.join(e.legacy_batch_ids)}）。"
            ),
        )
    except services.DefectPositionError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.delete(
    "/batches/{batch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["批次"],
)
def delete_batch(batch_id: str, conn: Connection = Depends(database.get_db)):
    """撤销一个误导入的批次。

    删除该批并按剩余批次原导入顺序重建预期范围、帧归属及各批新增/重复明细
    （共享帧不会丢失，批内重复保留）。批次不存在返回 404；若剩余批次为无法
    区分批内/跨批重复的旧数据，返回 409 且数据不变。
    """
    try:
        services.delete_batch(conn, batch_id.strip())
    except services.BatchNotFoundError:
        raise HTTPException(status_code=404, detail="批次不存在")
    except services.LegacyBatchDataError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "撤销被拒绝：该胶卷存在旧版格式的历史批次，缺少原始帧列表，"
                f"无法区分批内与跨批重复（相关批次：{', '.join(e.legacy_batch_ids)}）；"
                "为保证重建结果准确，本次未改动任何数据。"
            ),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/rolls", response_model=list[schemas.RollSummary], tags=["胶卷"])
def list_rolls(conn: Connection = Depends(database.get_db)):
    """列出所有胶卷及其汇总状态。"""
    return services.list_rolls(conn)


@app.get("/rolls/{roll_no}", response_model=schemas.RollState, tags=["胶卷"])
def get_roll(roll_no: str, conn: Connection = Depends(database.get_db)):
    """查询单个胶卷的合并帧号、重复帧明细与连续缺帧区间。"""
    result = services.get_roll_state(conn, roll_no.strip())
    if result is None:
        raise HTTPException(status_code=404, detail="胶卷不存在（尚未导入任何批次）")
    return result


@app.get(
    "/rolls/{roll_no}/frame-provenance",
    response_model=schemas.FrameProvenanceOut,
    tags=["胶卷"],
)
def get_frame_provenance(
    roll_no: str,
    start: int = Query(..., ge=0, description="帧号区间起始（含），非负整数"),
    end: int = Query(..., ge=0, description="帧号区间结束（含），非负整数，end ≥ start"),
    limit: int = Query(..., ge=1, description="每页最多返回的帧数（正整数）"),
    cursor: int | None = Query(
        None, ge=0, description="游标：上一页末帧号；续页只取帧号更大的帧"
    ),
    conn: Connection = Depends(database.get_db),
):
    """胶卷帧号扫描溯源：追查区间内每个实际存在的帧由哪些批次扫描。

    只按**实际存在的帧号**升序键集分页（不逐帧遍历区间跨度），每页最多
    ``limit`` 帧，``next_cursor`` 为下一页游标（上一页末帧号）。每帧返回：
    最早有效批次（首扫归属）、按批次**原导入顺序**排列的参与批次及各批
    **原始列表中的出现次数**（旧批次无原始列表时为 ``null``，不推断），
    以及该帧是否位于当前预期覆盖内。撤销批次后按剩余批次实时重算，
    首扫归属随之转移。参数无效（含 end < start）返回 422，胶卷不存在返回 404。
    """
    if end < start:
        raise HTTPException(status_code=422, detail="end 不能小于 start")
    try:
        return services.frame_provenance(
            conn, roll_no.strip(), start, end, limit, cursor
        )
    except services.RollNotFoundError:
        raise HTTPException(status_code=404, detail="胶卷不存在（尚未导入任何批次）")
    except services.ProvenanceRangeError:
        raise HTTPException(status_code=422, detail="end 不能小于 start")


@app.get(
    "/rolls/{roll_no}/master-picks",
    response_model=schemas.MasterPicksOut,
    tags=["胶卷母版选片"],
)
def get_roll_master_picks(
    roll_no: str,
    start: int = Query(..., ge=0, description="帧号区间起始（含），非负整数"),
    end: int = Query(..., ge=0, description="帧号区间结束（含），非负整数，end ≥ start"),
    conn: Connection = Depends(database.get_db),
):
    """胶卷母版选片清单：只处理请求闭区间与当前预期覆盖的交集。

    逐帧从存活批次的扫描出现中剔除已登记瑕疵的位置：同批同帧有多个合格出现时
    取原始列表最早位置；旧批次（无原始列表）的去重帧视为一个位置为空（null）
    的合格候选。对每段连续可选帧选择**批次切换次数最少**的来源序列；并列时按
    帧号顺序逐帧比较批次原导入顺序、再比较原始位置，返回每帧的批次号与位置。

    另返回交集内的**未扫描闭区间**及**已扫描却无合格候选**的帧号（该帧全部
    扫描出现均已被标记瑕疵）；缺帧用区间集合运算得出，不展开巨大帧号跨度。
    查询只读并随批次撤销、瑕疵变动实时更新。参数无效（含 end < start）返回
    422，胶卷不存在（尚未导入任何批次，或最后一批已被撤销）返回 404。
    """
    if end < start:
        raise HTTPException(status_code=422, detail="end 不能小于 start")
    try:
        return services.get_master_picks(conn, roll_no.strip(), start, end)
    except services.RollNotFoundError:
        raise HTTPException(status_code=404, detail="胶卷不存在（尚未导入任何批次）")
    except services.MasterPickRangeError:
        raise HTTPException(status_code=422, detail="end 不能小于 start")


@app.get(
    "/rolls/{roll_no}/defect-queue",
    response_model=schemas.DefectQueueOut,
    tags=["扫描瑕疵复核"],
)
def get_roll_defect_queue(
    roll_no: str, conn: Connection = Depends(database.get_db)
):
    """胶卷瑕疵复核队列：当前胶卷中**所有有效扫描出现均被标记瑕疵**的帧。

    按存活批次的原始帧列表逐出现实时重算：同帧任一出现未被标记（所在批次
    未提交瑕疵，或该批只有部分位置被标记）即不入队；未提交瑕疵的批次视为
    全部未标记；旧批次只有去重帧集合，其帧按一次未标记出现处理。批次撤销后
    只按存活批次重算，被撤销批次的瑕疵标记随之清理。胶卷不存在（尚未导入
    任何批次，或最后一批已被撤销）返回 404。
    """
    try:
        return services.get_defect_queue(conn, roll_no.strip())
    except services.RollNotFoundError:
        raise HTTPException(status_code=404, detail="胶卷不存在（尚未导入任何批次）")


@app.post(
    "/rolls/{roll_no}/delivery-checks",
    response_model=schemas.DeliveryCheckOut,
    status_code=status.HTTP_201_CREATED,
    tags=["胶卷交付核验"],
)
def create_delivery_check(
    roll_no: str,
    response: Response,
    conn: Connection = Depends(database.get_db),
):
    """申请胶卷交付核验：在同一 SQLite 事务内读取预期覆盖、实扫帧、存活批次
    与瑕疵标记，返回三类阻断项——预期内缺帧区间、预期外帧、全部扫描出现均被
    标记瑕疵的帧。

    - 任一阻断项非空：不签发、不写库，返回 ``200``（``issued=false``）及三类阻断项；
    - 三类全空：签发持久化凭证（``201``），冻结核验依据及包含批次身份、瑕疵
      位置与类型的状态指纹；
    - 同一状态重复申请返回原凭证（``200``、``repeated=true``）；
    - 胶卷不存在（尚未导入任何批次，或最后一批已被撤销）返回 ``404``。
    """
    try:
        result, code = services.create_delivery_check(conn, roll_no.strip())
    except services.RollNotFoundError:
        raise HTTPException(status_code=404, detail="胶卷不存在（尚未导入任何批次）")
    response.status_code = code
    return result


@app.get(
    "/delivery-checks/{check_id}",
    response_model=schemas.DeliveryCheckDetailOut,
    tags=["胶卷交付核验"],
)
def get_delivery_check(check_id: str, conn: Connection = Depends(database.get_db)):
    """返回交付核验凭证的签发依据，并按当前状态重算有效性。

    历史凭证只读、不改写：批次导入/撤销或瑕疵标记变化即
    ``currently_valid=false``（同时给出按当前状态重算的三类阻断项）；
    胶卷最后一批被撤销后当前按空状态处理，凭证失效但仍可查询。
    凭证不存在返回 ``404``。
    """
    try:
        return services.get_delivery_check(conn, check_id.strip())
    except services.DeliveryCheckNotFoundError:
        raise HTTPException(status_code=404, detail="交付核验凭证不存在")


@app.post(
    "/rolls/{roll_no}/snapshots",
    response_model=schemas.RollSnapshotOut,
    status_code=status.HTTP_201_CREATED,
    tags=["交付快照"],
)
def create_roll_snapshot(
    roll_no: str, conn: Connection = Depends(database.get_db)
):
    """为胶卷创建交付快照：在一致性读中固化当时的预期闭区间、实扫帧与缺帧闭区间。

    返回可长期查询的快照 ID；快照不随批次导入、撤销或补扫改变。
    胶卷不存在（尚未导入任何批次）返回 404。
    """
    try:
        return services.create_snapshot(conn, roll_no.strip())
    except services.RollNotFoundError:
        raise HTTPException(status_code=404, detail="胶卷不存在（尚未导入任何批次）")


@app.get(
    "/rolls/{roll_no}/snapshots/{snapshot_id}/diff",
    response_model=schemas.SnapshotDiffOut,
    tags=["交付快照"],
)
def get_roll_snapshot_diff(
    roll_no: str, snapshot_id: str, conn: Connection = Depends(database.get_db)
):
    """返回胶卷当前状态相对快照的差异。

    预期覆盖、实扫帧、缺帧三个维度独立比较，分别给出相对快照新增与消失的
    闭区间（按区间做集合运算）。即使胶卷最后一批已被撤销，只要快照属于该
    胶卷编号，仍返回差异（当前按空状态处理，roll_exists=false）。未知快照
    ID 或快照与胶卷编号不匹配返回 404。
    """
    try:
        return services.get_snapshot_diff(conn, roll_no.strip(), snapshot_id.strip())
    except services.SnapshotNotFoundError:
        raise HTTPException(status_code=404, detail="快照不存在或不属于该胶卷")


@app.post(
    "/rolls/{roll_no}/rescan-claims",
    response_model=schemas.RescanClaimOut,
    status_code=status.HTTP_201_CREATED,
    tags=["补扫认领"],
)
def create_rescan_claim(
    roll_no: str,
    payload: schemas.RescanClaimIn,
    conn: Connection = Depends(database.get_db),
):
    """认领一段缺帧用于补扫。

    当前缺帧区间扣除所有未到期认领后，按帧号升序分配首段不超过
    ``max_frames`` 的连续闭区间。胶卷不存在返回 404，暂无可领取帧返回 409。
    """
    try:
        return services.create_rescan_claim(conn, payload, roll_no.strip())
    except services.RollNotFoundError:
        raise HTTPException(status_code=404, detail="胶卷不存在（尚未导入任何批次）")
    except services.NoClaimableFramesError:
        raise HTTPException(status_code=409, detail="当前没有可领取的缺帧（可能已被其他未到期任务认领）")


@app.get(
    "/rolls/{roll_no}/rescan-claims",
    response_model=list[schemas.RescanClaimOut],
    tags=["补扫认领"],
)
def list_rescan_claims(
    roll_no: str, conn: Connection = Depends(database.get_db)
):
    """列出该胶卷全部未到期的补扫认领任务，及其帧段内仍缺失的帧段。"""
    result = services.list_rescan_claims(conn, roll_no.strip())
    if result is None:
        raise HTTPException(status_code=404, detail="胶卷不存在（尚未导入任何批次）")
    return result


@app.delete(
    "/rescan-claims/{claim_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["补扫认领"],
)
def release_rescan_claim(
    claim_id: str, conn: Connection = Depends(database.get_db)
):
    """释放（删除）一个补扫认领任务；任务不存在、已释放、已到期或已结案返回 404。"""
    if not services.release_rescan_claim(conn, claim_id.strip()):
        raise HTTPException(status_code=404, detail="补扫任务不存在、已释放、已到期或已结案")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/rescan-claims/{claim_id}/receipts",
    response_model=schemas.RescanReceiptOut,
    status_code=status.HTTP_201_CREATED,
    tags=["补扫认领"],
)
def submit_rescan_receipt(
    claim_id: str,
    payload: schemas.RescanReceiptIn,
    response: Response,
    conn: Connection = Depends(database.get_db),
):
    """提交补扫完成回执：认领 ID、补扫批次号与实际扫到的非空帧列表。

    以认领闭区间作为该补扫批次的预期范围，批次导入、回执持久化与认领结案
    在同一 SQLite 写事务内完成。只接受未到期、未释放、未结案的认领；帧号须在
    认领闭区间内且提交时仍属该胶卷缺帧，否则 409 且数据不变。相同认领、批次号
    与原始帧列表的重试返回已存回执（200），不重复导入；变更内容或对已结案认领
    再提交返回 409。
    """
    try:
        result, code = services.submit_rescan_receipt(conn, claim_id, payload)
    except services.ClaimNotFoundError:
        raise HTTPException(status_code=404, detail="补扫任务不存在")
    except services.ClaimUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except services.ReceiptConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except services.BatchIdConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"batch_id '{e.batch_id}' 已被其他内容占用，请更换后重试",
        )
    response.status_code = code
    return result


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
