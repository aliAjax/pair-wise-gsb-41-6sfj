"""现场证据核验规则：灾区范围、拍摄时间、设备轨迹的纯函数判定。"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

DEVICE_WINDOW_MINUTES = 60
DEVICE_JUMP_KM = 100.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def parse_instant(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s必须是 ISO 8601 时间字符串" % label)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("%s格式无效" % label) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def evaluate_field_evidence(zone: dict[str, Any] | None, captured_at: str,
                            latitude: float, longitude: float, device_id: str,
                            sightings: list[dict[str, Any]]) -> list[dict[str, str]]:
    """对一条现场证据执行核验规则，返回触发的标记列表（空列表表示通过）。

    zone: 灾区登记 {"center_lat","center_lon","disaster_time","radius_km"}，未登记则为 None。
    sightings: 同一设备此前全部现场证据 [{"latitude","longitude","captured_at"}]。
    """
    flags: list[dict[str, str]] = []
    captured = parse_instant(captured_at, "拍摄时间")
    if zone:
        distance = haversine_km(latitude, longitude, zone["center_lat"], zone["center_lon"])
        if distance > zone["radius_km"]:
            flags.append({
                "code": "outside_zone",
                "message": "拍摄位置距灾区中心 %.1f 公里，超出登记半径 %.1f 公里" % (distance, zone["radius_km"]),
            })
        disaster_at = parse_instant(zone["disaster_time"], "灾情时刻")
        if captured < disaster_at:
            flags.append({
                "code": "before_disaster",
                "message": "拍摄时间 %s 早于灾情时刻 %s" % (captured_at, zone["disaster_time"]),
            })
    if device_id:
        for sighting in sightings:
            other = parse_instant(sighting["captured_at"], "历史拍摄时间")
            minutes = abs((captured - other).total_seconds()) / 60.0
            if minutes > DEVICE_WINDOW_MINUTES:
                continue
            jump = haversine_km(latitude, longitude, sighting["latitude"], sighting["longitude"])
            if jump > DEVICE_JUMP_KM:
                flags.append({
                    "code": "device_jump",
                    "message": "设备 %s 在 %d 分钟内出现在相距 %.1f 公里的另一现场" % (device_id, round(minutes), jump),
                })
                break
    return flags
