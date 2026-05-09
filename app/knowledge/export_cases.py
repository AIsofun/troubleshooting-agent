"""
知识包导出 CLI — 将验证后的案例打包为可分发的知识包。

用途：
  - 将当前 site（现场）已验证的经验上传到总部经验中台
  - 总部汇总后生成 kp_YYYY_MM.zip 分发给各现场
  - 各现场通过 `python -m app.knowledge.ingest <kp_dir>` 导入

输出结构：
  knowledge_packs/
  └── kp_2026_05/
      ├── cases.json      ← CaseRecord 列表（可选脱敏）
      └── metadata.json   ← { version, count, sha256, exported_at, exported_by }
  knowledge_packs/kp_2026_05.zip  ← 打包版本（与目录同步生成）

用法：
  python -m app.knowledge.export_cases --kp-version kp_2026_05
  python -m app.knowledge.export_cases --kp-version kp_2026_05 --anonymize --out ./exports/
  python -m app.knowledge.export_cases --kp-version kp_2026_05 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── 脱敏处理 ─────────────────────────────────────────────


def _anonymize(case: Dict[str, Any]) -> Dict[str, Any]:
    """
    脱敏处理：移除或模糊化可能含有客户信息的字段。
    仅当 --anonymize 标志开启时应用。
    """
    out = dict(case)
    # 移除现场标识
    out.pop("site_id", None)
    # 模糊 site_type（保留大类，去掉具体工厂）
    if out.get("site_type"):
        out["site_type"] = out["site_type"].split("_")[0]   # "factory_A" → "factory"
    # 移除工程师操作记录（含有人员信息）
    if out.get("device_context") and isinstance(out["device_context"], dict):
        out["device_context"].pop("agent_version", None)
    # 标记为已脱敏
    out["sensitive_level"] = "anonymized"
    return out


# ── 从磁盘 cases/pending/ 导出（无 DB 模式）────────────


def _export_from_disk(
    cases_dir: Path,
    anonymize: bool = False,
) -> List[Dict[str, Any]]:
    """从 cases/verified/ 目录读取已验证案例（无 DB 时降级使用）。"""
    verified_dir = cases_dir / "verified"
    if not verified_dir.exists():
        return []

    cases = []
    for p in sorted(verified_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if anonymize:
                data = _anonymize(data)
            cases.append(data)
        except Exception as exc:
            print(f"[WARN] 跳过无法解析的文件 {p.name}: {exc}", file=sys.stderr)
    return cases


# ── 从 DB 导出（有 DB 时优先）────────────────────────────


def _export_from_db(
    kp_version: Optional[str],
    anonymize: bool = False,
    status: str = "verified",
) -> List[Dict[str, Any]]:
    """从 agent_cases 表导出指定状态的案例。"""
    from app.persistence.db import get_session, is_db_available
    from app.cases.case_repo import CaseRepository

    if not is_db_available():
        return []   # 调用方会 fallback 到磁盘模式

    cases: List[Dict[str, Any]] = []
    with get_session() as session:
        rows = CaseRepository.list(
            session,
            limit=10_000,
            case_status=status,
        )
    for row in rows:
        if anonymize:
            row = _anonymize(row)
        cases.append(row)
    return cases


# ── 写入知识包目录 ─────────────────────────────────────


def _write_pack(
    out_dir: Path,
    kp_version: str,
    cases: List[Dict[str, Any]],
    dry_run: bool = False,
) -> Path:
    """
    将案例列表写入 knowledge_packs/{kp_version}/ 并生成 zip。
    返回 zip 文件路径。
    """
    pack_dir = out_dir / kp_version
    zip_path = out_dir / f"{kp_version}.zip"

    if dry_run:
        print(f"[DRY-RUN] 将写入 {pack_dir} ({len(cases)} 条案例)")
        print(f"[DRY-RUN] 将生成 {zip_path}")
        return zip_path

    pack_dir.mkdir(parents=True, exist_ok=True)

    # 写 cases.json
    cases_path = pack_dir / "cases.json"
    cases_json = json.dumps(cases, ensure_ascii=False, indent=2, default=str)
    cases_path.write_text(cases_json, encoding="utf-8")

    # 计算 sha256 校验和
    sha256 = hashlib.sha256(cases_json.encode()).hexdigest()

    # 写 metadata.json
    meta = {
        "version": kp_version,
        "count": len(cases),
        "sha256": sha256,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by": os.environ.get("USERNAME", os.environ.get("USER", "unknown")),
        "format_version": "1.0",
    }
    (pack_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 打 zip
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in pack_dir.iterdir():
            zf.write(f, arcname=f"{kp_version}/{f.name}")

    return zip_path


# ── 主入口 ────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="导出验证后的经验案例为知识包（.zip）",
    )
    parser.add_argument(
        "--kp-version",
        required=True,
        help="知识包版本号，例如 kp_2026_05",
    )
    parser.add_argument(
        "--out",
        default="./knowledge_packs",
        help="输出目录（默认 ./knowledge_packs）",
    )
    parser.add_argument(
        "--status",
        default="verified",
        choices=["verified", "candidate"],
        help="导出的 case_status（默认 verified）",
    )
    parser.add_argument(
        "--anonymize",
        action="store_true",
        help="脱敏处理：移除现场标识和工程师信息",
    )
    parser.add_argument(
        "--cases-dir",
        default="./cases",
        help="本地 cases 目录（DB 不可用时从磁盘读取，默认 ./cases）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式，不实际写入文件",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    cases_dir = Path(args.cases_dir)

    # 初始化 DB（可选）
    try:
        from app.persistence.db import init_db
        init_db()
    except Exception:
        pass

    # 优先从 DB 导出，DB 不可用时 fallback 到磁盘
    cases = _export_from_db(args.kp_version, anonymize=args.anonymize, status=args.status)
    source = "database"
    if not cases:
        cases = _export_from_disk(cases_dir, anonymize=args.anonymize)
        source = "disk"

    if not cases:
        print(f"[WARN] 没有找到状态为 '{args.status}' 的案例，知识包为空。")
    else:
        print(f"[INFO] 导出来源: {source}，共 {len(cases)} 条案例")

    zip_path = _write_pack(
        out_dir=out_dir,
        kp_version=args.kp_version,
        cases=cases,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        print(f"[OK] 知识包已生成: {zip_path}")
        print(f"     包含 {len(cases)} 条案例，版本 {args.kp_version}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
