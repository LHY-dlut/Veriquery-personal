#!/usr/bin/env python3
"""参数证据链路单元测试（T01–T19）。

全部**内存隔离**，不依赖 PDF 上传 / Chroma / 网络，可直接在 CI 跑。

    .venv/bin/python scripts/test_param_pipeline.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from core.param_resolver import (  # noqa: E402
    COND_CONFLICT, COND_MATCH, COND_UNKNOWN,
    ParamEvidence, _condition_match, _column_value, normalize_units,
    norm_number, norm_symbol, parse_param_query, select_value,
)
from ingestion.table_struct import (  # noqa: E402
    ParamRecord, _build_table, _expand_spans, extract_param_records, parse_tables,
)

RESULTS = []


def check(tid, desc, fn):
    try:
        fn()
        RESULTS.append((tid, desc, True, ""))
        print(f"  [PASS] {tid} {desc}")
    except AssertionError as e:
        RESULTS.append((tid, desc, False, str(e)))
        print(f"  [FAIL] {tid} {desc} — {e}")


# ---------------------------------------------------------------- 构件
def rec(**kw) -> ParamRecord:
    base = dict(param="", symbol="", conditions="", conditions_raw="", unit="mΩ",
                page=1, table_id="T1", row_index=1)
    base.update(kw)
    return ParamRecord(**base)


def ev(**kw) -> ParamEvidence:
    base = dict(param="", symbol="", conditions="", conditions_raw="",
                values={}, unit="mΩ", page=1, table_id="T1", row_index=1)
    base.update(kw)
    return ParamEvidence(**base)


RDS_ROWS = [
    ev(param="Drain-source on-state resistance", symbol="RDS(on)",
       conditions_raw="VGS=-4.5 V, ID=-12 A", conditions="VGS=-4.5 V, ID=-12 A",
       raw_row="... | RDS(on) | VGS=-4.5 V, ID=-12 A | - | 8.8 | 12 | mΩ",
       values={"typ": "8.8", "max": "12"}),
    ev(param="Drain-source on-state resistance", symbol="RDS(on)",
       conditions_raw="VGS=-10 V, ID=-14.9 A", conditions="VGS=-10 V, ID=-14.9 A",
       raw_row="... | RDS(on) | VGS=-10 V, ID=-14.9 A | - | 6.3 | 8.0",
       values={"typ": "6.3", "max": "8.0"}),
]


# ---------------------------------------------------------------- 用例
def t01_norm():
    assert norm_number("−10 V") == norm_number("-10V") == "-10v"
    assert norm_symbol("I D") == norm_symbol("I_D".replace("_", "")) == "id"
    assert norm_symbol("V GS") == "vgs"
    assert "μ" in normalize_units("µA") or normalize_units("µA") == "μa"
    assert norm_number("25 °C") == norm_number("25℃") == "25°c"


def t02_roundtrip():
    r = rec(param="P", symbol="S1", conditions="VGS=-10 V", conditions_raw="VGS=-10 V",
            min="1", typ="2", max="3", raw_row="raw line",
            value_columns=[("steady state", "12")], extra_values=["99"])
    m = r.to_metadata()
    r2 = ParamRecord.from_metadata(m)
    assert r2.valid, "roundtrip should be valid"
    assert r2.raw_row == "raw line"
    assert r2.conditions_raw == "VGS=-10 V"
    assert ("steady state", "12") in r2.value_columns
    assert r2.extra_values == ["99"]
    assert r2.max == "3"


def t03_old_schema_rejected():
    old = {"param": "P", "symbol": "S", "pmin": "1", "pmax": "2"}  # 无 raw_row/conditions_raw
    r = ParamRecord.from_metadata(old)
    assert not r.valid, "旧结构缺少条件证据，必须标记 invalid"


def t04_rowspan_colspan():
    # 2x2 合并: A 跨两行, B/C 第二行
    rows = [
        [("A", 1, 2), ("B", 1, 1)],
        [("C", 1, 1)],
    ]
    grid = _expand_spans(rows)
    assert grid[0] == ["A", "B"], grid
    assert grid[1] == ["A", "C"], grid
    # colspan 与 rowspan 同时出现
    rows2 = [[("X", 2, 2)], [("Y", 1, 1)]]
    g2 = _expand_spans(rows2)
    assert g2[0][:2] == ["X", "X"] and g2[1][:2] == ["X", "X"], g2


def t05_conditions_preserved():
    html = ("<table><tr><td>Parameter</td><td>Symbol</td><td>Conditions</td>"
            "<td>Min</td><td>Typ</td><td>Max</td><td>Unit</td></tr>"
            "<tr><td>RDS(on)</td><td>RDS(on)</td><td>VGS=-10 V, ID=-14.9 A</td>"
            "<td>-</td><td>6.3</td><td>8.0</td><td>mΩ</td></tr></table>")
    tbl = parse_tables(html)[0]
    recs = extract_param_records(tbl, 1, "T")
    assert recs, "应抽出记录"
    r = recs[0]
    assert "VGS=-10 V" in r.conditions, r.conditions
    assert "-14.9" in r.conditions_raw, r.conditions_raw
    assert "VGS= ," not in r.conditions, "条件中的数值不能被清空"


def t06_conflict_not_ok():
    pq = parse_param_query("VGS=-99 V 条件下 RDS(on) 最大值是多少？")
    sel = select_value(pq, RDS_ROWS)
    assert sel.status == "condition_conflict", sel.status
    assert sel.value is None


def t07_vgs45():
    pq = parse_param_query("VGS=-4.5 V，ID=-12 A 条件下 BSO301SP 的 RDS(on) 最大值是多少？")
    sel = select_value(pq, RDS_ROWS)
    assert sel.status == "ok" and sel.value == "12", (sel.status, sel.value)


def t08_vgs10():
    pq = parse_param_query("VGS=-10 V，ID=-14.9 A 条件下 BSO301SP 的 RDS(on) 最大值是多少？")
    sel = select_value(pq, RDS_ROWS)
    assert sel.status == "ok" and sel.value == "8.0", (sel.status, sel.value)


def t09_no_condition_ambiguous():
    pq = parse_param_query("BSO301SP 的 RDS(on) 最大值是多少？")
    sel = select_value(pq, RDS_ROWS)
    assert sel.status == "ambiguous", f"多工况无条件下必须歧义，实际 {sel.status}"
    assert len(sel.alternatives) == 2


def t10_id_vs_idss():
    rows = [
        ev(param="Zero gate voltage drain current", symbol="IDSS", conditions_raw="VDS=-30 V",
           values={"max": "-1"}, unit="µA"),
        ev(param="Zero gate voltage drain current", symbol="IDSS", conditions_raw="VDS=-25 V",
           values={"max": "-1"}, unit="µA"),
        ev(param="Zero gate voltage drain current", symbol="IDSS", conditions_raw="VDS=-20 V",
           values={"max": "-1"}, unit="µA"),
        ev(param="Continuous drain current", symbol="ID", conditions_raw="TA=25°C",
           values={"max": "-12.6"}, unit="A"),
    ]
    pq = parse_param_query("BSO301SP 的连续漏极电流 ID 是多少？")
    sel = select_value(pq, rows)
    assert sel.status == "ok", sel.status
    assert sel.evidence.symbol == "ID", f"选错记录: {sel.evidence.symbol}"
    assert sel.value == "-12.6"


def t11_missing_column_no_guess():
    one = ev(param="RDS(on)", symbol="RDS(on)", values={"typ": "5", "min": "1"})
    pq = parse_param_query("RDS(on) 的最大值是多少？")
    sel = select_value(pq, [one])
    assert sel.status == "missing_value", f"缺 max 不得用其他列顶替，实际 {sel.status}"


def t12_column_label():
    one = ev(param="Continuous drain current", symbol="ID",
             conditions_raw="TA=25°C", values={"steady state": "-12.6", "10 secs": "-14.9"},
             unit="A")
    pq = parse_param_query("TA=25℃ 稳态条件下连续漏极电流 ID 是多少？")
    sel = select_value(pq, [one])
    assert sel.status == "ok" and sel.value == "-12.6", (sel.status, sel.value)


def t13_status_rendered():
    for status in ("not_found", "missing_value", "condition_conflict", "ambiguous",
                   "unsupported_parameter"):
        from core.param_resolver import Selection
        s = Selection(status=status)
        assert s.render().strip(), f"{status} 必须有可读输出"


def t14_no_prefix_confusion():
    pq = parse_param_query("ID 是多少？")
    only_idss = [ev(param="Zero gate voltage drain current", symbol="IDSS",
                    values={"max": "-1"})]
    sel = select_value(pq, only_idss)
    assert sel.status != "ok", "ID 查询不得匹配 IDSS"


def t15_unknown_condition():
    # 记录条件残缺（键在、值缺失）
    broken = [ev(param="RDS(on)", symbol="RDS(on)", conditions="VGS=", conditions_raw="VGS=",
                 raw_row="RDS(on) | RDS(on) | VGS= | 1", values={"max": "5"})]
    pq = parse_param_query("VGS=-10 V 条件下 RDS(on) 的最大值是多少？")
    state = _condition_match(pq, broken[0])
    assert state == COND_UNKNOWN, state
    sel = select_value(pq, broken)
    assert sel.status == "ambiguous", f"条件不可核验时不得给确定值，实际 {sel.status}"


def t16_value_columns_kept():
    html = ("<table><tr><td>Parameter</td><td>Symbol</td><td>Conditions</td>"
            "<td>&le;10 secs</td><td>steady state</td><td>Unit</td></tr>"
            "<tr><td>Continuous drain current</td><td>ID</td><td>TA=25C</td>"
            "<td>-14.9</td><td>-12.6</td><td>A</td></tr></table>")
    tbl = parse_tables(html)[0]
    recs = extract_param_records(tbl, 1, "T")
    assert recs, "应抽出记录"
    r = recs[0]
    labels = [k for k, _ in r.value_columns]
    assert any("secs" in k.lower() for k in labels), f"自定义列标签丢失: {labels}"


def t17_render_has_source():
    pq = parse_param_query("VGS=-10 V，ID=-14.9 A 条件下 RDS(on) 最大值？")
    sel = select_value(pq, RDS_ROWS)
    text = sel.render()
    assert "VGS=-10 V" in text, "答案必须回显条件"
    assert "第1页" in text, "答案必须带来源页码"


def t18_identity_by_score():
    # 3 条弱匹配 + 1 条强匹配：应按匹配质量选身份，而不是按记录数
    weak = [ev(param="Other parameter drain current x", symbol="XX", values={"max": "1"})
            for _ in range(3)]
    strong = [ev(param="Drain-source on-state resistance", symbol="RDS(on)",
                 conditions_raw="VGS=-10 V", values={"max": "8.0"})]
    pq = parse_param_query("RDS(on) 最大值是多少？")
    sel = select_value(pq, weak + strong)
    assert sel.status == "ok" and sel.value == "8.0", (sel.status, sel.value)


def t19_empty():
    sel = select_value(parse_param_query("RDS(on) 最大值？"), [])
    assert sel.status == "not_found"


def t20_english_gate():
    """英文参数题必须能进入 Resolver（golden 评测全部是英文题）。"""
    from core.param_resolver import looks_like_param_query
    assert looks_like_param_query("What is the gain bandwidth of the OPA620?")
    assert looks_like_param_query("What is the slew rate of the OPA620?")
    assert not looks_like_param_query("What color is the logo?")
    assert not looks_like_param_query("首页标题是什么？")


def t21_multi_param():
    """多参数题（"上升时间和下降时间"）逐参数选值，两个身份都出结果。

    回归背景：单参数假设下 param_keys 是别名并集，身份选择只出一个组，
    Fall time 被静默丢弃；子查询 raw 若保留完整问句，兄弟条目别名也进
    mentioned，两组并列分后按插入序重复选同一身份（两段全 Rise time）。
    """
    from core.param_resolver import parse_param_query, select_values_multi
    evs = [
        ev(param="Rise time", symbol="tr", conditions_raw="VDD=-15 V, VGS=-10 V, ID=-1 A",
           values={"typ": "22", "max": "33"}, unit="ns"),
        ev(param="Fall time", symbol="tf", conditions_raw="VDD=-15 V, VGS=-10 V, ID=-1 A",
           values={"typ": "110", "max": "165"}, unit="ns"),
    ]
    pq = parse_param_query("BSO301SP的上升时间和下降时间分别是多少")
    assert pq.matched_zh == ["上升时间", "下降时间"], pq.matched_zh
    multi = select_values_multi(pq, evs)
    assert multi is not None, "多参数题应返回结果"
    rendered, sels = multi
    assert "33" in rendered and "165" in rendered, rendered
    assert "Rise time" in rendered and "Fall time" in rendered, rendered
    assert len(sels) == 2 and all(s.status == "ok" for s in sels)


def t22_no_param_signal_guard():
    """无参数信号查询（封装/版本号/价格）在 Resolver 中不得靠符号撞表。"""
    from core.param_resolver import parse_param_query, query_has_param_signal
    for q in ("BSO301SP是什么封装", "BSO301SP的文档版本号是多少",
              "BSO301SP的价格是多少，1000片多少钱"):
        pq = parse_param_query(q)
        assert not pq.param_keys and not query_has_param_signal(q), q



def t23_ti_merged_min_typ_max():
    """TI 合并列拆分：表头格 'MIN TYP MAX' + 数据格 '±6 ±25' → typ/max。

    回归背景：物理 4 列（PARAMETER/CONDITIONS/合并数值列/UNIT）被当
    5 逻辑列映射，整格 '±6 ±25' 塞进 pmin、单位串挤进 max（g002 语义
    无法作答）。拆分规则：双值带 ± → (typ,max)，无 ± → (min,max)。
    """
    rows = [
        ["PARAMETER", "CONDITIONS", "MIN TYP MAX", "UNIT"],
        ["OFFSET VOLTAGE", "", "", ""],
        ["VOS Input offset voltage", "", "±6 ±25", "μV"],
        ["VOS Input offset voltage", "TA = -40°C to +125°C", "±0.03 ±0.085", "μV/°C"],
        ["Supply voltage", "TA = -55°C to 70°C", "1.7 5.5", "V"],
    ]
    table = _build_table(rows, fmt="html")
    recs = getattr(table, "param_records", None) or _records_of(table)
    by_typ = {r.typ: r for r in recs if r.typ}
    r1 = by_typ.get("±6")
    assert r1 is not None, f"未拆出 typ=±6: {[(r.param, r.min, r.typ, r.max) for r in recs]}"
    assert r1.max == "±25", (r1.typ, r1.max)
    r2 = [r for r in recs if r.typ == "±0.03"]
    assert r2 and r2[0].max == "±0.085", [(r.typ, r.max) for r in recs]
    r3 = [r for r in recs if r.min == "1.7"]
    assert r3 and r3[0].max == "5.5", [(r.min, r.max) for r in recs]


def _records_of(table):
    from ingestion.table_struct import extract_param_records
    return extract_param_records(table, page=2, table_id="T1")



def t24_opa211_colspan_values():
    """OPA211 类表格：值格用 colspan 表达列角色（横跨=typ/下限/单MAX=max）。"""
    rows = [
        ["PARAMETER", "PARAMETER", "TEST CONDITIONS", "TEST CONDITIONS", "MIN", "TYP", "MAX", "UNIT"],
        ["FREQUENCY RESPONSE", "", "", "", "", "", "", ""],
        ["GBW", "Gain-bandwidth product", "G = 100", "G = 100", "80", "80", "80", "MHz"],
        ["GBW", "Gain-bandwidth product", "G = 1", "G = 1", "45", "45", "45", "MHz"],
        ["", "Overload recovery time", "G=-10", "G=-10", "500", "500", "500", "ns"],
        ["V_OUT", "Voltage output", "RL=10k", "RL=10k", "(V-) + 0.2", "(V-) + 0.2", "(V+) - 0.2", "V"],
    ]
    ranges = [
        {0: (0, 1), 1: (2, 3), 2: (4, 5), 3: (6, 6), 4: (7, 7)},
        {0: (0, 7)},
        {0: (0, 0), 1: (1, 1), 2: (2, 3), 3: (4, 6), 4: (7, 7)},
        {0: (0, 0), 1: (1, 1), 2: (2, 3), 3: (4, 6), 4: (7, 7)},
        {0: (0, 0), 1: (1, 1), 2: (2, 3), 3: (4, 6), 4: (7, 7)},
        {0: (0, 0), 1: (1, 1), 2: (2, 3), 3: (4, 5), 4: (6, 6), 5: (7, 7)},
    ]
    table = _build_table(rows, fmt="html", cell_ranges=ranges)
    recs = _records_of(table)
    by_sym = {}
    for r in recs:
        key = (r.symbol or r.param)
        by_sym.setdefault(key, []).append(r)
    gbw = by_sym.get("GBW") or by_sym.get("Gain-bandwidth product") or []
    assert gbw and len(gbw) == 2, [(r.param, r.min, r.typ, r.max) for r in gbw]
    assert {g.typ for g in gbw} == {"80", "45"}, [(g.typ, g.max) for g in gbw]
    query = parse_param_query("What is the typical gain-bandwidth product of OPA211 at G=100?")
    assert query.conditions == {"g": "100"}, query.conditions
    selection = select_value(query, [ParamEvidence.from_record(record) for record in gbw])
    assert (selection.status, selection.value, selection.unit) == ("ok", "80", "MHz"), selection
    ovl = [r for r in recs if "verload" in (r.param or "")]
    assert ovl and ovl[0].typ == "500", [(r.typ, r.max) for r in ovl]
    vout = [r for r in recs if "voltage output" in ((r.symbol or "") + " " + (r.param or "")).lower()]
    assert vout and vout[0].min == "(V-) + 0.2" and vout[0].max == "(V+) - 0.2", \
        [(r.min, r.max) for r in vout]



def t25_semantic_fallback():
    """语义兜底：别名 miss 的查询经提名路径（skip_identity）仍可选值。"""
    from core.param_resolver import parse_param_query, select_value
    evs = [
        ev(param="Settling time", symbol="tS", conditions_raw="VS=±15 V, G=-1",
           values={"typ": "400"}, unit="ns"),
    ]
    # 'XYZ' 不在受控别名表：常规路径 unsupported
    pq = parse_param_query("What is the XYZ of the OPA211?")
    sel = select_value(pq, evs)
    assert sel.status in ("unsupported_parameter", "not_found"), sel.status
    # 提名路径（skip_identity）：同一证据按条件/列规则选值
    sel2 = select_value(pq, evs, skip_identity=True)
    assert sel2.status == "ok" and sel2.value == "400", (sel2.status, sel2.value)
    # 有工况线索时条件匹配依然生效
    pq2 = parse_param_query("XYZ at VS=±15 V")
    sel3 = select_value(pq2, evs, skip_identity=True)
    assert sel3.status == "ok", sel3.status


def main():
    print("=" * 78)
    print("参数证据链路单元测试 T01–T20")
    print("=" * 78)
    check("T01", "归一化（Unicode 负号/μ/空格/℃）", t01_norm)
    check("T02", "metadata 往返（raw_row/pvals/conditions_raw）", t02_roundtrip)
    check("T03", "旧 schema 记录被拒绝采信", t03_old_schema_rejected)
    check("T04", "rowspan/colspan 展开", t04_rowspan_colspan)
    check("T05", "条件原文不被清空数值", t05_conditions_preserved)
    check("T06", "条件冲突 → condition_conflict，不给数值", t06_conflict_not_ok)
    check("T07", "VGS=-4.5V → 12", t07_vgs45)
    check("T08", "VGS=-10V → 8.0", t08_vgs10)
    check("T09", "无工况多记录 → ambiguous", t09_no_condition_ambiguous)
    check("T10", "ID 不被 IDSS 记录数量压倒", t10_id_vs_idss)
    check("T11", "缺 max 列 → missing_value（不跨列猜）", t11_missing_column_no_guess)
    check("T12", "自定义列标签（steady state）", t12_column_label)
    check("T13", "所有非 ok 状态都有可读输出", t13_status_rendered)
    check("T14", "ID / IDSS 前缀不混淆", t14_no_prefix_confusion)
    check("T15", "条件残缺 → unknown，不给确定值", t15_unknown_condition)
    check("T16", "自定义工况列保存在 value_columns", t16_value_columns_kept)
    check("T17", "答案回显条件与页码", t17_render_has_source)
    check("T18", "参数身份按匹配质量而非记录数", t18_identity_by_score)
    check("T19", "空证据 → not_found", t19_empty)
    check("T20", "英文参数题进入 Resolver 门控", t20_english_gate)
    check("T21", "多参数题逐参数选值（Rise+Fall 都出结果）", t21_multi_param)
    check("T22", "无参数信号查询不撞表（封装/版本号/价格）", t22_no_param_signal_guard)
    check("T23", "TI 合并列拆分（'MIN TYP MAX' 表头 + '±6 ±25' 数据格）", t23_ti_merged_min_typ_max)
    check("T24", "OPA211 colspan 跨列值格（横跨=typ/下限= min/单MAX=max）", t24_opa211_colspan_values)
    check("T25", "语义兜底提名（skip_identity 选值）", t25_semantic_fallback)

    passed = sum(1 for _, _, ok, _ in RESULTS if ok)
    print("=" * 78)
    print(f"通过 {passed}/{len(RESULTS)}")
    for tid, desc, ok, err in RESULTS:
        if not ok:
            print(f"  FAIL {tid} {desc}: {err}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
